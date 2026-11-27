"""Microsoft Office through PowerShell COM scripts (decided 2026-11-23: no pywin32), with a time limit and a cleanup
that kills only the Office processes automation started.

Used by the hub's Office check (`app.files.office`) and by `hub-doc docx pages|compare`. COM starts Word and Excel from
DCOM's launcher, not as children of the script, so ending the script's process tree never reaches them, and Excel
was seen still running 1.5 s after `Quit()` (probe 2026-11-21, section 4). So each run notes the automation
processes (`-Embedding` or `/Automation` in the command line) running before it starts, and afterwards waits a
moment for new ones to exit, then kills those that didn't. The user's own Word (no such flag) and automation that
was already running are never touched.
"""

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.doctool import winproc
from app.doctool.contract import Failed
from app.doctool.office_registry import OfficeLock, RunRecord, reap

OFFICE_IMAGES = {"WINWORD.EXE", "EXCEL.EXE", "POWERPNT.EXE"}
AUTOMATION = re.compile(r"(?:^|\s)[-/](?:embedding|automation)\b", re.IGNORECASE)
SCRIPT = Path(__file__).with_name("scripts") / "office.ps1"
# hub-doc's Word actions; agents are told to give their shell call more than this (Claude's Bash default is 2 min).
ACTION_TIMEOUT = 180.0
# How often a run notes the Office processes that appeared, for the registry.
POLL = 0.5
# How long Office may take to exit by itself after the script ends, before the cleanup kills it.
SETTLE = 3.0


def automation_processes() -> set[int]:
    """Office processes started by COM automation, right now."""
    return {pid for pid in winproc.processes(OFFICE_IMAGES) if _automation_line(pid)}


def is_automation(pid: int) -> bool:
    """Whether the PID is (still) an automation-started Office process."""
    return pid in winproc.processes(OFFICE_IMAGES) and _automation_line(pid)


def _automation_line(pid: int) -> bool:
    line = winproc.command_line(pid)
    return bool(line and AUTOMATION.search(line))


def script_command(action: str, **params: str) -> list[str]:
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
               str(SCRIPT), "-Action", action]
    for name, value in params.items():
        if value:
            command += [f"-{name}", value]
    return command


class OfficeRun:
    """One Office script run: start it, wait up to `timeout` seconds, then clean up. `cancel()` from another thread
    ends it early (the hub shutting down)."""

    def __init__(self, command: list[str], timeout: float, settle: float = SETTLE) -> None:
        self.command = command
        self.timeout = timeout
        self.settle = settle
        self.killed: list[int] = []
        self.reaped: list[int] = []
        self._proc: subprocess.Popen | None = None
        self._cancelled = threading.Event()

    def run(self) -> dict[str, Any]:
        """The script's JSON answer, or `{"ok": False, "error": ...}`. Never raises.

        Holds the PC-wide Office lock (`office_registry`) from the first look at running processes through the
        cleanup, so no other run's Word is ever mistaken for this one's; waiting for it counts against `timeout`."""
        deadline = time.monotonic() + self.timeout
        lock = OfficeLock()
        if not lock.acquire(deadline, self._cancelled):
            if self._cancelled.is_set():
                return {"ok": False, "error": "the Office check was stopped"}
            if not lock.usable:
                return {"ok": False, "lock_folder": True, "error": f"Office wasn't started: its lock folder {lock.path.parent} can't be "
                                              "used (set HUB_OFFICE_REGISTRY to a writable folder)"}
            return {"ok": False, "busy": True,
                    "error": f"Office stayed busy with another document for {self.timeout:g} seconds; try again"}
        try:
            self.reaped = reap(is_automation=is_automation)
            return self._run_locked(deadline)
        finally:
            lock.release()

    def _run_locked(self, deadline: float) -> dict[str, Any]:
        before = automation_processes()
        record = RunRecord()
        if self._cancelled.is_set():  # stopped before the script started (the hub shut down at once)
            return {"ok": False, "error": "the Office check was stopped"}
        try:
            self._proc = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            return {"ok": False, "error": f"the Office check couldn't start ({exc})"}
        timed_out = False
        try:
            while True:
                try:
                    stdout, stderr = self._proc.communicate(timeout=max(0.05, min(POLL, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    # Note the Office processes that appeared, in case this process is killed before its cleanup.
                    record.record(automation_processes() - before)
                    if self._cancelled.is_set():  # a cancel that came before the script process existed
                        self._kill_script()
                        stdout, stderr = _drain(self._proc)
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        self._kill_script()
                        stdout, stderr = _drain(self._proc)
                        break
        finally:
            self.killed = cleanup(before, 0 if self._cancelled.is_set() else self.settle)
            record.clear()
        if self._cancelled.is_set():
            return {"ok": False, "error": "the Office check was stopped"}
        if timed_out:
            return {"ok": False, "error": f"Office didn't answer within {self.timeout:g} seconds (it may be "
                                          "showing a dialog, or isn't activated)"}
        return parse_answer(stdout, stderr)

    def cancel(self) -> None:
        self._cancelled.set()
        self._kill_script()

    def _kill_script(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.poll() is None:
            proc.kill()


def _drain(proc: subprocess.Popen) -> tuple[bytes, bytes]:
    try:
        return proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, ValueError):
        return b"", b""


def cleanup(before: set[int], settle: float) -> list[int]:
    """Kill automation-started Office processes that weren't running `before`, after giving them `settle` seconds
    to exit by themselves. Returns the PIDs killed."""
    deadline = time.monotonic() + settle
    while True:
        new = automation_processes() - before
        if not new or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    for pid in sorted(new):
        winproc.kill(pid)
    return sorted(new)


def parse_answer(stdout: bytes, stderr: bytes) -> dict[str, Any]:
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                answer = json.loads(line)
            except ValueError:
                break
            if isinstance(answer, dict) and isinstance(answer.get("ok"), bool):
                return answer
            break
    shown = (stdout or stderr).decode("utf-8", "replace").strip()
    return {"ok": False, "error": f"unreadable answer from the Office check: {shown[:300] or 'nothing'}"}


# --- hub-doc's Office actions ------------------------------------------------------------------------------------


def run_action(command: Any, target: Path, outputs: Any, staging: Any) -> dict[str, Any]:
    """`docx pages` (Word's PDF, then PNG pages) or `docx compare` (Word's Compare), written to outputs."""
    files = [str(Path(name).resolve()) for name in command.files]
    if command.action == "pages":
        staged = staging.file(".pdf")
        answer = OfficeRun(script_command("pdf", In=files[0], Out=str(staged)), ACTION_TIMEOUT).run()
        _require(answer, staged)
        from app.doctool.cli import render_pages

        reply = render_pages(command, staged, outputs, staging)
        reply["pdf"] = str(staging.commit(staged, target))
        return reply
    staged = staging.file(".docx")
    author = (command.author or "Agent").strip()[:60] or "Agent"
    answer = OfficeRun(script_command("compare", In=files[0], Revised=files[1], Out=str(staged), Author=author),
                       ACTION_TIMEOUT).run()
    _require(answer, staged)
    return {"output": str(staging.commit(staged, target)), "revisions": answer.get("revisions"), "author": author}


def _require(answer: dict[str, Any], staged: Path) -> None:
    if answer.get("ok") and staged.is_file():
        return
    message = f"Microsoft Office couldn't do this: {answer.get('error') or 'it wrote no file'}."
    if not (answer.get("busy") or answer.get("lock_folder")):
        # Only a real Office failure points at the licence: a busy Office or an unusable lock folder isn't one.
        message += " This needs a licensed Office on this PC (Settings shows whether the hub found it)."
    raise Failed(message)
