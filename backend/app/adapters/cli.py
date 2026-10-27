"""Shared plumbing for adapters that run an agent CLI headless, one process per turn."""

import asyncio
import re
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Protocol

from app.adapters.base import AgentAdapter, AgentEvent, EventType
from app.adapters.runner import HostRunner, Runner, RunningProcess

NEEDS_INPUT_MARKER = "[[NEEDS_INPUT]]"
# Neither CLI offers its own plan tool headless (Claude Code 2.1.310, codex-cli 0.164.0),
# so agents write the plan as a checklist under this marker and the hub reads it.
PLAN_MARKER = "[[PLAN]]"
HEADLESS_PROMPT = (
    "You are running headless inside Multi-Agent Hub. The user cannot answer "
    "tool-based questions. When you need an answer or decision from the user "
    "before you can continue, ask in plain text and end your final message with "
    f"a line containing exactly: {NEEDS_INPUT_MARKER}\n\n"
    "Plans are required: the hub shows the user your progress only through them. "
    "When a task has 3 or more steps, your first message, before any tool call, must be "
    f"a line containing exactly {PLAN_MARKER} followed by a markdown checklist, for example:\n"
    f"{PLAN_MARKER}\n- [ ] Read the existing code\n- [ ] Add the function\n- [ ] Run the tests\n\n"
    f"Each time you finish a step, write the whole checklist again under {PLAN_MARKER} "
    "with that step checked, before you start the next one. Only skip the plan when the "
    "task takes fewer than 3 steps."
)
_CHECKLIST_ITEM = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.+?)\s*$")
# Nobody can answer a git login prompt or credential window in a headless turn, so a
# missing credential must fail the command instead of hanging it.
HEADLESS_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


def plan_items(steps: Sequence[tuple[str, bool]]) -> list[dict[str, str]]:
    """(text, done) steps as plan items. The first unchecked step is in progress."""
    items, current = [], True
    for text, done in steps:
        status = "completed" if done else "in_progress" if current else "pending"
        items.append({"text": text, "status": status})
        current = current and done
    return items


def split_plan(text: str) -> tuple[str, list[dict[str, str]] | None]:
    """The text without PLAN_MARKER lines, and the checklist under the last marker (None if there is none)."""
    lines = text.splitlines()
    marks = [i for i, line in enumerate(lines) if line.strip() == PLAN_MARKER]
    if not marks:
        return text, None
    steps: list[tuple[str, bool]] = []
    for line in lines[marks[-1] + 1 :]:
        if match := _CHECKLIST_ITEM.match(line):
            steps.append((match[2], match[1] != " "))
        elif steps or line.strip():
            break
    kept = "\n".join(line for i, line in enumerate(lines) if i not in marks).strip()
    return kept, plan_items(steps) or None


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
