"""Claude Code adapter: runs the official `claude` CLI headless, one process per turn.

Behavior verified against Claude Code 2.1.305:
- `claude -p --output-format stream-json --verbose` prints one JSON event per line,
  ending with a `result` event.
- AskUserQuestion is unavailable in -p mode, so Claude asks in plain text and ends
  the turn. HEADLESS_PROMPT asks it to mark such turns with NEEDS_INPUT_MARKER.
- A reply is a new process with `--resume <session id>`.
- With `--permission-prompts none`, anything that needs approval is denied and
  reported as a `system/permission_denied` event instead of hanging the run.
- Every turn carries a `rate_limit_event` with usage-window status.
"""

import json
import re
import shutil
import uuid
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

from app.adapters.base import AgentEvent, EventType
from app.adapters.cli import HEADLESS_PROMPT, NEEDS_INPUT_MARKER, CliAdapter, split_plan
from app.adapters.runner import Runner
from app.models import AgentKind

# Wording of subscription-limit errors is not verified; rate_limit_event "rejected" is the primary signal.
LIMIT_TEXT = re.compile(r"usage limit|limit reached|out of (extra )?usage", re.IGNORECASE)


class ClaudeCliNotFound(RuntimeError):
    pass


def find_claude() -> str:
    path = shutil.which("claude")
    if path is None:
        raise ClaudeCliNotFound("`claude` is not on PATH. Install Claude Code and log in.")
    # npm installs a .cmd shim; batch files re-parse arguments through cmd.exe, which is
    # unsafe for prompt text. The native installer provides claude.exe.
    if Path(path).suffix.lower() in (".cmd", ".bat"):
        raise ClaudeCliNotFound(f"Found {path}; use the native Claude Code install (claude.exe).")
    return path


class ClaudeAdapter(CliAdapter):
    kind = AgentKind.CLAUDE
    cli_name = "claude"

    def __init__(
        self,
        cwd: Path,
        model: str | None = None,
        native_session_id: str | None = None,
        permission_mode: str = "acceptEdits",
        command: Sequence[str] | None = None,
        runner: Runner | None = None,
    ) -> None:
        super().__init__(cwd, command or [find_claude()], runner)
        self.model = model
        self.permission_mode = permission_mode
        self._session_id = native_session_id

    @property
    def native_session_id(self) -> str | None:
        return self._session_id

    def start_task(self, prompt: str) -> AsyncGenerator[AgentEvent]:
        if self._session_id is not None:
            raise RuntimeError("Task already started; use send_reply")
        self._session_id = str(uuid.uuid4())
        return self._run(["--session-id", self._session_id], prompt)

    def send_reply(self, reply: str) -> AsyncGenerator[AgentEvent]:
        if self._session_id is None:
            raise RuntimeError("No task started; use start_task")
        return self._run(["--resume", self._session_id], reply)

    def _run(self, session_args: list[str], prompt: str) -> AsyncGenerator[AgentEvent]:
        args = [
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            *session_args,
            "--permission-mode", self.permission_mode,
            "--permission-prompts", "none",
            "--append-system-prompt", HEADLESS_PROMPT,
        ]
        if self.model:
            args += ["--model", self.model]
        return self._stream(args, prompt, TurnParser())


class TurnParser:
    """Turns Claude stream-json lines into shared AgentEvents for one turn."""

    def __init__(self) -> None:
        self.finished = False
        self._limit_reported = False
        self._last_plan: list[dict[str, str]] | None = None

    def feed(self, line: bytes) -> list[AgentEvent]:
        text = line.decode("utf-8", "replace").strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return [AgentEvent(EventType.ERROR, {"message": "Unparseable output line", "line": text[:2000]})]
        events = self.parse(raw)
        if events:
            events[0].raw = raw
        return events

    def parse(self, raw: dict[str, Any]) -> list[AgentEvent]:
        match raw.get("type"), raw.get("subtype"):
            case "system", "init":
                return [
                    AgentEvent(
                        EventType.STARTED,
                        {
                            "session_id": raw.get("session_id"),
                            "model": raw.get("model"),
                            "permission_mode": raw.get("permissionMode"),
                            "cwd": raw.get("cwd"),
                        },
                    )
                ]
            case "system", "permission_denied":
                return [
                    AgentEvent(
                        EventType.PERMISSION_DENIED,
                        {
                            "tool_name": raw.get("tool_name"),
                            "tool_use_id": raw.get("tool_use_id"),
                            "reason": raw.get("decision_reason"),
                        },
                    )
                ]
            case "assistant", _:
                return self._assistant(raw)
            case "user", _:
                return self._tool_results(raw)
            case "rate_limit_event", _:
                return self._rate_limit(raw)
            case "result", _:
                return self._result(raw)
        # thinking_tokens, hook events and anything new are not shown in the UI.
        return []

    def _assistant(self, raw: dict[str, Any]) -> list[AgentEvent]:
        parent = raw.get("parent_tool_use_id")
        events = []
        for block in _content_blocks(raw):
            if block.get("type") == "text":
                text, plan = split_plan(block.get("text", "").replace(NEEDS_INPUT_MARKER, "").rstrip())
                if text:
                    events.append(AgentEvent(EventType.MESSAGE, {"text": text, "parent_tool_use_id": parent}))
                # The main agent's checklist under PLAN_MARKER, only when it changed.
                if plan and parent is None and plan != self._last_plan:
                    self._last_plan = plan
                    events.append(AgentEvent(EventType.PLAN, {"items": plan}))
            elif block.get("type") == "tool_use" and block.get("name") == "TodoWrite" and parent is None:
                # The to-do list, when the CLI offers it (2.1.310 headless doesn't): a plan event, not a tool row.
                todos = (block.get("input") or {}).get("todos") or []
                items = [{"text": t.get("content") or "", "status": t.get("status") or "pending"} for t in todos]
                events.append(AgentEvent(EventType.PLAN, {"items": items}))
            elif block.get("type") == "tool_use":
                events.append(
                    AgentEvent(
                        EventType.TOOL_USE,
                        {
                            "tool_use_id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input", {}),
                            "parent_tool_use_id": parent,
                        },
                    )
                )
        return events

    def _tool_results(self, raw: dict[str, Any]) -> list[AgentEvent]:
        return [
            AgentEvent(
                EventType.TOOL_RESULT,
                {
                    "tool_use_id": block.get("tool_use_id"),
                    "is_error": bool(block.get("is_error")),
                    "content": _text_of(block.get("content")),
                    "parent_tool_use_id": raw.get("parent_tool_use_id"),
                },
            )
            for block in _content_blocks(raw)
            if block.get("type") == "tool_result"
        ]

    def _rate_limit(self, raw: dict[str, Any]) -> list[AgentEvent]:
        info = raw.get("rate_limit_info") or {}
        data = {
            "status": info.get("status"),
            "rate_limit_type": info.get("rateLimitType"),
            "resets_at": info.get("resetsAt"),
            "windows": info.get("unifiedWindows"),
        }
        if info.get("status") == "rejected":
            self._limit_reported = True
            return [AgentEvent(EventType.LIMIT_REACHED, data)]
        return [AgentEvent(EventType.RATE_LIMIT, data)]

    def _result(self, raw: dict[str, Any]) -> list[AgentEvent]:
        self.finished = True
        text = raw.get("result") or ""
        events = [
            AgentEvent(
                EventType.USAGE,
                {
                    "cost_usd": raw.get("total_cost_usd"),
                    "num_turns": raw.get("num_turns"),
                    "duration_ms": raw.get("duration_ms"),
                    "usage": raw.get("usage"),
                },
            )
        ]

        if raw.get("is_error") or raw.get("subtype") != "success":
            if self._limit_reported:
                return events
            if LIMIT_TEXT.search(text):
                events.append(AgentEvent(EventType.LIMIT_REACHED, {"message": text}))
            else:
                events.append(
                    AgentEvent(
                        EventType.ERROR,
                        {"subtype": raw.get("subtype"), "message": text, "errors": raw.get("errors")},
                    )
                )
        elif (text := split_plan(text)[0]).rstrip().endswith(NEEDS_INPUT_MARKER):
            events.append(AgentEvent(EventType.QUESTION, {"text": text.replace(NEEDS_INPUT_MARKER, "").rstrip()}))
        else:
            events.append(
                AgentEvent(
                    EventType.COMPLETED,
                    {"text": text, "permission_denials": len(raw.get("permission_denials") or [])},
                )
            )
        return events


def _content_blocks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    message = raw.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""

