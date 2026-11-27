"""Whether Microsoft Office works on this PC, for Word page images and Compare (revision 4, attachments PR 2).

Registry entries don't prove a licence, so the check is a real test conversion: a PowerShell COM script
(`app/doctool/scripts/office.ps1`) opens a tiny document read-only in hidden Word with alerts and macros off and
saves it as a PDF (and tries Excel the same way). It runs at startup in the background, never holding up the
backend, and again from Settings. A time limit ends a check stuck on a hidden dialog, and afterwards the Office
processes the check started are killed if they linger (`app.doctool.office`: only automation-started ones, never
the user's own Word). Any failure, a garbled answer or a timeout is "not found".

The result is kept in memory: a restart checks again.
"""

import asyncio
import logging
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.doctool.office import OfficeRun, script_command

log = logging.getLogger(__name__)

CHECK_TIMEOUT = 60.0  # the first Word start on a slow PC can take a while
FOUND, NOT_FOUND, CHECKING = "found", "not found", "checking"


class OfficeDetector:
    def __init__(self, command: Sequence[str] | None = None, timeout: float = CHECK_TIMEOUT,
                 enabled: bool = True) -> None:
        # Tests replace the script with a fake; the time limit and the cleanup still apply.
        self.command = list(command) if command else None
        self.timeout = timeout
        # HUB_OFFICE_CHECK=false: never start Office; it counts as not found.
        self.enabled = enabled
        self.status = CHECKING if enabled else NOT_FOUND
        self.version: str | None = None
        self.word = self.excel = False
        self.error: str | None = None if enabled else "The Office check is turned off (HUB_OFFICE_CHECK)."
        self.checked_at: float | None = None
        self._run: OfficeRun | None = None
        self._stopping = False  # set by shutdown: a check that hasn't made its run yet doesn't start one
        self._task: asyncio.Task[dict[str, Any]] | None = None

    @property
    def found(self) -> bool:
        return self.status == FOUND

    def snapshot(self) -> dict[str, Any]:
        return {"status": self.status, "version": self.version, "word": self.word, "excel": self.excel,
                "error": self.error, "checked_at": self.checked_at}

    def start(self) -> None:
        """Start a check in the background (at startup)."""
        if not self.enabled:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._check())

    async def check(self) -> dict[str, Any]:
        """Run a check now and answer once it has finished; joins a check that is already running."""
        if not self.enabled:
            return self.snapshot()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._check())
        return await asyncio.shield(self._task)

    async def shutdown(self) -> None:
        """Stop a running check (its process, and any Office it started), without waiting for a slow cleanup."""
        self._stopping = True
        run, task = self._run, self._task
        if run is not None:
            await asyncio.to_thread(run.cancel)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 5)
            except (TimeoutError, asyncio.CancelledError, Exception):
                pass

    async def _check(self) -> dict[str, Any]:
        previous = self.status
        self.status = CHECKING
        loop = asyncio.get_running_loop()
        done: asyncio.Future[dict[str, Any]] = loop.create_future()

        def work() -> None:
            try:
                answer = self._run_check()
            except Exception as exc:  # never let a check take the backend down
                log.exception("The Office check failed")
                answer = {"ok": False, "error": f"the check failed ({exc.__class__.__name__})"}
            if not loop.is_closed():
                loop.call_soon_threadsafe(_settle, done, answer)

        # A daemon thread, not the default executor: a check stuck in Office must never hold up the backend's exit.
        threading.Thread(target=work, name="office-check", daemon=True).start()
        answer = await done
        if answer.get("busy"):
            # Another run (an agent's Word job) held Office for the whole time limit: that says nothing about
            # whether Office works, so a "found" stays found (agents keep their Word commands) and only the note
            # changes.
            self.status = previous if previous in (FOUND, NOT_FOUND) else NOT_FOUND
            self.error = str(answer.get("error"))
            self.checked_at = time.time()
            return self.snapshot()
        self.status = FOUND if answer.get("ok") is True else NOT_FOUND
        self.version = str(answer["version"]) if answer.get("ok") and answer.get("version") else None
        self.word = bool(answer.get("ok") and answer.get("word", True))
        self.excel = bool(answer.get("ok") and answer.get("excel"))
        self.error = None if self.found else str(answer.get("error") or "Office didn't pass the test conversion")
        self.checked_at = time.time()
        log.info("Microsoft Office: %s%s", self.status, f" ({self.version})" if self.version else "")
        return self.snapshot()

    def _start(self, run: OfficeRun) -> dict[str, Any]:
        self._run = run
        if self._stopping:  # shutdown came before this run existed (QA's N9)
            run.cancel()
        return run.run()

    def _run_check(self) -> dict[str, Any]:
        if self.command is not None:
            return self._start(OfficeRun(self.command, self.timeout))
        with tempfile.TemporaryDirectory(prefix="hub-office-", ignore_cleanup_errors=True) as folder:
            sample = Path(folder) / "check.docx"
            _write_sample(sample)
            command = script_command("check", In=str(sample), Out=str(Path(folder) / "check.pdf"),
                                     ExcelOut=str(Path(folder) / "check-excel.pdf"))
            return self._start(OfficeRun(command, self.timeout))


def _settle(future: asyncio.Future, value: dict[str, Any]) -> None:
    if not future.done():
        future.set_result(value)


def _write_sample(path: Path) -> None:
    from docx import Document

    document = Document()
    document.add_paragraph("Multi-Agent Hub checks that Microsoft Word can save a PDF.")
    document.save(str(path))
