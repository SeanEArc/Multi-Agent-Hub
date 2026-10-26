"""The brief that lets the next agent continue a session's task, and the choice of that agent.

The hub writes the brief from saved events and the project's git state, so a
handoff costs no agent turns and works when the outgoing agent is out of usage.
"""

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.adapters.base import EventType
from app.adapters.cli import HEADLESS_GIT_ENV
from app.models import AgentKind, ChainEntry, SessionEvent, StintReason

MAX_BRIEF = 40_000
MAX_DIFF = 20_000
MAX_MESSAGE = 4_000
LATER_MESSAGES = 10
LAST_AGENT_MESSAGES = 3
LAST_COMMANDS = 20
GIT_TIMEOUT = 10.0

AGENT_NAMES = {AgentKind.CLAUDE: "Claude Code", AgentKind.CODEX: "Codex", AgentKind.OLLAMA: "a local model"}
REASON_TEXT = {
    StintReason.LIMIT: "it reached its usage limit",
    StintReason.MANUAL: "the user handed the session over to you",
}
# Tool names as the adapters report them: Claude's own, and Codex's mapped names (Shell, Edit).
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
COMMAND_TOOLS = {"Bash", "Shell", "PowerShell"}


@dataclass
class GitState:
    status: str
    diff_stat: str
    diff: str


async def git_state(path: Path, base: str | None = None) -> GitState | None:
    """`git status` and the diff against `base` (default HEAD), or None if the folder is not a git work tree.

    A worktree passes its base commit, so work the agents already committed on the branch is included.
    """
    inside = await _git(path, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != "true":
        return None
    status = await _git(path, "status", "--short")
    since = base or "HEAD"
    # A repository with no commits has no HEAD; fall back to the unstaged diff.
    diff_stat = await _git(path, "diff", "--stat", since) or await _git(path, "diff", "--stat") or ""
    diff = await _git(path, "diff", since) or await _git(path, "diff") or ""
    return GitState(status=status or "", diff_stat=diff_stat, diff=diff)


async def _git(path: Path, *args: str) -> str | None:
    """Output of a git command, or None if it failed, timed out or git is missing."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "core.quotepath=false", *args,
            cwd=path,
            env={**os.environ, **HEADLESS_GIT_ENV},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return stdout.decode("utf-8", "replace") if proc.returncode == 0 else None


def build_brief(
    events: Sequence[SessionEvent],
    previous_stint_id: int,
    from_agent: AgentKind,
    to_agent: AgentKind,
    reason: StintReason,
    git: GitState | None,
    note: str | None = None,
) -> str:
    """The first prompt for the new agent: the task so far, the last agent's state, and git."""
    user_messages = [e.payload.get("text", "") for e in events if e.type == EventType.USER_MESSAGE]
    previous = [e for e in events if e.stint_id == previous_stint_id]
    agent_messages = [e.payload.get("text", "") for e in previous if e.type == EventType.MESSAGE]
    files: dict[str, None] = {}  # ordered set
    commands: list[str] = []
    for event in events:
        if event.type != EventType.TOOL_USE:
            continue
        name, tool_input = event.payload.get("name"), event.payload.get("input") or {}
        if name in EDIT_TOOLS:
            for path in _edited_paths(tool_input):
                files[path] = None
        elif name in COMMAND_TOOLS and tool_input.get("command"):
            commands.append(str(tool_input["command"]))

    sections = [
        f"# Handoff from {AGENT_NAMES[from_agent]}\n\n"
        f"You are {AGENT_NAMES[to_agent]}, taking over a task in this project from another coding agent, "
        f"{AGENT_NAMES[from_agent]}, because {REASON_TEXT.get(reason, 'the session moved to you')}. "
        "You cannot see its conversation; this brief, written by Multi-Agent Hub from the session's "
        "history, is all you get. Check the current files before changing them, don't redo finished "
        "work, and continue from where it stopped."
    ]
    if user_messages:
        sections.append("## The task\n\n" + _clip(user_messages[0], MAX_MESSAGE))
    later = user_messages[1:][-LATER_MESSAGES:]
    if later:
        sections.append("## Later messages from the user, oldest first\n\n" + "\n\n".join(
            f"- {_clip(m, MAX_MESSAGE)}" for m in later
        ))
    if agent_messages:
        sections.append(f"## Last messages from {AGENT_NAMES[from_agent]}, oldest first\n\n" + "\n\n---\n\n".join(
            _clip(m, MAX_MESSAGE) for m in agent_messages[-LAST_AGENT_MESSAGES:]
        ))
    interrupted = _interrupted_message(previous)
    if interrupted:
        sections.append(
            f"## Unfinished turn\n\n{AGENT_NAMES[from_agent]} was cut off before finishing its reply to:\n\n"
            + _clip(interrupted, MAX_MESSAGE)
        )
    if files:
        sections.append("## Files edited in this session\n\n" + "\n".join(f"- {f}" for f in files))
    if commands:
        sections.append(f"## Last commands run (up to {LAST_COMMANDS})\n\n" + "\n".join(
            f"- `{_clip(c, 300)}`" for c in commands[-LAST_COMMANDS:]
        ))

    closing = (
        "## What to do now\n\n" + note.strip()
        if note and note.strip()
        else "## What to do now\n\nContinue the task from where the previous agent stopped."
    )
    head = "\n\n".join(sections)
    git_section = _git_section(git, MAX_BRIEF - len(head) - len(closing) - 200)
    return _clip("\n\n".join([head, git_section, closing]), MAX_BRIEF)


def next_in_chain(chain: Sequence[ChainEntry], current: AgentKind) -> list[ChainEntry]:
    """Agents to try after `current`, in order. An agent not in the chain is followed by the whole chain."""
    agents = [entry.agent for entry in chain]
    after = chain[agents.index(current) + 1:] if current in agents else chain
    return [entry for entry in after if entry.agent != current]


def _edited_paths(tool_input: dict) -> list[str]:
    if changes := tool_input.get("changes"):  # Codex file_change
        return [c.get("path") for c in changes if c.get("path")]
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    return [path] if path else []


def _interrupted_message(previous: Sequence[SessionEvent]) -> str | None:
    """The user message whose turn ended in a usage limit, if the last turn did."""
    last_user = None
    for event in previous:
        if event.type == EventType.USER_MESSAGE:
            last_user = event
        elif event.type == EventType.LIMIT_REACHED and last_user is not None:
            return last_user.payload.get("text")
        elif event.type in (EventType.COMPLETED, EventType.QUESTION):
            last_user = None
    return None


def _git_section(git: GitState | None, budget: int) -> str:
    if git is None:
        return "## Git\n\nThe project folder is not a git repository, so no diff is available."
    parts = ["## Git state"]
    # Only newlines are stripped: the leading space in " M file" is status's column for the index.
    parts.append("`git status --short`:\n\n```\n" + (git.status.strip("\n") or "(clean)") + "\n```")
    if git.diff_stat.strip():
        parts.append("`git diff --stat HEAD`:\n\n```\n" + git.diff_stat.strip() + "\n```")
    if git.diff.strip():
        room = max(0, min(MAX_DIFF, budget - sum(len(p) for p in parts) - 100))
        diff = git.diff.strip()
        if len(diff) > room:
            diff = diff[:room] + f"\n... (diff cut off; {len(git.diff):,} characters in full, run `git diff HEAD`)"
        parts.append("`git diff HEAD` (uncommitted changes; new untracked files are not included):\n\n```diff\n"
                     + diff + "\n```")
    return "\n\n".join(parts)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " ... (cut off)"
