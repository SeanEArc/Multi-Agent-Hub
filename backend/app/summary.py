"""The prompt for an agent-written session summary (the "Re-summarize" button).

The hub's own summary is built live in the UI from the events and `GET /summary`'s git
state, at no usage cost. This module gives an agent the same material, plus recent
messages and the diff, for a short written recap in a one-off call.
"""

from collections.abc import Sequence
from typing import Any

from app.adapters.base import EventType
from app.handoff import AGENT_NAMES, GitState, clip, files_and_commands
from app.models import PlanItemRead, SessionEvent, Stint, StintReason

SUMMARY_INSTRUCTIONS = (
    "You write short status summaries of coding sessions for Multi-Agent Hub, an app that runs "
    "coding agents on a user's projects. The user reads your summary when they come back to a "
    "session after a break, to see what it is about, what is done and which step it is on. "
    "Use only the material you are given; don't call tools.\n\n"
    "Format, in markdown, under 250 words:\n"
    "- First line: one plain sentence (no heading mark, under 120 characters) saying what the "
    "session is doing and where it stands.\n"
    "- Then these sections, each a few short bullets: **Goal**, **Done**, **Now** (the current "
    "step, or that the task is finished), **Next** (what remains, and any question waiting for "
    "the user).\n"
    "Name files and decisions concretely. Don't invent progress the material doesn't show."
)

MAX_PROMPT = 40_000
MAX_DIFF = 12_000
MAX_MESSAGE = 3_000
LAST_USER_MESSAGES = 10
LAST_AGENT_MESSAGES = 4
LAST_COMMANDS = 10
REASONS = {StintReason.LIMIT: "took over after a usage limit", StintReason.MANUAL: "handed over by the user"}
TURN_ENDS = {
    EventType.COMPLETED: "finished",
    EventType.QUESTION: "asked the user a question and is waiting for the answer",
    EventType.LIMIT_REACHED: "stopped at a usage limit",
    EventType.ERROR: "ended with an error",
}


def build_summary_prompt(
    title: str,
    events: Sequence[SessionEvent],
    stints: Sequence[Stint],
    plan: list[PlanItemRead] | None,
    git: GitState | None,
) -> str:
    user_messages = [e.payload.get("text", "") for e in events if e.type == EventType.USER_MESSAGE]
    agent_messages = [
        (e.agent, e.payload.get("text", ""))
        for e in events
        if e.type == EventType.MESSAGE and not e.payload.get("parent_tool_use_id")
    ]
    files, commands = files_and_commands(events)

    sections = [f"Summarize this session.\n\n# Session: {title}"]
    if user_messages:
        sections.append("## The task (the user's first message)\n\n" + clip(user_messages[0], MAX_MESSAGE))
    later = user_messages[1:][-LAST_USER_MESSAGES:]
    if later:
        sections.append("## Later messages from the user, oldest first\n\n" + "\n\n".join(
            f"- {clip(m, MAX_MESSAGE)}" for m in later
        ))
    sections.append("## Agents, in order\n\n" + "\n".join(
        f"- {AGENT_NAMES[s.agent]}" + (f" ({REASONS[s.reason]})" if s.reason in REASONS else "")
        for s in stints
    ))
    if plan:
        marks = {"completed": "[x]", "in_progress": "[ ] (in progress)", "pending": "[ ]"}
        sections.append("## The current agent's plan\n\n" + "\n".join(
            f"- {marks.get(item.status, '[ ]')} {item.text}" for item in plan
        ))
    if agent_messages:
        sections.append("## Latest messages from the agents, oldest first\n\n" + "\n\n---\n\n".join(
            f"{AGENT_NAMES[agent]}:\n\n{clip(text, MAX_MESSAGE)}" for agent, text in agent_messages[-LAST_AGENT_MESSAGES:]
        ))
    sections.append("## How the last turn ended\n\n" + _last_turn(events))
    if files:
        sections.append("## Files edited\n\n" + "\n".join(f"- {f}" for f in files))
    if commands:
        sections.append(f"## Last commands run (up to {LAST_COMMANDS})\n\n" + "\n".join(
            f"- `{clip(c, 200)}`" for c in commands[-LAST_COMMANDS:]
        ))
    if git is not None:
        parts = ["## Git", "`git status --short`:\n\n```\n" + (git.status.strip("\n") or "(clean)") + "\n```"]
        if git.diff_stat.strip():
            parts.append("Changes on this session's branch (`git diff --stat`):\n\n```\n" + git.diff_stat.strip() + "\n```")
        if git.diff.strip():
            diff = git.diff.strip()
            if len(diff) > MAX_DIFF:
                diff = diff[:MAX_DIFF] + "\n... (diff cut off)"
            parts.append("```diff\n" + diff + "\n```")
        sections.append("\n\n".join(parts))
    return clip("\n\n".join(sections), MAX_PROMPT)


def _last_turn(events: Sequence[SessionEvent]) -> str:
    for event in reversed(events):
        if event.type in TURN_ENDS:
            if event.type == EventType.ERROR and event.payload.get("stopped"):
                return "The user stopped it."
            return f"{AGENT_NAMES[event.agent]} {TURN_ENDS[EventType(event.type)]}."
        if event.type in (EventType.USER_MESSAGE, EventType.HANDOFF):
            return "A turn is still running."
    return "No turn has run yet."


def total_tokens(usage: dict[str, Any] | None) -> int | None:
    """Every token the call used. Codex's input_tokens already includes cached input; Claude's doesn't."""
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    return sum(int(usage.get(key) or 0) for key in keys)
