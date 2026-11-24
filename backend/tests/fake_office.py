"""Stand-in for the hub's Office test conversion (revision 4, PR 2). Usage: python fake_office.py <mode> [args...]

The real check is a PowerShell COM script (probe 2026-11-21, rev 4 PR 2, section 4) that opens Word and Excel with
alerts off, converts a tiny document to PDF and prints ONE JSON line: `{"ok": true, "word": true, "excel": true,
"version": "16.0..."}`, or `{"ok": false, "error": "..."}` when Office is missing, unlicensed or fails. The hub
runs it with a time limit and afterwards kills only Office processes that automation started during the check.
This fake prints the same line, or misbehaves the ways the real thing can:

- `found`: Office works (one JSON line, exit 0).
- `missing`: Word isn't registered, as on a PC without Office (`ok: false`, exit 1).
- `garbage`: prints something that isn't the JSON line (a COM error dumped by PowerShell, say).
- `hang`: a dialog or activation prompt on a hidden instance: never answers.
- `leftover <office dir> <pid file>`: starts a fake `WINWORD.EXE` and `EXCEL.EXE` from `<office dir>` the way COM
  does (command line containing `/Automation -Embedding`, parent not the script: DCOM's launcher starts them, so
  a process-tree kill of the script never reaches them), writes their PIDs to `<pid file>`, then hangs like a stuck
  conversion. Real Excel was seen still running 1.5 s after `Quit()` (probe section 4).
- `found-leftover <office dir> <pid file>`: the same Office processes, but the script answers `ok` and exits
  (Office lingering after a successful check).
- `spawn <exe> <pid file>`: internal: starts one detached Office process and exits, so it is orphaned.

Extra arguments the hub adds after the mode are ignored.
"""

import json
import os
import subprocess
import sys
import time

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
SLEEPER = "import time; time.sleep(180)"


def answer(data: dict, code: int = 0) -> None:
    print(json.dumps(data), flush=True)
    sys.exit(code)


def start_detached(command: list[str]) -> subprocess.Popen:
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for extra in (CREATE_BREAKAWAY_FROM_JOB, 0):  # breakaway fails inside a job that doesn't allow it
        try:
            return subprocess.Popen(command, creationflags=flags | extra, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        except OSError:
            continue
    raise OSError("couldn't start " + command[0])


def leftovers(office_dir: str, pid_file: str) -> None:
    for exe in ("WINWORD.EXE", "EXCEL.EXE"):
        launcher = start_detached([sys.executable, __file__, "spawn", os.path.join(office_dir, exe), pid_file])
        launcher.wait(30)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # both PIDs written
        if os.path.exists(pid_file) and len(open(pid_file).read().split()) >= 2:
            return
        time.sleep(0.05)


mode = sys.argv[1]
if mode == "spawn":
    exe, pid_file = sys.argv[2], sys.argv[3]
    office = start_detached([exe, "-c", SLEEPER, "/Automation", "-Embedding"])
    with open(pid_file, "a") as out:
        out.write(f"{office.pid}\n")
    sys.exit(0)
if mode == "found":
    answer({"ok": True, "word": True, "excel": True, "version": "16.0.20527.20194", "seconds": 2.6})
if mode == "missing":
    answer({"ok": False, "word": False, "excel": False,
            "error": "Retrieving the COM class factory for component Word.Application failed: Class not registered"}, 1)
if mode == "garbage":
    print("New-Object : Retrieving the COM class factory failed due to the following error: 80080005", flush=True)
    sys.exit(1)
if mode == "hang":
    time.sleep(600)
if mode == "leftover":
    leftovers(sys.argv[2], sys.argv[3])
    time.sleep(600)
if mode == "found-leftover":
    leftovers(sys.argv[2], sys.argv[3])
    answer({"ok": True, "word": True, "excel": True, "version": "16.0.20527.20194", "seconds": 3.1})
print(f"fake office: unknown mode {mode}", file=sys.stderr)
sys.exit(5)
