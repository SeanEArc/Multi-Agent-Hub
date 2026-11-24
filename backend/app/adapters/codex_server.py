"""A small client for `codex app-server`, Codex's documented JSON-RPC protocol over stdio (revision 4).

Turns run through `codex exec`, but `exec` handles none of Codex's slash commands (they reach the model as text),
so the hub runs them through the app server on the same thread files. Verified with codex-cli 0.164.0:
- `initialize` then the `initialized` notification, then requests; replies carry the request's `id`,
  notifications a `method` and no `id`.
- `thread/resume {threadId, excludeTurns: true}` loads a thread `codex exec` started and then sends
  `thread/tokenUsage/updated` (`last.totalTokens` of `modelContextWindow` is the context in use), but only when the
  thread's last turn reported usage (a review turn doesn't).
- `thread/compact/start {threadId}` replies `{}` at once and runs as a turn: `turn/started`, a `contextCompaction`
  item, `turn/completed`. The token usage it reports is the compaction call's own, not the new context size.
- `review/start {threadId, target}` runs Codex's reviewer inline on the thread: `commandExecution` items, an
  `exitedReviewMode` item and a final `agentMessage` with the findings, then `turn/completed`.
- `account/rateLimits/read` returns the plan's windows (`primary` 300 min, `secondary` 10080 min). Its `accountId`
  is never kept. `mcpServerStatus/list` takes `detail` `full` or `toolsAndAuthOnly`.
- `account/read` returns the account's identity: the hub never calls it (`FORBIDDEN`).
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.adapters.cli import HEADLESS_GIT_ENV
from app.adapters.runner import Runner, RunningProcess

FORBIDDEN = {"account/read"}
REQUEST_TIMEOUT = 60.0


class AppServerError(RuntimeError):
    """An error reply, or the app server exiting or not answering."""


class AppServer:
    """One `codex app-server` process: `async with AppServer(...) as server`, then `request` and `notifications`."""

    def __init__(self, command: Sequence[str], cwd: Path, runner: Runner, config: Sequence[str] = (),
                 env: Mapping[str, str] | None = None) -> None:
        self._command = [*command, "app-server", *config]
        self._cwd = cwd
        self._runner = runner
        # Added to the environment: headless git, plus what the adapter's turns get (hub-doc on PATH, the outputs
        # folder), since a /code-review turn runs commands too.
        self._env = dict(env) if env is not None else dict(HEADLESS_GIT_ENV)
        self._proc: RunningProcess | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._notifications: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._next_id = 0

    async def __aenter__(self) -> "AppServer":
        self._proc = await self._runner.start(self._command, self._cwd, self._env)
        self._reader = asyncio.create_task(self._read())
        try:
            await self.request("initialize", {"clientInfo": {"name": "multi-agent-hub", "version": "1"}})
            await self._send({"method": "initialized"})
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            await self._proc.kill()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      timeout: float = REQUEST_TIMEOUT) -> dict[str, Any]:
        """Send a request and return its `result`; raises AppServerError on an error reply."""
        if method in FORBIDDEN:
            raise ValueError(f"The hub never calls {method}")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params or {}})
            async with asyncio.timeout(timeout):
                reply = await future
        except TimeoutError:
            raise AppServerError(f"Codex didn't answer {method} in time") from None
        finally:
            self._pending.pop(request_id, None)
        if "error" in reply:
            error = reply["error"] or {}
            raise AppServerError(error.get("message") or f"{method} failed")
        return reply.get("result") or {}

    async def notifications(self, timeout: float | None = None) -> AsyncIterator[dict[str, Any]]:
        """Notifications as they arrive, until the process exits (or nothing arrives for `timeout` seconds)."""
        while True:
            try:
                async with asyncio.timeout(timeout):
                    message = await self._notifications.get()
            except TimeoutError:
                return
            if message is None:
                return
            yield message

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._proc is not None
        await self._proc.write((json.dumps(message) + "\n").encode("utf-8"))

    async def _read(self) -> None:
        assert self._proc is not None
        try:
            async for line in self._proc.lines():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" in message:
                    # A request from Codex (an approval): the hub never approves anything through here.
                    await self._send({"id": message["id"], "error": {"code": -32601, "message": "Not supported"}})
                elif "id" in message:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        future.set_result(message)
                else:
                    await self._notifications.put(message)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(AppServerError("Codex's app server exited"))
            await self._notifications.put(None)
