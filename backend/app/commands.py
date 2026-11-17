"""Agent commands (revision 4): what a message starting with `/name` does for each agent, and the `/` menu.

Each agent's own documented commands come first; the hub builds its own only where none exists.

- **Claude:** the Agent SDK sends a command as the prompt, and the `init` stream event's `slash_commands` lists
  every command that works without a terminal (built-ins, bundled skills such as `code-review`, the user's skills).
  Those are passed through unchanged, except the few the hub does with its own controls (`CLAUDE_HIDDEN`).
  Verified with Claude Code 2.1.323: `/compact [focus]` compacts the saved conversation (a `compact_boundary`
  event with the token counts); `/context`, `/usage` and `/cost` answer with no model request.
- **Codex** and the **local model:** `/compact` is still the hub's own (a fresh conversation from the hub's
  brief) until their documented versions are wired in.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.models import AgentKind
from app.skills import CLAUDE_PLUGIN, COMMAND_PATTERN, SkillError, SkillLibrary, read_skill

# The hub's own commands, run by `Orchestrator._run_hub_command` instead of sending the message
# (phase 7 adds /delegate, phase 8 /split). Claude has its own /compact.
HUB_COMMANDS: dict[str, str] = {
    "compact": "Start fresh from the hub's summary of the session; add text to keep something in focus",
}
HUB_COMMAND_AGENTS: dict[str, set[AgentKind]] = {"compact": {AgentKind.CODEX, AgentKind.OLLAMA}}

# Claude commands that answer from Claude Code itself, with no model request. Without a conversation yet (a new
# chat, or right after a handoff) they run as a one-off that isn't saved, so no empty conversation is left behind.
CLAUDE_LOCAL = {"context", "usage", "cost"}
# Listed in `slash_commands` but done with the hub's own controls, or not at all: `/clear` is redundant (a new chat
# does the same; the user's decision); the model, effort and speed are in the composer's picker; the title is the
# session's. Names starting with `__` are Claude Code's internals.
CLAUDE_HIDDEN: dict[str, str] = {
    "clear": "There's no /clear in the hub: start a new chat for a fresh conversation, or use /compact to shorten "
             "this one.",
    "model": "Change the model with the model picker beside Send.",
    "effort": "Change the reasoning effort with the model picker beside Send.",
    "fast": "Change the model or effort with the model picker beside Send.",
    "rename": "Rename the session from its title at the top of the chat.",
}
# Shown first in every agent's menu, so the commands the hub promises for all three are easy to find.
CORE_COMMANDS = ["compact", "context", "usage", "code-review"]
# Descriptions for Claude's built-in commands and bundled skills (`init` lists names only).
CLAUDE_DESCRIPTIONS: dict[str, str] = {
    "compact": "Summarize the conversation to free up context; add text to keep something in focus",
    "context": "Show how full the context window is",
    "usage": "Show your plan's usage",
    "cost": "Show your plan's usage",
    "code-review": "Review the current changes for bugs",
    "security-review": "Review the pending changes for security issues",
    "review": "Review a pull request",
    "ultrareview": "Deep multi-agent review in the cloud (billed)",
    "init": "Create a CLAUDE.md describing this project",
    "verify": "Check that a change works",
    "simplify": "Clean up the changed code",
    "debug": "Debug a problem step by step",
    "batch": "Run a change across many files",
    "loop": "Run a prompt repeatedly",
    "schedule": "Schedule a recurring agent",
    "insights": "Report on your Claude Code sessions",
    "recap": "Recap this conversation",
    "goal": "Set a goal Claude keeps working toward",
    "mcp": "Manage MCP servers",
    "config": "Change Claude Code's settings",
    "output-style": "Change the output style",
    "agents": "Manage subagents",
    "import": "Import from another tool",
    "autocompact": "Set when Claude compacts on its own",
}
CLAUDE_FALLBACK = ["compact", "context", "usage", "code-review"]  # before any `init` event was recorded


@dataclass
class Command:
    name: str
    description: str
    kind: str  # "skill" (the agent's own), "library", "builtin" (the agent's command) or "hub"

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description, "kind": self.kind}


@dataclass(frozen=True)
class Route:
    """How the current agent runs a `/name` message.

    `notice`: the hub answers with `message` and nothing is sent. `agent`: the message is sent as typed, never
    behind a brief; `local` when the agent answers it without a model request; `compact` for a compaction.
    """

    action: Literal["notice", "agent"]
    name: str
    message: str = ""
    local: bool = False

    @property
    def compact(self) -> bool:
        return self.name == "compact"


def parse(text: str) -> tuple[str, str] | None:
    """`(name, rest)` when the message starts with `/name`, else None."""
    match = COMMAND_PATTERN.match(text.strip())
    if not match:
        return None
    return match.group(1), (match.group(2) or "").strip()


def hub_command(agent: AgentKind, text: str) -> tuple[str, str] | None:
    """`(name, rest)` when the message is one of the hub's own commands for `agent`, else None."""
    parsed = parse(text)
    if not parsed or agent not in HUB_COMMAND_AGENTS.get(parsed[0], set()):
        return None
    return parsed


def claude_commands(capabilities: dict[str, Any] | None) -> list[str]:
    """Claude's commands that work headless (built-ins and skills) and that the hub passes through."""
    if not capabilities or not capabilities.get("slash_commands"):
        return list(CLAUDE_FALLBACK)
    terminal = set(capabilities.get("terminal_slash_commands") or [])
    return [name for name in capabilities["slash_commands"]
            if name not in terminal and name not in CLAUDE_HIDDEN and not name.startswith("__")
            and not name.startswith(f"{CLAUDE_PLUGIN}:")]


def route(agent: AgentKind, text: str, capabilities: dict[str, Any] | None = None) -> Route | None:
    """How `agent` runs the message, or None when it isn't one of the agent's commands (it is then sent as a
    normal message, with library skills expanded by `expand_command`)."""
    parsed = parse(text)
    if parsed is None or agent is not AgentKind.CLAUDE:
        return None
    name = parsed[0]
    if name in CLAUDE_HIDDEN:
        return Route("notice", name, CLAUDE_HIDDEN[name])
    if name in set((capabilities or {}).get("terminal_slash_commands") or []):
        return Route("notice", name, f"/{name} only works in Claude Code's own terminal.")
    if name in claude_commands(capabilities):
        return Route("agent", name, local=name in CLAUDE_LOCAL)
    return None


def menu(agent: AgentKind, library: SkillLibrary, native: Sequence[dict[str, Any]] | None,
         hub: bool = True, capabilities: dict[str, Any] | None = None) -> list[Command]:
    """The `/` menu for `agent`, each name once: the commands every agent has first, then the hub's own, the
    agent's own skills (`native`, names and descriptions), library skills switched on for it, and the rest of its
    commands (Claude's from its recorded `init` event, `capabilities`). `hub` is false for a new chat, which has
    nothing to compact yet."""
    commands: list[Command] = []
    seen: set[str] = set()

    def add(name: str, description: str, kind: str) -> None:
        if name and name not in seen:
            seen.add(name)
            commands.append(Command(name, description, kind))

    native_names = {str(s.get("name") or "") for s in native or []}
    claude = claude_commands(capabilities) if agent is AgentKind.CLAUDE else []
    for name in CORE_COMMANDS:
        if name == "compact" and not hub:
            continue
        if name in claude:
            add(name, CLAUDE_DESCRIPTIONS.get(name, ""), "skill" if name in native_names else "builtin")
        elif hub and agent in HUB_COMMAND_AGENTS.get(name, set()):
            add(name, HUB_COMMANDS[name], "hub")
    if hub:
        for name, description in HUB_COMMANDS.items():
            if agent in HUB_COMMAND_AGENTS.get(name, set()):
                add(name, description, "hub")
    for skill in native or []:
        name = str(skill.get("name") or "")
        if agent is AgentKind.CLAUDE and name.startswith(f"{CLAUDE_PLUGIN}:"):
            continue  # the library's own plugin, listed below under its plain name
        add(name, str(skill.get("description") or "") or CLAUDE_DESCRIPTIONS.get(name, ""), "skill")
    for skill in library.for_agent(agent):
        try:
            add(skill.name, read_skill(skill.path).description, "library")
        except SkillError:
            continue
    for name in claude:
        if hub or name != "compact":
            add(name, CLAUDE_DESCRIPTIONS.get(name, ""), "builtin")
    return commands
