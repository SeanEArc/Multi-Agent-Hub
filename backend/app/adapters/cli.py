"""Shared plumbing for adapters that run an agent CLI headless, one process per turn."""

import asyncio
import os
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Protocol

from app.adapters.base import AgentAdapter, AgentEvent, EventType

NEEDS_INPUT_MARKER = "[[NEEDS_INPUT]]"
HEADLESS_PROMPT = (
    "You are running headless inside Multi-Agent Hub. The user cannot answer "
    "tool-based questions. When you need an answer or decision from the user "
    "before you can continue, ask in plain text and end your final message with "
    f"a line containing exactly: {NEEDS_INPUT_MARKER}"
)
# One JSON line can hold a whole file or diff, far past asyncio's 64 KB default.
STREAM_LIMIT = 16 * 1024 * 1024
# Nobody can answer a git login prompt or credential window in a headless turn, so a
# missing credential must fail the command instead of hanging it.
HEADLESS_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


class LineParser(Protocol):
    """Turns one turn's JSONL output into AgentEvents; `finished` once the CLI reported the turn's end."""

    finished: bool

    def feed(self, line: bytes) -> list[AgentEvent]: ...


class CliAdapter(AgentAdapter):
    """Runs `command + args` per turn, sends the prompt over stdin and streams parsed events."""

    cli_name: str

    def __init__(self, cwd: Path, command: Sequence[str]) -> None:
        self.cwd = Path(cwd)
        self._command = list(command)
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False

    async def stop(self) -> None:
        if self._proc is not None:
            self._stopping = True
            await kill_tree(self._proc)

    def _on_event(self, event: AgentEvent) -> None:
        """Hook for adapters that learn state (such as the session id) from the stream."""

    async def _after_turn(self) -> list[AgentEvent]:
        """Hook for extra events once a turn has ended, such as state the CLI only writes to disk."""
        return []

    async def _stream(self, args: list[str], prompt: str, parser: LineParser) -> AsyncGenerator[AgentEvent]:
        if self._proc is not None:
            raise RuntimeError("A turn is already running")

        self._stopping = False
        proc = await asyncio.create_subprocess_exec(
            *self._command,
            *args,
            cwd=self.cwd,
            env={**os.environ, **HEADLESS_GIT_ENV},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
        self._proc = proc
        stderr_task = asyncio.create_task(proc.stderr.read())
        try:
            # The prompt goes over stdin, so its length and characters never hit Windows quoting.
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            async for line in proc.stdout:
                for event in parser.feed(line):
                    self._on_event(event)
                    yield event
            await proc.wait()
            stderr = (await stderr_task).decode("utf-8", "replace").strip()

            if self._stopping:
                yield AgentEvent(EventType.ERROR, {"message": "Stopped by user", "stopped": True})
            elif not parser.finished:
                yield AgentEvent(
                    EventType.ERROR,
                    {
                        "message": f"{self.cli_name} exited with code {proc.returncode} before finishing",
                        "stderr": stderr[-2000:],
                    },
                )
            else:
                for event in await self._after_turn():
                    yield event
        finally:
            if proc.returncode is None:
                await kill_tree(proc)
            stderr_task.cancel()
            self._proc = None


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the process and everything it started (agents run shells and subagents)."""
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
