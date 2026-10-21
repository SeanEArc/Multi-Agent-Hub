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

import asyncio
import json
import os
import re
import shutil
import uuid
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Any

from app.adapters.base import AgentAdapter, AgentEvent, EventType
from app.models import AgentKind

NEEDS_INPUT_MARKER = "[[NEEDS_INPUT]]"
HEADLESS_PROMPT = (
    "You are running headless inside Multi-Agent Hub. The user cannot answer "
    "tool-based questions. When you need an answer or decision from the user "
    "before you can continue, ask in plain text and end your final message with "
    f"a line containing exactly: {NEEDS_INPUT_MARKER}"
)
# One stream-json line can hold a whole file or diff, far past asyncio's 64 KB default.
STREAM_LIMIT = 16 * 1024 * 1024
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


class ClaudeAdapter(AgentAdapter):
    kind = AgentKind.CLAUDE

    def __init__(
        self,
        cwd: Path,
        model: str | None = None,
        native_session_id: str | None = None,
        permission_mode: str = "acceptEdits",
        command: Sequence[str] | None = None,
    ) -> None:
        self.cwd = Path(cwd)
        self.model = model
        self.permission_mode = permission_mode
        self._session_id = native_session_id
        self._command = list(command) if command else [find_claude()]
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False

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

    async def stop(self) -> None:
        if self._proc is not None:
            self._stopping = True
            await _kill_tree(self._proc)

    async def _run(self, session_args: list[str], prompt: str) -> AsyncGenerator[AgentEvent]:
        if self._proc is not None:
            raise RuntimeError("A turn is already running")

        args = [
            *self._command,
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

        self._stopping = False
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
        self._proc = proc
        stderr_task = asyncio.create_task(proc.stderr.read())
        parser = TurnParser()
        try:
            # The prompt goes over stdin, so its length and characters never hit Windows quoting.
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            async for line in proc.stdout:
                for event in parser.feed(line):
                    yield event
            await proc.wait()
            stderr = (await stderr_task).decode("utf-8", "replace").strip()

            if self._stopping:
                yield AgentEvent(EventType.ERROR, {"message": "Stopped by user", "stopped": True})
            elif not parser.finished:
                yield AgentEvent(
                    EventType.ERROR,
                    {
                        "message": f"claude exited with code {proc.returncode} before finishing",
                        "stderr": stderr[-2000:],
                    },
                )
        finally:
            if proc.returncode is None:
                await _kill_tree(proc)
            stderr_task.cancel()
            self._proc = None


class TurnParser:
    """Turns Claude stream-json lines into shared AgentEvents for one turn."""

    def __init__(self) -> None:
        self.finished = False
        self._limit_reported = False

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
                text = block.get("text", "").replace(NEEDS_INPUT_MARKER, "").rstrip()
                if text:
                    events.append(AgentEvent(EventType.MESSAGE, {"text": text, "parent_tool_use_id": parent}))
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
        elif text.rstrip().endswith(NEEDS_INPUT_MARKER):
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


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the process and everything it started (Claude runs shells and subagents)."""
    if proc.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(proc.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        proc.kill()
    await proc.wait()
