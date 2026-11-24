"""Codex's commands, run through its app server (revision 4): `/compact`, `/context`, `/usage`, `/review` (also
`/code-review`), `/status` and `/mcp`. Each streams the same `AgentEvent`s as a turn, ending in `COMPLETED`.

`codex exec` handles none of Codex's slash commands, so `CodexAdapter.run_command` comes here instead of starting a
turn. A command that needs a conversation runs on the session's thread; `/review` before there is one runs on a
throwaway (ephemeral) thread, so the waiting brief still goes with the first message.
"""

import asyncio
import time
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.adapters.base import AgentEvent, EventType
from app.adapters.codex_server import AppServer, AppServerError

if TYPE_CHECKING:
    from app.adapters.codex import CodexAdapter

COMMANDS = {"compact", "context", "usage", "review", "code-review", "status", "mcp"}
TURN_TIMEOUT = 1800.0  # a review of a large change can take a while
USAGE_WAIT = 2.0  # how long to wait for the token usage Codex sends after a resume
REVIEW_PROMPT = "Review the current code changes (staged, unstaged and untracked files) and provide prioritized findings."
WINDOW_LABELS = {300: "5-hour window", 10080: "Weekly window"}


async def run(adapter: "CodexAdapter", name: str, rest: str) -> AsyncGenerator[AgentEvent]:
    """Run `/name rest` for the adapter's session and stream its events."""
    if name not in COMMANDS:
        raise ValueError(f"Codex has no /{name} the hub can run")
    from app.adapters.codex import LIMIT_TEXT  # codex.py imports this module

    adapter._stopping = False
    try:
        async with AppServer(adapter._command, adapter.cwd, adapter.runner, adapter.server_config(),
                             adapter._env()) as server:
            adapter._proc = server._proc  # so Stop kills it, like a turn
            async for event in _COMMANDS[name](adapter, server, rest):
                yield event
    except AppServerError as exc:
        message = str(exc)
        if adapter._stopping:
            yield AgentEvent(EventType.ERROR, {"message": "Stopped by user", "stopped": True})
        elif LIMIT_TEXT.search(message):
            yield AgentEvent(EventType.LIMIT_REACHED, {"message": message})
        else:
            yield AgentEvent(EventType.ERROR, {"message": f"Codex couldn't run /{name}: {message}"})
    except OSError as exc:
        yield AgentEvent(EventType.ERROR, {"message": f"Codex couldn't run /{name}: {exc}"})
    finally:
        adapter._proc = None


# --- The commands --------------------------------------------------------------------------------


async def _compact(adapter: "CodexAdapter", server: AppServer, rest: str) -> AsyncGenerator[AgentEvent]:
    thread = _thread(adapter)
    await server.request("thread/resume", {"threadId": thread, "excludeTurns": True})
    before = await _token_usage(server, thread)
    await server.request("thread/compact/start", {"threadId": thread})
    failure = await _wait_for_turn(adapter, server, thread)
    if failure:
        yield failure
        return
    pre = ((before or {}).get("last") or {}).get("totalTokens")
    message = "Conversation compacted" + (f" · was {_tokens(pre)} tokens" if isinstance(pre, int) else "")
    if rest:
        message += " (Codex's compaction takes no focus text; it was ignored)"
    yield AgentEvent(EventType.NOTICE, {"message": message, "reason": "compacted", "trigger": "manual",
                                        "pre_tokens": pre, "post_tokens": None})
    yield AgentEvent(EventType.COMPLETED, {"text": ""})


async def _context(adapter: "CodexAdapter", server: AppServer, rest: str) -> AsyncGenerator[AgentEvent]:
    for event in _report(await _context_line(adapter, server)):
        yield event


async def _usage(adapter: "CodexAdapter", server: AppServer, rest: str) -> AsyncGenerator[AgentEvent]:
    limits = await server.request("account/rateLimits/read")
    snapshot = limits.get("rateLimits") or {}
    yield rate_limit_event_from(snapshot)
    for event in _report("## Codex usage\n\n" + _usage_lines(snapshot)):
        yield event


async def _status(adapter: "CodexAdapter", server: AppServer, rest: str) -> AsyncGenerator[AgentEvent]:
    lines = [
        "## Codex status\n",
        f"- **Model:** {adapter.model or 'Codex default'}",
        f"- **Reasoning effort:** {_effort_name(adapter.effort)}",
        f"- **Sandbox:** {adapter.sandbox}",
        f"- **Folder:** `{adapter.cwd}`",
        f"- **Thread:** `{adapter.native_session_id}`" if adapter.native_session_id else "- **Thread:** not started yet",
        f"- {await _context_line(adapter, server)}",
    ]
    snapshot = (await server.request("account/rateLimits/read")).get("rateLimits") or {}
    yield rate_limit_event_from(snapshot)
    for event in _report("\n".join(lines) + "\n\n**Usage**\n\n" + _usage_lines(snapshot)):
        yield event


async def _mcp(adapter: "CodexAdapter", server: AppServer, rest: str) -> AsyncGenerator[AgentEvent]:
    reply = await server.request("mcpServerStatus/list", {"detail": "toolsAndAuthOnly"})
    servers = reply.get("data") or []
    if not servers:
        text = "Codex has no MCP servers set up."
    else:
        rows = ["## Codex MCP servers\n", "| Server | Tools | Sign-in |", "|---|---|---|"]
        for entry in servers:
            tools = entry.get("tools") or {}
            auth = entry.get("authStatus") or "unknown"
            rows.append(f"| {entry.get('name')} | {len(tools)} | {AUTH_NAMES.get(auth, auth)} |")
        text = "\n".join(rows)
    for event in _report(text):
        yield event


async def _review(adapter: "CodexAdapter", server: AppServer, rest: str) -> AsyncGenerator[AgentEvent]:
    thread = adapter.native_session_id
    if thread:
        await server.request("thread/resume", {"threadId": thread, "excludeTurns": True})
    else:
        started = await server.request("thread/start", {"cwd": str(adapter.cwd), "ephemeral": True})
        thread = (started.get("thread") or {}).get("id")
        if not thread:
            raise AppServerError("Codex didn't start a thread for the review")
    if rest:
        target: dict[str, Any] = {"type": "custom", "instructions": f"{REVIEW_PROMPT}\n\n{rest}"}
    elif adapter.review_base:
        # A worktree session: everything on the session's branch since it began, committed or not.
        target = {"type": "baseBranch", "branch": adapter.review_base}
    else:
        target = {"type": "uncommittedChanges"}
    reply = await server.request("review/start", {"threadId": thread, "target": target})
    turn_id = (reply.get("turn") or {}).get("id")
    async for event in _review_events(adapter, server, thread, turn_id):
        yield event


_COMMANDS = {"compact": _compact, "context": _context, "usage": _usage, "status": _status, "mcp": _mcp,
             "review": _review, "code-review": _review}
AUTH_NAMES = {"unsupported": "not needed", "notLoggedIn": "not signed in", "bearerToken": "token",
              "oAuth": "signed in"}


# --- Helpers -------------------------------------------------------------------------------------


def _thread(adapter: "CodexAdapter") -> str:
    if not adapter.native_session_id:
        raise AppServerError("Codex hasn't started a conversation in this chat yet")
    return adapter.native_session_id


def _report(text: str) -> list[AgentEvent]:
    """A command's answer: a message row, then the end of the turn."""
    return [AgentEvent(EventType.MESSAGE, {"text": text, "parent_tool_use_id": None}),
            AgentEvent(EventType.COMPLETED, {"text": text})]


async def _token_usage(server: AppServer, thread: str) -> dict[str, Any] | None:
    """The `tokenUsage` Codex sends after a resume, if it sends one within `USAGE_WAIT` seconds."""
    deadline = time.monotonic() + USAGE_WAIT
    async for message in server.notifications(timeout=USAGE_WAIT):
        params = message.get("params") or {}
        if message.get("method") == "thread/tokenUsage/updated" and params.get("threadId") == thread:
            return params.get("tokenUsage")
        if time.monotonic() > deadline:
            break
    return None


async def _context_line(adapter: "CodexAdapter", server: AppServer) -> str:
    if not adapter.native_session_id:
        return "**Context:** Codex hasn't started a conversation in this chat yet."
    thread = adapter.native_session_id
    await server.request("thread/resume", {"threadId": thread, "excludeTurns": True})
    usage = await _token_usage(server, thread)
    used = ((usage or {}).get("last") or {}).get("totalTokens")
    window = (usage or {}).get("modelContextWindow")
    if not isinstance(used, int):
        return "**Context:** Codex hasn't reported its context use since the last turn; ask again after the next message."
    if isinstance(window, int) and window > 0:
        return f"**Context:** {_tokens(used)} of {_tokens(window)} tokens used ({round(used * 100 / window)}%)"
    return f"**Context:** {_tokens(used)} tokens used"


async def _wait_for_turn(adapter: "CodexAdapter", server: AppServer, thread: str) -> AgentEvent | None:
    """Wait for the thread's running turn to end; an `ERROR`/`LIMIT_REACHED` event if it failed, else None."""
    from app.adapters.codex import LIMIT_TEXT

    error: str | None = None
    async with asyncio.timeout(TURN_TIMEOUT):
        async for message in server.notifications():
            method, params = message.get("method"), message.get("params") or {}
            if params.get("threadId") not in (None, thread):
                continue
            if method == "error":
                error = ((params.get("error") or {}).get("message")) or error
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("status") == "completed":
                    return None
                error = ((turn.get("error") or {}).get("message")) or error or f"The turn ended {turn.get('status')}"
                break
    if adapter._stopping:
        return AgentEvent(EventType.ERROR, {"message": "Stopped by user", "stopped": True})
    message = error or "Codex's app server exited before finishing"
    return AgentEvent(EventType.LIMIT_REACHED if LIMIT_TEXT.search(message) else EventType.ERROR, {"message": message})


async def _review_events(adapter: "CodexAdapter", server: AppServer, thread: str, turn_id: str | None
                         ) -> AsyncGenerator[AgentEvent]:
    """The review turn's command rows and findings, in the shapes `codex exec` turns use."""
    from app.adapters.codex import LIMIT_TEXT, _unwrap_shell

    last_message = ""
    error: str | None = None
    started: set[str] = set()
    async with asyncio.timeout(TURN_TIMEOUT):
        async for message in server.notifications():
            method, params = message.get("method"), message.get("params") or {}
            if params.get("threadId") not in (None, thread):
                continue
            item = params.get("item") or {}
            kind, item_id = item.get("type"), f"review-{item.get('id')}"
            if method == "item/started" and kind == "commandExecution":
                started.add(item_id)
                yield _shell_use(item_id, _unwrap_shell(item.get("command") or ""))
            elif method == "item/completed" and kind == "commandExecution":
                if item_id not in started:
                    yield _shell_use(item_id, _unwrap_shell(item.get("command") or ""))
                code = item.get("exitCode")
                yield AgentEvent(EventType.TOOL_RESULT, {
                    "tool_use_id": item_id, "is_error": code not in (0, None) or item.get("status") == "failed",
                    "content": item.get("aggregatedOutput") or "", "exit_code": code, "parent_tool_use_id": None,
                })
            elif method == "item/completed" and kind == "agentMessage" and item.get("text"):
                last_message = item["text"]
                yield AgentEvent(EventType.MESSAGE, {"text": last_message, "parent_tool_use_id": None})
            elif method == "error":
                error = ((params.get("error") or {}).get("message")) or error
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if turn_id and turn.get("id") != turn_id:
                    continue  # an inner turn of the review
                if turn.get("status") == "completed":
                    yield AgentEvent(EventType.COMPLETED, {"text": last_message})
                    return
                error = ((turn.get("error") or {}).get("message")) or error or f"The review ended {turn.get('status')}"
                break
    if adapter._stopping:
        yield AgentEvent(EventType.ERROR, {"message": "Stopped by user", "stopped": True})
        return
    message = error or "Codex's app server exited before the review finished"
    yield AgentEvent(EventType.LIMIT_REACHED if LIMIT_TEXT.search(message) else EventType.ERROR, {"message": message})


def _shell_use(item_id: str, command: str) -> AgentEvent:
    return AgentEvent(EventType.TOOL_USE, {"tool_use_id": item_id, "name": "Shell", "input": {"command": command},
                                           "parent_tool_use_id": None})


def rate_limit_event_from(snapshot: dict[str, Any]) -> AgentEvent:
    """The app server's rate-limit snapshot as the hub's `RATE_LIMIT` event (the same shape as after a turn)."""
    from app.adapters.codex import rate_limit_event

    def window(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        return {"used_percent": raw.get("usedPercent"), "window_minutes": raw.get("windowDurationMins"),
                "resets_at": raw.get("resetsAt")}

    event = rate_limit_event({"primary": window(snapshot.get("primary")), "secondary": window(snapshot.get("secondary")),
                              "rate_limit_reached_type": snapshot.get("rateLimitReachedType"),
                              "plan_type": snapshot.get("planType")})
    event.raw = None  # the snapshot is shown as it came; nothing about the account is kept
    return event


def _usage_lines(snapshot: dict[str, Any]) -> str:
    lines = []
    for key in ("primary", "secondary"):
        raw = snapshot.get(key)
        if not isinstance(raw, dict) or not isinstance(raw.get("usedPercent"), int | float):
            continue
        minutes = raw.get("windowDurationMins")
        label = WINDOW_LABELS.get(minutes, f"{minutes}-minute window")
        resets = f", resets {_when(raw['resetsAt'])}" if isinstance(raw.get("resetsAt"), int) else ""
        lines.append(f"- **{label}:** {round(raw['usedPercent'])}% used{resets}")
    if snapshot.get("rateLimitReachedType"):
        lines.append("- **Limit reached:** Codex won't run until the window resets.")
    if snapshot.get("planType"):
        lines.append(f"- **Plan:** {snapshot['planType']}")
    return "\n".join(lines) or "Codex didn't report any usage windows."


def _when(epoch: int) -> str:
    moment = datetime.fromtimestamp(epoch)
    today = datetime.now().date()
    return moment.strftime("%H:%M") if moment.date() == today else moment.strftime("%a %d %b, %H:%M")


def _tokens(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


def _effort_name(effort: str | None) -> str:
    return {"low": "Light", "medium": "Medium", "high": "High", "xhigh": "Extra high"}.get(effort or "", "Codex default")
