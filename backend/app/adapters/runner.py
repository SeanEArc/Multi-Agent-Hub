"""Where an agent CLI runs: every launch goes through a Runner.

Adapters build the command line and parse the output; the runner starts the
process in a folder, carries the prompt and output, and kills it. Phase 4 ships
only the host runner (a process on this PC). Phase 9 adds a Docker runner behind
the same interface, so adapters, the orchestrator and handoff stay unchanged.
"""

import asyncio
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

# One JSON line can hold a whole file or diff, far past asyncio's 64 KB default.
STREAM_LIMIT = 16 * 1024 * 1024


class RunningProcess(ABC):
    """One started CLI process: stdin in, stdout lines and stderr out."""

    @property
    @abstractmethod
    def returncode(self) -> int | None:
        """The exit code, or None while it runs."""

    @abstractmethod
    async def send_input(self, data: bytes) -> None:
        """Write all of stdin and close it."""

    @abstractmethod
    def lines(self) -> AsyncIterator[bytes]:
        """stdout, one line at a time, until the process closes it."""

    @abstractmethod
    async def read_stderr(self) -> bytes:
        """All of stderr, once the process has closed it."""

    @abstractmethod
    async def wait(self) -> int:
        """Wait for exit and return the exit code."""

    @abstractmethod
    async def kill(self) -> None:
        """Kill the process and everything it started (agents run shells and subagents)."""


class Runner(ABC):
    @abstractmethod
    async def start(self, command: Sequence[str], cwd: Path, env: Mapping[str, str]) -> RunningProcess:
        """Start `command` in `cwd`, with `env` added to the runner's environment."""


class HostRunner(Runner):
    """Runs the CLI as a process on this PC, under the user's account and logins."""

    async def start(self, command: Sequence[str], cwd: Path, env: Mapping[str, str]) -> RunningProcess:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env={**os.environ, **env},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
        return HostProcess(proc)


class HostProcess(RunningProcess):
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def send_input(self, data: bytes) -> None:
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()
        self._proc.stdin.close()

    async def lines(self) -> AsyncIterator[bytes]:
        async for line in self._proc.stdout:
            yield line

    async def read_stderr(self) -> bytes:
        return await self._proc.stderr.read()

    async def wait(self) -> int:
        return await self._proc.wait()

    async def kill(self) -> None:
        proc = self._proc
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
