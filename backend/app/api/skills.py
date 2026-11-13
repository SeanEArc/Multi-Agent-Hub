"""The skill library (import, switches, update) and each agent's `/` menu."""

import json
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlmodel import Session

from app.adapters.claude import ClaudeCliNotFound, find_claude
from app.adapters.codex import CodexCliNotFound, find_codex
from app.config import get_settings
from app.db import get_session
from app.models import AgentKind, AgentSession
from app.orchestrator import AgentUnavailable, session_workdir
from app.skills import (
    SkillError,
    SkillLibrary,
    claude_native,
    clone,
    codex_skills,
    diff_skill,
    extract_zip,
    find_skill_folders,
    is_git_url,
    load_capabilities,
    menu,
    probe_claude,
    read_skill,
    record_capabilities,
    shareable,
)

router = APIRouter(prefix="/skills", tags=["skills"])

DbSession = Annotated[Session, Depends(get_session)]
META_FILE = "meta.json"
SOURCE_DIR = "src"


class Switches(BaseModel):
    claude: bool = False
    codex: bool = False
    ollama: bool = False


class PreviewRequest(BaseModel):
    # A folder or zip on this PC, or a git repository address.
    source: str


class Pick(BaseModel):
    # The skill folder: absolute for a folder import, relative to the download for a repository or zip.
    path: str
    agents: Switches


class AddRequest(BaseModel):
    source: str
    staging_id: str | None = None
    picks: list[Pick]


class SwitchUpdate(BaseModel):
    agents: dict[str, bool]


class ApplyUpdate(BaseModel):
    staging_id: str


def _library(request: Request) -> SkillLibrary:
    return request.app.state.skills


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _entry(skill: Any) -> dict[str, Any]:
    out = skill.to_json()
    out["present"] = skill.present
    try:
        info = read_skill(skill.path)
        out.update(description=info.description, warnings=info.warnings, scripts=info.scripts)
    except SkillError as exc:
        out.update(description="", warnings=[str(exc)] if skill.present else [], scripts=[])
    return out


@router.get("")
def list_skills(request: Request) -> dict[str, Any]:
    """The library: each skill with its source, per-agent switches, and whether its folder is still there."""
    return {"skills": [_entry(s) for s in _library(request).skills()], "root": str(_library(request).root)}


@router.get("/available")
async def available(request: Request) -> dict[str, Any]:
    """The user's own Claude and Codex skills that can be shared with the other agents (linked, not copied)."""
    library = _library(request)
    codex = await _codex_list(request, Path.home())
    return {"skills": shareable(library, codex)}


@router.post("/preview")
async def preview(body: PreviewRequest, request: Request) -> dict[str, Any]:
    """Find the skills in a folder, zip or git repository, for the user to look at before adding any.

    Downloads go to a staging folder that is kept for an hour. Nothing in them is run.
    """
    library = _library(request)
    source = body.source.strip().strip('"')
    if not source:
        raise _bad(SkillError("Enter a folder, a zip file or a git repository address."))
    staging_id: str | None = None
    meta: dict[str, Any] = {"source": source}
    try:
        if is_git_url(source):
            staging_id, staging = library.new_staging()
            meta["kind"] = "git"
            meta["commit"] = await clone(source, staging / SOURCE_DIR)
            root = staging / SOURCE_DIR
        elif source.lower().endswith(".zip") and Path(source).is_file():
            staging_id, staging = library.new_staging()
            meta["kind"] = "zip"
            extract_zip(Path(source), staging / SOURCE_DIR)
            root = staging / SOURCE_DIR
        elif Path(source).is_dir():
            meta["kind"] = "folder"
            root = Path(source).resolve()
        else:
            raise SkillError(f"{source} isn't a folder, a zip file or a git repository address (https://...).")
        if staging_id:
            (library.staging(staging_id) / META_FILE).write_text(json.dumps(meta), encoding="utf-8")
        skills, problems = [], []
        for folder in find_skill_folders(root):
            try:
                skills.append(read_skill(folder).to_json(root))
            except SkillError as exc:
                problems.append(str(exc))
        if not skills:
            raise SkillError("No skills were found there (a skill is a folder with a SKILL.md file)."
                             + (f" {problems[0]}" if problems else ""))
    except SkillError as exc:
        if staging_id:
            library.drop_staging(staging_id)
        raise _bad(exc) from None
    existing = {s.name for s in library.skills()}
    for skill in skills:
        skill["exists"] = skill["name"] in existing
    return {"staging_id": staging_id, "kind": meta["kind"], "source": source, "skills": skills,
            "problems": problems}


@router.post("", status_code=status.HTTP_201_CREATED)
def add(body: AddRequest, request: Request) -> dict[str, Any]:
    """Add the picked skills: a folder on this PC is linked; a repository or zip download is copied in."""
    library = _library(request)
    added = []
    try:
        if body.staging_id:
            staging = library.staging(body.staging_id)
            meta = json.loads((staging / META_FILE).read_text(encoding="utf-8"))
            root = (staging / SOURCE_DIR).resolve()
            for pick in body.picks:
                folder = (root / pick.path).resolve()
                if not folder.is_relative_to(root):
                    raise SkillError(f"{pick.path} isn't part of the download.")
                added.append(library.copy_in(folder, pick.agents.model_dump(), repo=meta.get("source")
                                             if meta.get("kind") == "git" else None, commit=meta.get("commit"),
                                             subpath=pick.path))
            library.drop_staging(body.staging_id)
        else:
            for pick in body.picks:
                added.append(library.link(Path(pick.path), pick.agents.model_dump()))
    except (SkillError, OSError, ValueError) as exc:
        raise _bad(exc) from None
    return {"added": [s.name for s in added], "skills": [_entry(s) for s in library.skills()]}


@router.patch("/{name}")
def set_switches(name: str, body: SwitchUpdate, request: Request) -> dict[str, Any]:
    try:
        skill = _library(request).set_agents(name, body.agents)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No skill named {name}") from None
    return _entry(skill)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def remove(name: str, request: Request) -> None:
    """Take a skill out of the library. A linked folder is left alone; a downloaded copy is deleted."""
    try:
        _library(request).remove(name)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No skill named {name}") from None


@router.post("/{name}/update")
async def check_update(name: str, request: Request) -> dict[str, Any]:
    """Download a repository skill again and show what would change; nothing changes until it is applied."""
    library = _library(request)
    skill = library.get(name)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No skill named {name}")
    if not skill.repo:
        raise _bad(SkillError(f"{name} didn't come from a git repository, so there is nothing to update from."))
    staging_id, staging = library.new_staging()
    try:
        commit = await clone(skill.repo, staging / SOURCE_DIR)
        folder = (staging / SOURCE_DIR / (skill.subpath or "")).resolve()
        if not folder.is_relative_to((staging / SOURCE_DIR).resolve()):
            raise SkillError("The skill's place in the repository is invalid.")
        read_skill(folder)
    except SkillError as exc:
        library.drop_staging(staging_id)
        raise _bad(SkillError(f"{exc} (the skill may have moved or been removed from the repository)")) from None
    (staging / META_FILE).write_text(json.dumps({"source": skill.repo, "kind": "git", "commit": commit}),
                                     encoding="utf-8")
    changes = diff_skill(skill.path, folder)
    unchanged = commit == skill.commit or not any(changes[k] for k in ("skill_diff", "added", "removed", "changed"))
    if unchanged:
        library.drop_staging(staging_id)
    return {"staging_id": None if unchanged else staging_id, "commit": commit, "current_commit": skill.commit,
            "up_to_date": unchanged, **changes}


@router.post("/{name}/update/apply")
def apply_update(name: str, body: ApplyUpdate, request: Request) -> dict[str, Any]:
    library = _library(request)
    skill = library.get(name)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No skill named {name}")
    try:
        staging = library.staging(body.staging_id)
        meta = json.loads((staging / META_FILE).read_text(encoding="utf-8"))
        folder = (staging / SOURCE_DIR / (skill.subpath or "")).resolve()
        updated = library.replace_copy(name, folder, meta.get("commit"))
    except (SkillError, OSError, ValueError) as exc:
        raise _bad(exc) from None
    library.drop_staging(body.staging_id)
    return _entry(updated)


@router.get("/commands")
async def commands(
    request: Request,
    db: DbSession,
    agent: AgentKind,
    session_id: str | None = None,
    path: str | None = None,
    refresh: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """The `/` menu for `agent`: its own skills and checked built-ins, library skills on for it, hub commands.

    Claude's list comes from its last turn's `init` event (or, with none yet or `refresh`, from a run of
    `/context`, which makes no model request). Codex's comes from its app server, for the session's folder.
    """
    library = _library(request)
    cwd = _cwd(db, session_id, path)
    native: list[dict[str, Any]] | None = None
    mcp: list[dict[str, Any]] = []
    if agent is AgentKind.CLAUDE:
        capabilities = load_capabilities(db, AgentKind.CLAUDE)
        if (capabilities is None or refresh) and request.app.state.discover_native:
            capabilities = await _probe_claude(cwd) or capabilities
            if capabilities:
                record_capabilities(db, AgentKind.CLAUDE, {k: v for k, v in capabilities.items()
                                                           if k != "updated_at"})
        native = claude_native(capabilities, library.claude_home)
        mcp = list((capabilities or {}).get("mcp_servers") or [])
    elif agent is AgentKind.CODEX:
        native = await _codex_list(request, cwd)
    commands = menu(agent, library, native, hub=session_id is not None)
    return {"agent": agent.value, "commands": [c.to_json() for c in commands],
            "mcp_servers": mcp, "native_known": native is not None or agent is AgentKind.OLLAMA}


def _cwd(db: Session, session_id: str | None, path: str | None) -> Path:
    if session_id:
        agent_session = db.get(AgentSession, session_id)
        if agent_session is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
        try:
            return session_workdir(db, agent_session)
        except AgentUnavailable:
            return Path.home()
    if path and Path(path).is_dir():
        return Path(path)
    return Path.home()


async def _probe_claude(cwd: Path) -> dict[str, Any] | None:
    try:
        command = [find_claude()]
    except ClaudeCliNotFound:
        return None
    # A project folder may hold its own skills; otherwise an empty folder of its own.
    if cwd == Path.home():
        folder = Path(tempfile.mkdtemp(prefix="hub-skills-"))
        try:
            return await probe_claude(command, folder)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    return await probe_claude(command, cwd)


async def _codex_list(request: Request, cwd: Path) -> list[dict[str, Any]] | None:
    if not request.app.state.discover_native:
        return None
    try:
        command = [find_codex(get_settings().codex_path)]
    except CodexCliNotFound:
        return None
    return await codex_skills(command, cwd)
