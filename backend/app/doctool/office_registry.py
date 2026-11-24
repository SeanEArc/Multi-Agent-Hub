"""One Office run at a time on the PC, and a record of the Office processes each run started.

**The lock.** Each run kills the automation Office processes that appeared during it, so two runs that overlap
(two sessions' `hub-doc docx pages`, or an agent's run and the hub's Office check) would kill each other's Word
(QA, 2026-11-23: the second run failed with RPC 0x800706BE). Every run, in hub-doc and in the hub, holds one
lock file from its first look at the running processes until its cleanup is done. Windows releases a file lock
when its process dies, so a killed run never leaves the lock taken. A waiting run counts the wait against its own
time limit.

**The registry.** When a run is killed midway (Stop, Claude's Bash timeout, the app closing), nobody runs its
cleanup, and the hidden Word it started would run until logoff: later runs see it as "already running" and leave
it alone. So while a run waits for its script, it writes the Office processes that appeared to
`run-<pid>-<token>.json`, with its own PID and creation time. The next run (inside the lock) and every Office
check kill the processes listed in files whose owner is gone, and only those, and only if each is still the same
process (PID and creation time) and still an automation-started Office process.

The folder is `HUB_OFFICE_REGISTRY`, by default `%TEMP%\\MultiAgentHub-office` (hub-doc runs inside Codex's
sandbox, where `%TEMP%` is writable and the hub's data folder is not). The hub passes its own resolved folder to
every agent (`DocumentAccess.env`), so all runs on the PC share one lock whatever each process's `TEMP` is. If the
folder can't be used, the run **fails closed**: Office isn't started, and the answer says the lock folder can't be
used, because a run without the lock could kill another run's Word.
"""

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from app.doctool import winproc

REGISTRY_ENV = "HUB_OFFICE_REGISTRY"
LOCK_NAME = "office.lock"


def folder() -> Path:
    return Path(os.environ.get(REGISTRY_ENV) or Path(tempfile.gettempdir()) / "MultiAgentHub-office")


class OfficeLock:
    """The PC-wide Office lock (a byte-range lock on a file). `acquire` waits until `deadline` (monotonic) or until
    `cancelled` is set."""

    def __init__(self, where: Path | None = None) -> None:
        self.path = (where or folder()) / LOCK_NAME
        self._file = None
        self.usable = True

    def acquire(self, deadline: float, cancelled: threading.Event) -> bool:
        if os.name != "nt":
            return True
        import msvcrt

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, "a+b")
        except OSError:
            # Fail closed: without the lock this run's cleanup could kill another run's Word (QA's B1). The caller
            # answers at once, naming the folder, rather than waiting or running unlocked.
            self.usable = False
            return False
        while True:
            try:
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if cancelled.is_set() or time.monotonic() >= deadline:
                    self._file.close()
                    self._file = None
                    return False
                time.sleep(0.2)

    def release(self) -> None:
        if self._file is None:
            return
        import msvcrt

        try:
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._file.close()
        self._file = None


class RunRecord:
    """This run's registry file: its owner (this process) and the Office processes that appeared during it."""

    def __init__(self, where: Path | None = None) -> None:
        self.path = (where or folder()) / f"run-{os.getpid()}-{secrets.token_hex(4)}.json"
        self.recorded: set[int] = set()

    def record(self, pids: set[int]) -> None:
        if pids == self.recorded:
            return
        entries = [{"pid": pid, "created": winproc.creation_time(pid)} for pid in sorted(pids)]
        data = {"owner": os.getpid(), "owner_created": winproc.creation_time(os.getpid()), "office": entries}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.path.with_suffix(".part")
            partial.write_text(json.dumps(data), encoding="utf-8")
            os.replace(partial, self.path)
            self.recorded = set(pids)
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _alive(pid: Any, created: Any) -> bool:
    return isinstance(pid, int) and created is not None and winproc.creation_time(pid) == created


def reap(where: Path | None = None, is_automation=None) -> list[int]:
    """Kill the Office processes recorded by runs whose owner is gone; delete their records. Returns PIDs killed.

    `is_automation(pid)` says whether a PID is still an automation-started Office process (app.doctool.office)."""
    killed: list[int] = []
    for record in sorted((where or folder()).glob("run-*.json")):
        try:
            raw = record.read_bytes()
        except OSError:
            continue  # held open or being replaced right now: left for a later run, never deleted unread
        try:
            data = json.loads(raw.decode("utf-8"))
            owner, owner_created = data["owner"], data["owner_created"]
            entries = list(data["office"])
        except (ValueError, KeyError, TypeError):
            _unlink(record)  # garbled: nothing in it can be trusted
            continue
        owner_running = (_alive(owner, owner_created) if owner_created is not None
                         else isinstance(owner, int) and winproc.creation_time(owner) is not None)
        if owner_running:
            continue  # its run is still going (or still cleaning up)
        for entry in entries:
            pid, created = (entry.get("pid"), entry.get("created")) if isinstance(entry, dict) else (None, None)
            if _alive(pid, created) and (is_automation is None or is_automation(pid)):
                winproc.kill(pid)
                killed.append(pid)
        _unlink(record)
    return killed


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
