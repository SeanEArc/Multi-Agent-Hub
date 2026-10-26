"""Shared plumbing for adapters that run an agent CLI headless, one process per turn."""

import asyncio
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Protocol

from app.adapters.base import AgentAdapter, AgentEvent, EventType
from app.adapters.runner import HostRunner, Runner, RunningProcess

NEEDS_INPUT_MARKER = "[[NEEDS_INPUT]]"
HEADLESS_PROMPT = (
    "You are running headless inside Multi-Agent Hub. The user cannot answer "
    "tool-based questions. When you need an answer or decision from the user "
    "before you can continue, ask in plain text and end your final message with "
    f"a line containing exactly: {NEEDS_INPUT_MARKER}"
)
# Nobody can answer a git login prompt or credential window in a headless turn, so a
# missing credential must fail the command instead of hanging it.
HEADLESS_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


class LineParser(Protocol):
    """Turns one turn's JSONL output into AgentEvents; `finished` once the CLI reported the turn's end."""

    finished: bool

    def feed(self, line: bytes) -> list[AgentEvent]: ...


class CliAdapter(AgentAdapter):
    """Runs `command + args` per turn through its runner, sends the prompt over stdin and streams parsed events."""

    cli_name: str

    def __init__(self, cwd: Path, command: Sequence[str], runner: Runner | None = None) -> None:
        self.cwd = Path(cwd)
        self._command = list(command)
        self.runner = runner or HostRunner()
        self._proc: RunningProcess | None = None
        self._stopping = False

    async def stop(self) -> None:
        if self._proc is not None:
            self._stopping = True
            await self._proc.kill()

    def _on_event(self, event: AgentEvent) -> None:
        """Hook for adapters that learn state (such as the session id) from the stream."""

    async def _after_turn(self) -> list[AgentEvent]:
        """Hook for extra events once a turn has ended, such as state the CLI only writes to disk."""
        return []

    async def _stream(self, args: list[str], prompt: str, parser: LineParser) -> AsyncGenerator[AgentEvent]:
        if self._proc is not None:
            raise RuntimeError("A turn is already running")

        self._stopping = False
        proc = await self.runner.start([*self._command, *args], self.cwd, HEADLESS_GIT_ENV)
        self._proc = proc
        stderr_task = asyncio.create_task(proc.read_stderr())
        try:
            # The prompt goes over stdin, so its length and characters never hit Windows quoting.
            await proc.send_input(prompt.encode("utf-8"))

            async for line in proc.lines():
                for event in parser.feed(line):
                    self._on_event(event)
                    yield event
            returncode = await proc.wait()
            stderr = (await stderr_task).decode("utf-8", "replace").strip()

            if self._stopping:
                yield AgentEvent(EventType.ERROR, {"message": "Stopped by user", "stopped": True})
            elif not parser.finished:
                yield AgentEvent(
                    EventType.ERROR,
                    {
                        "message": f"{self.cli_name} exited with code {returncode} before finishing",
                        "stderr": stderr[-2000:],
                    },
                )
            else:
                for event in await self._after_turn():
                    yield event
        finally:
            if proc.returncode is None:
                await proc.kill()
            stderr_task.cancel()
            self._proc = None
