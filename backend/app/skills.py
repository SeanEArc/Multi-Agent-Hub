"""The hub's skill library: skills any agent can use, and each agent's own skills and commands.

A skill is a folder with a `SKILL.md` (YAML frontmatter with `name` and `description`, then instructions),
the format both Claude Code and Codex load. The library lives in `HUB_SKILLS_DIR`:

- `library.json`: one entry per skill with its source, path and a switch per agent.
- `library/<name>/`: skills copied in from a git repository or a zip.
- `views/claude/`: a Claude Code plugin (`--plugin-dir`) holding junctions to the skills on for Claude.
- `staging/<id>/`: downloads waiting for the user to look at them before they are added.

Skills shared from the user's own Claude or Codex folders are linked, not copied: the entry keeps the path,
so edits carry over, and the skill shows as missing if the folder goes away.

How each agent gets library skills: Claude through `--plugin-dir` (named `hub:<name>`), Codex through a
list in `developer_instructions` (its own skills work the same way: it reads the `SKILL.md` when a task
matches), and the local model through the same list in its system prompt plus read access to the folders.
A skill picked from the composer's `/` menu is spelled out in the prompt (`expand_command`), so it works
without relying on the model to find it. Nothing in a skill runs when it is imported.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlmodel import Session

from app.models import AgentKind, Setting, utcnow

SKILL_FILE = "SKILL.md"
AGENTS = (AgentKind.CLAUDE, AgentKind.CODEX, AgentKind.OLLAMA)
CLAUDE_PLUGIN = "hub"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SCRIPT_SUFFIXES = {".py", ".ps1", ".psm1", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".bat", ".cmd", ".rb", ".exe"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
MAX_DEPTH = 6
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_SKILL_TEXT = 40_000
STAGING_MAX_AGE = 3600.0
GIT_TIMEOUT = 120.0
CAPABILITIES_KEY = "agent_capabilities"

# A skill that names another CLI's own tools only partly works elsewhere; the switch starts off there.
TOOL_HINTS: dict[AgentKind, re.Pattern[str]] = {
    AgentKind.CLAUDE: re.compile(
        r"\b(AskUserQuestion|TodoWrite|NotebookEdit|ExitPlanMode|EnterPlanMode|SendUserMessage|"
        r"(?:Agent|Task|Skill) tool)\b"
    ),
    AgentKind.CODEX: re.compile(r"\b(apply_patch|update_plan|request_user_input)\b"),
}
# Frontmatter only Claude Code understands (other agents ignore it, so the skill may behave differently).
CLAUDE_FRONTMATTER = {"allowed-tools", "model", "context", "agent", "hooks", "disable-model-invocation",
                      "user-invocable", "argument-hint"}

# Built-in commands that work headless, checked against Claude Code 2.1.322 (`/status`, `/help`, `/memory`
# and others answer "isn't available in this environment"; `/compact` has the hub's own for now; `/clear` is
# left out (the user decided it is redundant in the hub: a new chat does the same); `/model` has the hub's picker).
CLAUDE_BUILTINS = {
    "context": "Show how full the context window is",
    "usage": "Show your plan's usage",
}
# The hub's own commands, for every agent (phase 7 adds /delegate, phase 8 /split). `Orchestrator.send` runs
# them instead of sending the message; they come first in the menu, so a skill can't hide them.
HUB_COMMANDS: dict[str, str] = {
    "compact": "Start fresh from the hub's summary of the session; add text to keep something in focus",
}


class SkillError(ValueError):
    """A skill or import the user should be told about (shown as a 400)."""


# --- Reading skills ------------------------------------------------------------------------------


@dataclass
class SkillInfo:
    name: str
    description: str
    folder: Path
    content: str
    scripts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # The per-agent switches the hub suggests from the warnings.
    suggested: dict[str, bool] = field(default_factory=dict)

    def to_json(self, root: Path | None = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "path": str(self.folder),
            "subpath": self.folder.relative_to(root).as_posix() if root and self.folder.is_relative_to(root) else None,
            "content": self.content,
            "scripts": self.scripts,
            "warnings": self.warnings,
            "suggested": self.suggested,
        }


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """The YAML frontmatter of a `SKILL.md` and the text after it."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            try:
                meta = yaml.safe_load("".join(lines[1:i])) or {}
            except yaml.YAMLError as exc:
                raise SkillError(f"its frontmatter isn't valid YAML: {exc}") from exc
            return (meta if isinstance(meta, dict) else {}), "".join(lines[i + 1:])
    return {}, text


def read_skill(folder: Path) -> SkillInfo:
    """A skill folder's name, description, contents, scripts and portability warnings."""
    path = folder / SKILL_FILE
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SkillError(f"{path} can't be read: {exc.strerror or exc}") from exc
    try:
        meta, body = parse_frontmatter(text)
    except SkillError as exc:
        raise SkillError(f"{path}: {exc}") from exc
    name = str(meta.get("name") or folder.name).strip()
    description = " ".join(str(meta.get("description") or "").split())
    if not description:
        raise SkillError(f"{path} has no description in its frontmatter, so no agent would know when to use it.")

    scripts = sorted(
        p.relative_to(folder).as_posix() for p in _walk_files(folder) if p.suffix.lower() in SCRIPT_SUFFIXES
    )
    warnings: list[str] = []
    suggested = {agent.value: True for agent in AGENTS}
    for agent, pattern in TOOL_HINTS.items():
        found = sorted(set(pattern.findall(body)))
        if found:
            others = [a for a in AGENTS if a is not agent]
            for other in others:
                suggested[other.value] = False
            warnings.append(f"It names {_agent_name(agent)}'s own tools ({', '.join(found)}), so it may only "
                            f"partly work for the other agents.")
    claude_keys = sorted(CLAUDE_FRONTMATTER & set(meta))
    if claude_keys:
        warnings.append(f"Its frontmatter uses settings only Claude Code understands ({', '.join(claude_keys)}).")
    if scripts:
        suggested[AgentKind.OLLAMA.value] = False
        warnings.append(f"It includes {len(scripts)} script{'s' if len(scripts) != 1 else ''}, which an agent "
                        "may run. Check them before turning it on.")
    return SkillInfo(name=name, description=description, folder=folder.resolve(), content=text[:MAX_SKILL_TEXT],
                     scripts=scripts, warnings=warnings, suggested=suggested)


def find_skill_folders(root: Path, max_depth: int = MAX_DEPTH) -> list[Path]:
    """Folders under `root` (or `root` itself) that hold a `SKILL.md`, not looking inside a found skill."""
    found: list[Path] = []

    def visit(folder: Path, depth: int) -> None:
        if (folder / SKILL_FILE).is_file():
            found.append(folder)
            return
        if depth >= max_depth:
            return
        try:
            children = sorted(p for p in folder.iterdir() if p.is_dir() and p.name not in SKIP_DIRS)
        except OSError:
            return
        for child in children:
            visit(child, depth + 1)

    visit(root, 0)
    return found


def _walk_files(folder: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def _agent_name(agent: AgentKind) -> str:
    return {AgentKind.CLAUDE: "Claude Code", AgentKind.CODEX: "Codex", AgentKind.OLLAMA: "the local model"}[agent]


def slug(name: str) -> str:
    """A skill name usable as a folder and a `/` command."""
    value = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-._")[:64]
    if not value or not NAME_PATTERN.match(value):
        raise SkillError(f"{name!r} can't be used as a skill name.")
    return value


# --- The library ---------------------------------------------------------------------------------


@dataclass
class LibrarySkill:
    name: str
    source: str  # "link" (the user's own folder) or "copy" (from a git repository or a zip)
    path: Path
    agents: dict[str, bool]
    # The CLI whose own folder a linked skill lives in: that CLI already has it and isn't given it twice.
    origin: str | None = None
    repo: str | None = None
    commit: str | None = None
    subpath: str | None = None
    added_at: str | None = None

    @property
    def present(self) -> bool:
        return (self.path / SKILL_FILE).is_file()

    def enabled_for(self, agent: AgentKind) -> bool:
        return bool(self.agents.get(agent.value)) and self.present

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "path": str(self.path), "agents": self.agents,
                "origin": self.origin, "repo": self.repo, "commit": self.commit, "subpath": self.subpath,
                "added_at": self.added_at}


class SkillLibrary:
    """The skills in `root` and their per-agent switches. Safe to create per call (state lives on disk)."""

    def __init__(self, root: Path, claude_home: Path | None = None, codex_home: Path | None = None) -> None:
        self.root = root
        self.claude_home = claude_home or Path.home() / ".claude"
        self.codex_home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self.agents_home = Path.home() / ".agents"

    # Index

    @property
    def index_path(self) -> Path:
        return self.root / "library.json"

    def skills(self) -> list[LibrarySkill]:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        skills = []
        for name, entry in sorted((raw.get("skills") or {}).items()):
            try:
                skills.append(LibrarySkill(
                    name=name, source=entry["source"], path=Path(entry["path"]),
                    agents={a.value: bool((entry.get("agents") or {}).get(a.value)) for a in AGENTS},
                    origin=entry.get("origin"), repo=entry.get("repo"), commit=entry.get("commit"),
                    subpath=entry.get("subpath"), added_at=entry.get("added_at"),
                ))
            except (KeyError, TypeError):
                continue
        return skills

    def get(self, name: str) -> LibrarySkill | None:
        return next((s for s in self.skills() if s.name == name), None)

    def _write(self, skills: Sequence[LibrarySkill]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = {"skills": {s.name: {k: v for k, v in s.to_json().items() if k != "name"} for s in skills}}
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)
        self._build_claude_view(skills)

    def origin_of(self, folder: Path) -> str | None:
        """Which CLI already loads a skill in this folder by itself (its own skill folders), if any."""
        folder = folder.resolve()
        if folder.is_relative_to(self.claude_home.resolve()):
            return AgentKind.CLAUDE.value
        if folder.is_relative_to(self.codex_home.resolve()) or folder.is_relative_to(self.agents_home.resolve()):
            return AgentKind.CODEX.value
        return None

    # Changes

    def link(self, folder: Path, agents: dict[str, bool]) -> LibrarySkill:
        """Add a skill folder on this PC, read where it lives."""
        info = read_skill(folder)
        return self._add(LibrarySkill(name=slug(info.name), source="link", path=info.folder,
                                      agents=_switches(agents), origin=self.origin_of(info.folder),
                                      added_at=utcnow().isoformat()))

    def copy_in(self, folder: Path, agents: dict[str, bool], repo: str | None = None, commit: str | None = None,
                subpath: str | None = None) -> LibrarySkill:
        """Copy a downloaded skill folder into the library."""
        info = read_skill(folder)
        name = slug(info.name)
        if self.get(name) is not None:
            raise SkillError(f"A skill named {name} is already in the library. Remove it first.")
        target = self.root / "library" / name
        if target.exists():
            _remove_tree(target)
        shutil.copytree(folder, target, ignore=shutil.ignore_patterns(*SKIP_DIRS), symlinks=False)
        return self._add(LibrarySkill(name=name, source="copy", path=target.resolve(), agents=_switches(agents),
                                      repo=repo, commit=commit, subpath=subpath, added_at=utcnow().isoformat()))

    def _add(self, skill: LibrarySkill) -> LibrarySkill:
        skills = self.skills()
        if any(s.name == skill.name for s in skills):
            raise SkillError(f"A skill named {skill.name} is already in the library. Remove it first.")
        self._write([*skills, skill])
        return skill

    def set_agents(self, name: str, agents: dict[str, bool]) -> LibrarySkill:
        skills = self.skills()
        skill = next((s for s in skills if s.name == name), None)
        if skill is None:
            raise KeyError(name)
        skill.agents = {**skill.agents, **_switches(agents, partial=True)}
        self._write(skills)
        return skill

    def remove(self, name: str) -> None:
        skills = self.skills()
        skill = next((s for s in skills if s.name == name), None)
        if skill is None:
            raise KeyError(name)
        self._write([s for s in skills if s.name != name])
        # Only a copy the hub made is deleted; a linked folder is the user's own.
        if skill.source == "copy" and skill.path.is_relative_to((self.root / "library").resolve()):
            _remove_tree(skill.path)

    def replace_copy(self, name: str, folder: Path, commit: str | None) -> LibrarySkill:
        """Swap a copied skill's files for a newer download (the Update button), keeping its switches."""
        skills = self.skills()
        skill = next((s for s in skills if s.name == name), None)
        if skill is None or skill.source != "copy":
            raise SkillError(f"{name} isn't a downloaded skill, so it can't be updated.")
        read_skill(folder)  # still a valid skill
        _remove_tree(skill.path)
        shutil.copytree(folder, skill.path, ignore=shutil.ignore_patterns(*SKIP_DIRS), symlinks=False)
        skill.commit = commit
        self._write(skills)
        return skill

    # What each agent gets

    def for_agent(self, agent: AgentKind) -> list[LibrarySkill]:
        """Library skills switched on for `agent` that it doesn't already load from its own folders."""
        return [s for s in self.skills() if s.enabled_for(agent) and s.origin != agent.value]

    def claude_plugin_dir(self) -> Path | None:
        """The `--plugin-dir` for Claude, or None when no library skill is on for it."""
        view = self.root / "views" / "claude"
        if not self.for_agent(AgentKind.CLAUDE) or not (view / ".claude-plugin" / "plugin.json").is_file():
            return None
        return view

    def _build_claude_view(self, skills: Sequence[LibrarySkill]) -> None:
        """Rebuild the Claude plugin: one junction per skill on for Claude (a copy where junctions fail)."""
        view = self.root / "views" / "claude"
        links = view / "skills"
        links.mkdir(parents=True, exist_ok=True)
        (view / ".claude-plugin").mkdir(exist_ok=True)
        (view / ".claude-plugin" / "plugin.json").write_text(json.dumps({
            "name": CLAUDE_PLUGIN, "version": "1.0.0", "description": "Skills from the Multi-Agent Hub library",
        }), encoding="utf-8")
        for entry in links.iterdir():
            _remove_link(entry)
        for skill in skills:
            if skill.enabled_for(AgentKind.CLAUDE) and skill.origin != AgentKind.CLAUDE.value:
                _link_folder(skill.path, links / skill.name)

    def instructions(self, agent: AgentKind) -> str | None:
        """The list of library skills for Codex's developer instructions or the local model's system prompt."""
        skills = self.for_agent(agent)
        if not skills:
            return None
        lines = [
            "## Skills from Multi-Agent Hub",
            "",
            "Each skill below has a name, a description of when to use it, and its SKILL.md file. When a task "
            "matches a description, or the user writes /name, read that SKILL.md first (with your file-reading "
            "tool) and follow it. Files the skill mentions are in the same folder as its SKILL.md.",
            "",
        ]
        for skill in skills:
            try:
                description = read_skill(skill.path).description
            except SkillError:
                continue
            lines.append(f"- {skill.name}: {description} File: {skill.path / SKILL_FILE}")
        return "\n".join(lines)

    def read_roots(self, agent: AgentKind) -> list[Path]:
        """Folders the local model may read (not write) outside its working folder: its skills."""
        return [s.path for s in self.for_agent(agent)]

    # Staging (imports waiting for the user)

    def staging_dir(self) -> Path:
        return self.root / "staging"

    def new_staging(self) -> tuple[str, Path]:
        self._clean_staging()
        staging_id = uuid.uuid4().hex
        path = self.staging_dir() / staging_id
        path.mkdir(parents=True)
        return staging_id, path

    def staging(self, staging_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", staging_id or ""):
            raise SkillError("That download has expired. Start the import again.")
        path = self.staging_dir() / staging_id
        if not path.is_dir():
            raise SkillError("That download has expired. Start the import again.")
        return path

    def drop_staging(self, staging_id: str) -> None:
        try:
            _remove_tree(self.staging(staging_id))
        except SkillError:
            pass

    def _clean_staging(self) -> None:
        now = time.time()
        try:
            entries = list(self.staging_dir().iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if now - entry.stat().st_mtime > STAGING_MAX_AGE:
                    _remove_tree(entry)
            except OSError:
                continue


def _switches(agents: dict[str, bool] | None, partial: bool = False) -> dict[str, bool]:
    agents = agents or {}
    if partial:
        return {a.value: bool(agents[a.value]) for a in AGENTS if a.value in agents}
    return {a.value: bool(agents.get(a.value)) for a in AGENTS}


def _link_folder(target: Path, link: Path) -> None:
    try:
        import _winapi  # Windows only

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, OSError):
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            shutil.copytree(target, link, ignore=shutil.ignore_patterns(*SKIP_DIRS))


def _remove_link(path: Path) -> None:
    """Remove a view entry without ever following a junction or symlink into the real skill folder."""
    if path.is_junction():
        os.rmdir(path)  # removes the junction only
    elif path.is_symlink():
        path.unlink()
    elif path.is_dir():
        _remove_tree(path)
    else:
        path.unlink(missing_ok=True)


def _remove_tree(path: Path) -> None:
    """Delete a folder the hub made, without following junctions or symlinks inside it."""
    if not path.exists() and not path.is_junction():
        return
    if path.is_junction() or path.is_symlink():
        _remove_link(path)
        return

    def on_error(func: Any, failed: str, exc: BaseException) -> None:
        # Git marks its object files read-only.
        os.chmod(failed, 0o700)
        func(failed)

    shutil.rmtree(path, onexc=on_error)


# --- Importing -----------------------------------------------------------------------------------


GIT_URL = re.compile(r"^(https://|git@)[\w.@:/~+-]+$")


def is_git_url(source: str) -> bool:
    return bool(GIT_URL.match(source.strip())) and not Path(source).exists()


async def clone(url: str, into: Path) -> str | None:
    """Shallow-clone `url` into `into` (no hooks run, no submodules) and return the commit, or raise SkillError."""
    if url.startswith("-"):
        raise SkillError("That isn't a repository address.")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", "core.hooksPath=NUL", "-c", "protocol.file.allow=never", "clone", "--depth", "1",
        "--no-recurse-submodules", "--", url, str(into),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise SkillError("Downloading the repository took too long.") from None
    if proc.returncode != 0:
        message = stderr.decode("utf-8", "replace").strip().splitlines()
        raise SkillError(f"The repository couldn't be downloaded: {message[-1] if message else 'git failed'}")
    head = await asyncio.create_subprocess_exec(
        "git", "-C", str(into), "rev-parse", "HEAD", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await head.communicate()
    return out.decode().strip() or None


def extract_zip(zip_path: Path, into: Path) -> None:
    """Unpack a zip, refusing paths that would land outside `into` and archives that are too big."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            if sum(i.file_size for i in archive.infolist()) > MAX_ZIP_BYTES:
                raise SkillError("The zip is larger than 50 MB unpacked.")
            root = into.resolve()
            for item in archive.infolist():
                target = (into / item.filename).resolve()
                if not target.is_relative_to(root):
                    raise SkillError(f"The zip holds a path outside its folder ({item.filename}).")
            archive.extractall(into)
    except zipfile.BadZipFile as exc:
        raise SkillError(f"{zip_path.name} isn't a valid zip file.") from exc


def diff_skill(old: Path, new: Path) -> dict[str, Any]:
    """What an update would change: the SKILL.md diff and the files added, removed or changed."""
    import difflib

    def files(root: Path) -> dict[str, bytes]:
        out = {}
        for p in _walk_files(root):
            try:
                out[p.relative_to(root).as_posix()] = p.read_bytes()
            except OSError:
                continue
        return out

    before, after = files(old), files(new)
    old_text = before.get(SKILL_FILE, b"").decode("utf-8", "replace").splitlines()
    new_text = after.get(SKILL_FILE, b"").decode("utf-8", "replace").splitlines()
    return {
        "skill_diff": "\n".join(difflib.unified_diff(old_text, new_text, "SKILL.md (now)", "SKILL.md (new)",
                                                     lineterm=""))[:MAX_SKILL_TEXT],
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(k for k in set(before) & set(after) if before[k] != after[k] and k != SKILL_FILE),
    }


# --- The agents' own skills and commands ---------------------------------------------------------


def record_capabilities(db: Session, agent: AgentKind, capabilities: dict[str, Any]) -> None:
    """Remember what an agent reported it can use (Claude's `init` event), for the `/` menu."""
    row = db.get(Setting, CAPABILITIES_KEY)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    entry = {**capabilities, "updated_at": utcnow().isoformat()}
    previous = stored.get(agent.value) or {}
    if {k: v for k, v in previous.items() if k != "updated_at"} == capabilities:
        return  # nothing new: skip the write after every turn
    stored[agent.value] = entry
    db.merge(Setting(key=CAPABILITIES_KEY, value=stored))
    db.commit()


def load_capabilities(db: Session, agent: AgentKind) -> dict[str, Any] | None:
    row = db.get(Setting, CAPABILITIES_KEY)
    stored = row.value if row and isinstance(row.value, dict) else {}
    return stored.get(agent.value)


def claude_capabilities(init: dict[str, Any]) -> dict[str, Any]:
    """What the `/` menu needs from Claude Code's `system`/`init` stream event."""
    return {
        "skills": [s for s in init.get("skills") or [] if isinstance(s, str)],
        "slash_commands": [s for s in init.get("slash_commands") or [] if isinstance(s, str)],
        "terminal_slash_commands": [s for s in init.get("terminal_slash_commands") or [] if isinstance(s, str)],
        "mcp_servers": [
            {"name": m.get("name"), "status": m.get("status")}
            for m in init.get("mcp_servers") or [] if isinstance(m, dict) and m.get("name")
        ],
        "tools": [t for t in init.get("tools") or [] if isinstance(t, str)],
    }


async def probe_claude(command: Sequence[str], cwd: Path, timeout: float = 60.0) -> dict[str, Any] | None:
    """Claude Code's `init` event from a run of `/context`, a local command that makes no model request."""
    proc = await asyncio.create_subprocess_exec(
        *command, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
        "--permission-prompts", "none",
        cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert proc.stdin and proc.stdout
        proc.stdin.write(b"/context")
        await proc.stdin.drain()
        proc.stdin.close()
        async with asyncio.timeout(timeout):
            while line := await proc.stdout.readline():
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if raw.get("type") == "system" and raw.get("subtype") == "init":
                    return claude_capabilities(raw)
    except TimeoutError:
        return None
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    return None


_codex_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
CODEX_CACHE_SECONDS = 30.0


async def codex_skills(command: Sequence[str], cwd: Path, timeout: float = 20.0) -> list[dict[str, Any]] | None:
    """Codex's enabled skills for `cwd` from `codex app-server` (`skills/list`), or None if it can't be asked.

    Codex's own answer to "list your skills" changes from run to run; the app server's list is exact.
    It never reads `auth.json`: the app server is Codex's own process.
    """
    key = str(cwd)
    cached = _codex_cache.get(key)
    if cached and time.monotonic() - cached[0] < CODEX_CACHE_SECONDS:
        return cached[1]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, "app-server", cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None
    assert proc.stdin and proc.stdout

    async def send(message: dict[str, Any]) -> None:
        proc.stdin.write((json.dumps(message) + "\n").encode())
        await proc.stdin.drain()

    async def reply(request_id: int) -> dict[str, Any]:
        while line := await proc.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                return message
        raise EOFError

    try:
        async with asyncio.timeout(timeout):
            await send({"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "multi-agent-hub",
                                                                                    "version": "1"}}})
            await reply(0)
            await send({"method": "initialized"})
            await send({"id": 1, "method": "skills/list", "params": {"cwds": [str(cwd)]}})
            result = (await reply(1)).get("result") or {}
    except (TimeoutError, EOFError, OSError):
        return None
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    skills = [
        {"name": s.get("name"), "description": s.get("description") or "", "path": s.get("path"),
         "scope": s.get("scope")}
        for entry in result.get("data") or [] for s in entry.get("skills") or []
        if s.get("name") and s.get("enabled", True)
    ]
    _codex_cache[key] = (time.monotonic(), skills)
    return skills


def cached_codex_names() -> set[str]:
    """Codex skill names seen by any recent `skills/list` (the `/` menu asks before a command is sent)."""
    return {s["name"] for _, skills in _codex_cache.values() for s in skills}


# --- The `/` menu and sending a command ----------------------------------------------------------


@dataclass
class Command:
    name: str
    description: str
    kind: str  # "skill" (the agent's own), "library", "builtin" or "hub"

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description, "kind": self.kind}


def menu(agent: AgentKind, library: SkillLibrary, native: Sequence[dict[str, Any]] | None,
         hub: bool = True) -> list[Command]:
    """The `/` menu for `agent`: the hub's commands (with `hub`; a new chat has nothing to compact), its own
    skills (`native`, names and descriptions), library skills switched on for it and its checked built-in
    commands. Each name once."""
    commands: list[Command] = []
    seen: set[str] = set()

    def add(name: str, description: str, kind: str) -> None:
        if name and name not in seen:
            seen.add(name)
            commands.append(Command(name, description, kind))

    if hub:
        for name, description in HUB_COMMANDS.items():
            add(name, description, "hub")
    for skill in native or []:
        name = str(skill.get("name") or "")
        if agent is AgentKind.CLAUDE and name.startswith(f"{CLAUDE_PLUGIN}:"):
            continue  # the library's own plugin, listed below under its plain name
        add(name, str(skill.get("description") or ""), "skill")
    for skill in library.for_agent(agent):
        try:
            add(skill.name, read_skill(skill.path).description, "library")
        except SkillError:
            continue
    if agent is AgentKind.CLAUDE:
        for name, description in CLAUDE_BUILTINS.items():
            add(name, description, "builtin")
    return commands


def claude_native(capabilities: dict[str, Any] | None, claude_home: Path) -> list[dict[str, Any]]:
    """Claude's own skills for the menu: the names from `init`, with descriptions from its skill folders."""
    if not capabilities:
        return []
    descriptions: dict[str, str] = {}
    for folder in find_skill_folders(claude_home / "skills", max_depth=3) if (claude_home / "skills").is_dir() else []:
        try:
            info = read_skill(folder)
        except SkillError:
            continue
        descriptions.setdefault(info.name, info.description)
    return [{"name": name, "description": descriptions.get(name.split(":")[-1], "")}
            for name in capabilities.get("skills") or []]


COMMAND_PATTERN = re.compile(r"^/([A-Za-z0-9][\w.:-]*)(?:\s+(.*))?$", re.DOTALL)


def hub_command(text: str) -> tuple[str, str] | None:
    """`(name, rest)` when the message is one of the hub's own commands, else None."""
    match = COMMAND_PATTERN.match(text.strip())
    if not match or match.group(1) not in HUB_COMMANDS:
        return None
    return match.group(1), (match.group(2) or "").strip()


def expand_command(agent: AgentKind, text: str, library: SkillLibrary,
                   codex_names: Iterable[str] = ()) -> str:
    """The prompt the agent gets for a message that starts with `/name` (the message shown stays as typed).

    A library skill on for the agent: Claude gets `/hub:name`; Codex and the local model get the skill's
    instructions and folder in front of the message, so it works without the model having to find it.
    A Codex skill of its own gets Codex's syntax (`$name`). Anything else is sent unchanged.
    """
    match = COMMAND_PATTERN.match(text.strip())
    if not match:
        return text
    name, rest = match.group(1), (match.group(2) or "").strip()
    skill = next((s for s in library.for_agent(agent) if s.name == name), None)
    if skill is not None:
        if agent is AgentKind.CLAUDE:
            return f"/{CLAUDE_PLUGIN}:{name}" + (f" {rest}" if rest else "")
        try:
            info = read_skill(skill.path)
        except SkillError:
            return text
        _, body = parse_frontmatter(info.content)
        request = rest or "Run this skill."
        return (
            f"Use the skill \"{name}\" for this request. Its instructions follow; files it mentions are in "
            f"{skill.path}.\n\n<skill name=\"{name}\">\n{body.strip()}\n</skill>\n\n## The request\n\n{request}"
        )
    if agent is AgentKind.CODEX and name in set(codex_names):
        return f"${name}" + (f" {rest}" if rest else "")
    return text


def shareable(library: SkillLibrary, codex_list: Sequence[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The user's own Claude and Codex skills that could be linked into the library for the other agents."""
    linked = {str(s.path).lower() for s in library.skills()}
    out: list[dict[str, Any]] = []
    claude_skills = library.claude_home / "skills"
    for folder in find_skill_folders(claude_skills, max_depth=3) if claude_skills.is_dir() else []:
        try:
            info = read_skill(folder)
        except SkillError:
            continue
        out.append({"name": info.name, "description": info.description, "path": str(info.folder),
                    "agent": AgentKind.CLAUDE.value, "linked": str(info.folder).lower() in linked})
    for skill in codex_list or []:
        if skill.get("scope") not in ("user", "repo") or not skill.get("path"):
            continue
        folder = Path(skill["path"]).parent
        out.append({"name": skill["name"], "description": skill.get("description") or "", "path": str(folder),
                    "agent": AgentKind.CODEX.value, "linked": str(folder.resolve()).lower() in linked})
    return out
