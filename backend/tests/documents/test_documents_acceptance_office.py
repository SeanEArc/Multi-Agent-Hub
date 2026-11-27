"""Acceptance tests for revision 4, attachments PR 2: Office detection, with Office absent.

Plan: PR 2 > "Office detection: at startup and from Settings, a test conversion with a time limit, alerts off, files
opened read-only, stuck processes killed; Settings shows "Microsoft Office: found / not found"", and Decided
2026-11-23 ("Office: through PowerShell COM scripts (no pywin32) ... Cleanup kills only Office processes started by
automation (-Embedding//Automation in the command line)").

Never real Office here: the check's script is replaced by `tests/support/fake_office.py` (the same one JSON line the real
script prints; see its docstring). Real Office is tested only in `test_office_live.py` (HUB_LIVE_OFFICE=1).

Seams assumed (one place each, see the report):
- `create_app(office_command=[...], office_timeout=seconds)`: the program the hub runs instead of its own Office
  check script, and the check's time limit. The hub itself (not the replaced script) applies the time limit and the
  cleanup, so both are tested here.
- `GET /api/office` -> {"status": "found" | "not found" | "checking", "version": str | None, ...};
  `POST /api/office/check` runs the check now and answers with the same shape once it has finished.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from support.hub import make_client
from support.office import alive, office_app
from support.paths import FAKE_OFFICE

OFFICE = "/api/office"


def fake(mode: str, *args: object) -> list[str]:
    return [sys.executable, str(FAKE_OFFICE), mode, *map(str, args)]


def status(client: TestClient) -> dict:
    response = client.get(OFFICE)
    assert response.status_code == 200, response.text
    return response.json()


def check(client: TestClient) -> dict:
    response = client.post(f"{OFFICE}/check")
    assert response.status_code == 200, response.text
    return response.json()


def wait_for(client: TestClient, wanted: str, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = status(client)
        if body["status"] == wanted:
            return body
        time.sleep(0.1)
    raise AssertionError(f"Office status stayed {body} instead of {wanted!r}")


@pytest.fixture
def tmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("office")


def test_found_and_reported(engine: Engine, tmp: Path) -> None:
    """Plan: PR 2 > Office detection ("Settings shows Microsoft Office: found")."""
    with make_client(office_app(engine, tmp, fake("found"))) as client:
        body = check(client)
        assert body["status"] == "found", body
        assert body["version"] == "16.0.20527.20194"
        assert status(client)["status"] == "found"


@pytest.mark.parametrize("mode", ["missing", "garbage"])
def test_not_found_when_the_check_fails(engine: Engine, tmp: Path, mode: str) -> None:
    """Plan: PR 2 > Office detection ("not found"); plan findings: unlicensed Office blocks saving and exporting, so
    Office counts only when the test conversion passes. Any failure or unreadable answer is "not found"."""
    with make_client(office_app(engine, tmp, fake(mode))) as client:
        assert check(client)["status"] == "not found"


def test_not_found_when_the_check_cant_even_start(engine: Engine, tmp: Path) -> None:
    """Plan: PR 2 > Office detection: a PC without PowerShell's COM bits (or a missing script) is just "not found",
    never an error page or a failed startup."""
    with make_client(office_app(engine, tmp, [str(tmp / "no-such-program.exe")])) as client:
        assert client.get("/api/health").status_code == 200
        assert check(client)["status"] == "not found"


def test_the_time_limit_ends_a_stuck_check(engine: Engine, tmp: Path) -> None:
    """Plan: PR 2 > Office detection ("a test conversion with a time limit"): a hidden dialog or activation prompt
    must not hang the check, and a stuck check is "not found"."""
    with make_client(office_app(engine, tmp, fake("hang"), timeout=2)) as client:
        started = time.monotonic()
        assert check(client)["status"] == "not found"
        assert time.monotonic() - started < 15


def test_startup_check_runs_in_the_background(engine: Engine, tmp: Path) -> None:
    """Plan: PR 2 > Office detection ("at startup"): the check starts by itself and never holds up the backend."""
    with make_client(office_app(engine, tmp, fake("hang"), timeout=30)) as client:
        started = time.monotonic()
        assert client.get("/api/health").status_code == 200
        assert time.monotonic() - started < 5  # the backend answers while the check is still running
        assert status(client)["status"] in ("checking", "not found")
    with make_client(office_app(engine, tmp, fake("found"))) as client:
        wait_for(client, "found")  # no POST: the startup check found it


# --- Only automation-started Office processes are killed ----------------------------------------------------------


def start(exe: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen([str(exe), "-c", "import time; time.sleep(180)", *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.mark.parametrize("mode", ["leftover", "found-leftover"])
def test_cleanup_kills_only_office_that_the_check_started(engine: Engine, tmp: Path, office_fakes: Path,
                                                          tmp_path: Path, mode: str) -> None:
    """Plan: Decided 2026-11-23 ("Cleanup kills only Office processes started by automation (-Embedding//Automation
    in the command line)") and PR 2 > Office detection ("stuck processes killed"). Probe s.4: Excel was still running
    1.5 s after Quit(); COM starts Office from DCOM's launcher, not as a child of the script, so killing the script's
    process tree never reaches it. The user's own Word (no -Embedding) and automation that was already running
    before the check must survive."""
    users_word = start(office_fakes / "WINWORD.EXE")
    earlier_automation = start(office_fakes / "EXCEL.EXE", "/Automation", "-Embedding")
    pid_file = tmp_path / "pids.txt"
    leftovers: list[int] = []
    try:
        time.sleep(0.5)
        with make_client(office_app(engine, tmp, fake(mode, office_fakes, pid_file), timeout=4)) as client:
            body = check(client)
            assert body["status"] == ("found" if mode == "found-leftover" else "not found"), body
            # The startup check may have run the same fake too: wait until no check is running.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and status(client)["status"] == "checking":
                time.sleep(0.1)
            leftovers = [int(pid) for pid in pid_file.read_text().split()]
            assert len(leftovers) >= 2
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(alive(pid) for pid in leftovers):
                time.sleep(0.1)
        assert not any(alive(pid) for pid in leftovers), "Office started by the check was left running"
        assert users_word.poll() is None, "the user's own Word was killed"
        assert earlier_automation.poll() is None, "Office automation from before the check was killed"
    finally:
        for process in (users_word, earlier_automation):
            process.kill()
        for pid in leftovers:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
