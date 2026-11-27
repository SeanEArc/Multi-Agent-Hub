"""A test app with its Office check replaced, and Windows process checks for the Office tests."""

import ctypes
import inspect
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from app.main import create_app

from support.hub import TEST_TOKEN, fake_adapter_factory, opened_files


def office_app(engine: Engine, tmp: Path, command: list[str], timeout: float = 60):
    accepted = inspect.signature(create_app).parameters
    if "office_command" not in accepted or "office_timeout" not in accepted:
        pytest.fail("create_app has no office_command / office_timeout yet (revision 4 PR 2 Office detection)")
    return create_app(engine, fake_adapter_factory, token=TEST_TOKEN, worktrees_dir=tmp / "worktrees",
                      chats_dir=tmp / "chats", codex_home=tmp / "codex-home", skills_dir=tmp / "skills",
                      claude_home=tmp / "claude-home", discover_native=False, attachments_dir=tmp / "attachments",
                      open_file=opened_files.append_call, office_command=command, office_timeout=timeout)


SYNCHRONIZE = 0x00100000


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


WAIT_TIMEOUT = 0x102


def alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def running_office(exclude: Path) -> list[str]:
    """Real WINWORD.EXE/EXCEL.EXE processes (not this test's fakes), so the test never risks the user's documents."""
    script = ("Get-CimInstance Win32_Process -Filter \"Name='WINWORD.EXE' or Name='EXCEL.EXE'\" | "
              "ForEach-Object { $_.ExecutablePath }")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True, text=True,
                         timeout=60).stdout
    return [line for line in out.splitlines() if line.strip() and not line.lower().startswith(str(exclude).lower())]
