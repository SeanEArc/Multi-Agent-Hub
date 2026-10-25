"""Codex adapter: runs the official `codex` CLI headless, one process per turn.

Behavior verified against codex-cli 0.164.0 (ChatGPT login, Windows):
- `codex exec --json -` reads the prompt from stdin and prints JSONL events:
  `thread.started`, `turn.started`, `item.started|updated|completed`, then
  `turn.completed` (with token usage) or `turn.failed`. A top-level `error`
  event can appear on its own. The usage on `turn.completed` is the thread's
  running total across all turns so far, not the turn's own tokens.
- Items are `agent_message`, `reasoning`, `command_execution`, `file_change`,
  `mcp_tool_call`, `web_search`, `todo_list` and `error`. Item ids restart at
  `item_0` every turn.
- The thread id is chosen by Codex and only known from `thread.started`. A reply is
  `codex exec resume <thread id> -`, which keeps the context and the same id.
- `resume` has no `--sandbox` or `--cd`, so the sandbox is set with `-c sandbox_mode`
  and the folder through the process cwd.
- In exec mode nothing waits for approval: a blocked action simply fails, e.g. a
  command with exit code 1 and "Access denied". There is no separate denied event.
- `workspace-write` also allows writes to %TEMP% unless
  `sandbox_workspace_write.exclude_tmpdir_env_var` is set.
- No rate-limit status is printed, so the only limit signal in the stream is the error
  text ("You've hit your usage limit...", taken from the binary; not seen live).
- The session file `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl`
  (resumes append to it) gets a `token_count` event per turn with the plan's usage
  windows (`primary` 300 min, `secondary` 10080 min). That file is Codex's internal
  format, so reading it is best effort: if it changes, no usage meter is shown.
"""

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

from app.adapters.base import AgentEvent, EventType
from app.adapters.cli import HEADLESS_PROMPT, NEEDS_INPUT_MARKER, CliAdapter
from app.models import AgentKind

LIMIT_TEXT = re.compile(
    r"usage limit|out of credits|usage_limit_reached|quota_exceeded|usage_not_included", re.IGNORECASE
)
# The npm package's `codex` launcher is a .cmd / .ps1 / node script; the native binary
# ships in a per-platform package next to it.
NPM_NATIVE_BINARIES = [
    Path("node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"),
    Path("node_modules/@openai/codex/node_modules/@openai/codex-win32-arm64/vendor/aarch64-pc-windows-msvc/bin/codex.exe"),
]
# Codex runs commands through PowerShell; show the command itself, not the wrapper.
POWERSHELL_WRAPPER = re.compile(r'^"?[^"]*powershell(?:\.exe)?"? -Command "(.*)"$', re.IGNORECASE | re.DOTALL)
TOOL_NAMES = {
    "command_execution": "Shell",
    "file_change": "Edit",
    "web_search": "WebSearch",
    "todo_list": "Plan",
}
# Usage windows by length in minutes, named like Claude's so the UI meter labels them.
WINDOW_NAMES = {300: "five_hour", 10080: "seven_day"}
# The latest token_count is near the end of the session file; a single line holds no file contents.
ROLLOUT_TAIL = 512 * 1024


class CodexCliNotFound(RuntimeError):
    pass


def find_codex(configured: str | None = None) -> str:
    """The native codex.exe: the configured path, a codex.exe on PATH, or the one inside the npm install."""
    if configured:
        if not Path(configured).is_file():
            raise CodexCliNotFound(f"HUB_CODEX_PATH points to {configured}, which does not exist.")
        return configured
    path = shutil.which("codex")
    if path is None:
        raise CodexCliNotFound("`codex` is not on PATH. Install the Codex CLI and log in.")
    if Path(path).suffix.lower() == ".exe":
        return path
    # Never run the npm launchers: .cmd re-parses arguments through cmd.exe.
    for relative in NPM_NATIVE_BINARIES:
        native = Path(path).parent / relative
        if native.is_file():
            return str(native)
    raise CodexCliNotFound(f"Found {path} but not the native codex.exe next to it; set HUB_CODEX_PATH.")


class CodexAdapter(CliAdapter):
    kind = AgentKind.CODEX
    cli_name = "codex"

    def __init__(
        self,
        cwd: Path,
        model: str | None = None,
        native_session_id: str | None = None,
        sandbox: str = "workspace-write",
        command: Sequence[str] | None = None,
        codex_home: Path | None = None,
    ) -> None:
        super().__init__(cwd, command or [find_codex()])
        self.model = model
        self.sandbox = sandbox
        self.codex_home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        self._session_id = native_session_id

    @property
    def native_session_id(self) -> str | None:
        return self._session_id

    def start_task(self, prompt: str) -> AsyncGenerator[AgentEvent]:
        if self._session_id is not None:
            raise RuntimeError("Task already started; use send_reply")
        return self._stream(["exec", "--json", *self._options(), "-"], prompt, TurnParser())

    def send_reply(self, reply: str) -> AsyncGenerator[AgentEvent]:
        if self._session_id is None:
            raise RuntimeError("No task started; use start_task")
        return self._stream(
            ["exec", "resume", "--json", *self._options(), self._session_id, "-"], reply, TurnParser()
        )

    def _options(self) -> list[str]:
        options = [
            # The user picked the folder in the hub; Codex's own git-repo guard would only block non-git folders.
            "--skip-git-repo-check",
            "-c", f"sandbox_mode={json.dumps(self.sandbox)}",
            "-c", 'approval_policy="never"',
            "-c", f"developer_instructions={json.dumps(HEADLESS_PROMPT)}",
        ]
        if self.model:
            options += ["--model", self.model]
        return options

    def _on_event(self, event: AgentEvent) -> None:
        if event.type is EventType.STARTED and event.data.get("session_id"):
            self._session_id = event.data["session_id"]

    async def _after_turn(self) -> list[AgentEvent]:
        if self._session_id is None:
            return []
        limits = await asyncio.to_thread(read_rate_limits, self.codex_home, self._session_id)
        return [rate_limit_event(limits)] if limits else []


def read_rate_limits(codex_home: Path, thread_id: str) -> dict[str, Any] | None:
    """The usage windows from the thread's latest token_count, or None if they can't be found."""
    files = sorted((codex_home / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    if not files:
        return None
    try:
        with files[-1].open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - ROLLOUT_TAIL))
            tail = f.read()
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if b'"token_count"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except (json.JSONDecodeError, AttributeError):
            continue  # the first line of the tail can be cut off
        if payload.get("type") == "token_count":
            limits = payload.get("rate_limits")
            return limits if isinstance(limits, dict) else None
    return None


def rate_limit_event(limits: dict[str, Any]) -> AgentEvent:
    """The same RATE_LIMIT shape the Claude adapter emits."""
    windows = {}
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if isinstance(window, dict) and isinstance(window.get("used_percent"), int | float):
            minutes = window.get("window_minutes")
            windows[WINDOW_NAMES.get(minutes, f"{minutes}m")] = {
                "utilization": window["used_percent"] / 100,
                "resets_at": window.get("resets_at"),
            }
    reached = limits.get("rate_limit_reached_type")
    return AgentEvent(
        EventType.RATE_LIMIT,
        {
            "status": "rejected" if reached else "allowed",
            "rate_limit_type": reached,
            "resets_at": (limits.get("primary") or {}).get("resets_at"),
            "windows": windows,
            "plan_type": limits.get("plan_type"),
        },
        raw=limits,
    )


class TurnParser:
    """Turns `codex exec --json` lines into shared AgentEvents for one turn."""

    def __init__(self) -> None:
        self.finished = False
        self._limit_reported = False
        self._last_message = ""
        self._started_items: set[str] = set()
        # Item ids repeat every turn, so tool ids get a per-turn prefix to stay unique in a session.
        self._turn = uuid.uuid4().hex[:8]
        self._began = time.monotonic()

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
        match raw.get("type"):
            case "thread.started":
                return [AgentEvent(EventType.STARTED, {"session_id": raw.get("thread_id")})]
            case "item.started":
                return self._item_started(raw.get("item") or {})
            case "item.completed":
                return self._item_completed(raw.get("item") or {})
            case "turn.completed":
                return self._turn_completed(raw)
            case "turn.failed":
                self.finished = True
                return self._failure((raw.get("error") or {}).get("message") or "Turn failed")
            case "error":
                return self._failure(raw.get("message") or "Unknown error")
        # turn.started, item.updated and anything new are not shown in the UI.
        return []

    def _item_started(self, item: dict[str, Any]) -> list[AgentEvent]:
        if item.get("type") not in (*TOOL_NAMES, "mcp_tool_call"):
            return []
        self._started_items.add(item.get("id", ""))
        return [self._tool_use(item)]

    def _item_completed(self, item: dict[str, Any]) -> list[AgentEvent]:
        match item.get("type"):
            case "agent_message":
                self._last_message = item.get("text") or ""
                text = self._last_message.replace(NEEDS_INPUT_MARKER, "").rstrip()
                return [AgentEvent(EventType.MESSAGE, {"text": text, "parent_tool_use_id": None})] if text else []
            case "error":
                return [AgentEvent(EventType.ERROR, {"message": item.get("message") or "Error"})]
            case kind if kind in TOOL_NAMES or kind == "mcp_tool_call":
                # Quick items can arrive completed without a started event.
                events = [] if item.get("id") in self._started_items else [self._tool_use(item)]
                return [*events, self._tool_result(item)]
        # reasoning is not shown, like Claude's thinking.
        return []

    def _tool_use(self, item: dict[str, Any]) -> AgentEvent:
        kind = item.get("type")
        if kind == "command_execution":
            name, tool_input = TOOL_NAMES[kind], {"command": _unwrap_shell(item.get("command") or "")}
        elif kind == "file_change":
            changes = item.get("changes") or []
            name, tool_input = TOOL_NAMES[kind], {"path": ", ".join(c.get("path", "") for c in changes), "changes": changes}
        elif kind == "web_search":
            name, tool_input = TOOL_NAMES[kind], {"query": item.get("query")}
        elif kind == "todo_list":
            name, tool_input = TOOL_NAMES[kind], {"items": item.get("items") or []}
        else:
            name, tool_input = f"mcp__{item.get('server')}__{item.get('tool')}", item.get("arguments") or {}
        return AgentEvent(
            EventType.TOOL_USE,
            {"tool_use_id": self._tool_id(item), "name": name, "input": tool_input, "parent_tool_use_id": None},
        )

    def _tool_result(self, item: dict[str, Any]) -> AgentEvent:
        kind = item.get("type")
        failed = item.get("status") == "failed"
        if kind == "command_execution":
            failed = failed or item.get("exit_code") not in (0, None)
            content = item.get("aggregated_output") or ""
        elif kind == "file_change":
            content = "\n".join(f"{c.get('kind')}: {c.get('path')}" for c in item.get("changes") or [])
        elif kind == "todo_list":
            content = "\n".join(
                f"[{'x' if i.get('completed') else ' '}] {i.get('text')}" for i in item.get("items") or []
            )
        elif kind == "mcp_tool_call":
            failed = failed or bool(item.get("error"))
            content = json.dumps(item.get("error") or item.get("result"), ensure_ascii=False)
        else:
            content = ""
        return AgentEvent(
            EventType.TOOL_RESULT,
            {"tool_use_id": self._tool_id(item), "is_error": failed, "content": content, "parent_tool_use_id": None},
        )

    def _tool_id(self, item: dict[str, Any]) -> str:
        return f"{self._turn}-{item.get('id')}"

    def _turn_completed(self, raw: dict[str, Any]) -> list[AgentEvent]:
        self.finished = True
        events = [
            AgentEvent(
                EventType.USAGE,
                {
                    "usage": raw.get("usage"),
                    # Codex reports the thread's running total, not this turn's tokens.
                    "cumulative": True,
                    "duration_ms": round((time.monotonic() - self._began) * 1000),
                },
            )
        ]
        text = self._last_message
        if text.rstrip().endswith(NEEDS_INPUT_MARKER):
            events.append(AgentEvent(EventType.QUESTION, {"text": text.replace(NEEDS_INPUT_MARKER, "").rstrip()}))
        else:
            events.append(AgentEvent(EventType.COMPLETED, {"text": text}))
        return events

    def _failure(self, message: str) -> list[AgentEvent]:
        if LIMIT_TEXT.search(message):
            # The same limit can arrive as an `error` event and again as `turn.failed`.
            if self._limit_reported:
                return []
            self._limit_reported = True
            return [AgentEvent(EventType.LIMIT_REACHED, {"message": message})]
        return [AgentEvent(EventType.ERROR, {"message": message})]


def _unwrap_shell(command: str) -> str:
    match = POWERSHELL_WRAPPER.match(command)
    return match.group(1) if match else command
