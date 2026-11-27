"""hub-doc's Office runs: one at a time on the PC (`app.doctool.office_registry` lock), the registry that reaps
Office left by a killed run, and the lock folder (passed to every agent; unusable means fail closed).

Never real Office: the "Office" here is `tests/support/fake_office.py` or a recording stand-in script, and every test
uses its own registry folder (`HUB_OFFICE_REGISTRY`), so the lock is never shared with the user's real runs.
"""

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.doctool import office, office_registry, winproc
from app.doctool.contract import Failed
from app.doctool.office import OfficeRun, is_automation
from app.files.documents import DocumentAccess

from support import document_samples as samples
from support.documents import Held, HubDoc, make_session
from support.office import alive, office_app
from support.paths import BACKEND, FAKE_OFFICE

WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows file locks, junctions and processes")


def fake(mode: str, *args: object) -> list[str]:
    return [sys.executable, str(FAKE_OFFICE), mode, *map(str, args)]


def pids(path: Path) -> set[int]:
    return {int(p) for p in path.read_text().split()} if path.exists() else set()


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "office-registry"
    monkeypatch.setenv(office_registry.REGISTRY_ENV, str(folder))
    return folder


def kill_all(*groups: set[int]) -> None:
    for group in groups:
        for pid in group:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


@windows_only
def test_overlapping_runs_never_kill_each_others_office(office_fakes: Path, registry: Path, tmp_path: Path) -> None:
    """QA's B1: run A's cleanup killed run B's Word. With the lock, B starts only after A's cleanup, so each run
    kills exactly the Office it started."""
    a_file, b_file = tmp_path / "a.txt", tmp_path / "b.txt"
    a = OfficeRun(fake("found-leftover", office_fakes, a_file), timeout=60)
    b = OfficeRun(fake("found-leftover", office_fakes, b_file), timeout=60)
    answers: dict[str, dict] = {}
    first = threading.Thread(target=lambda: answers.setdefault("a", a.run()))
    second = threading.Thread(target=lambda: answers.setdefault("b", b.run()))
    try:
        first.start()
        time.sleep(0.5)
        second.start()
        first.join(60)
        second.join(60)
        assert answers["a"]["ok"] and answers["b"]["ok"], answers
        assert set(a.killed) == pids(a_file) and len(a.killed) == 2
        assert set(b.killed) == pids(b_file) and len(b.killed) == 2
        deadline = time.monotonic() + 10  # a kill isn't instant
        while time.monotonic() < deadline and any(alive(pid) for pid in pids(a_file) | pids(b_file)):
            time.sleep(0.1)
        assert not any(alive(pid) for pid in pids(a_file) | pids(b_file))
    finally:
        kill_all(pids(a_file), pids(b_file))


@windows_only
def test_waiting_for_the_lock_counts_against_the_time_limit(registry: Path) -> None:
    stuck = OfficeRun(fake("hang"), timeout=4)
    holder = threading.Thread(target=stuck.run)
    holder.start()
    try:
        time.sleep(0.5)
        started = time.monotonic()
        answer = OfficeRun(fake("found"), timeout=1).run()
        assert answer["ok"] is False and answer.get("busy") and "busy" in answer["error"]
        assert time.monotonic() - started < 3
    finally:
        stuck.cancel()
        holder.join(30)
    assert OfficeRun(fake("found"), timeout=20).run()["ok"]  # the lock is free again


@windows_only
def test_a_killed_process_never_keeps_the_lock(registry: Path) -> None:
    script = textwrap.dedent(f"""
        import os, sys, threading, time
        sys.path.insert(0, {str(BACKEND)!r})
        os.environ[{office_registry.REGISTRY_ENV!r}] = {str(registry)!r}
        from app.doctool.office_registry import OfficeLock
        lock = OfficeLock()  # kept: closing the file (garbage collection) releases the lock
        lock.acquire(time.monotonic() + 5, threading.Event())
        print("locked", flush=True)
        time.sleep(60)
    """)
    holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert OfficeRun(fake("found"), timeout=1).run().get("busy")
    finally:
        holder.kill()
        holder.wait()
    assert OfficeRun(fake("found"), timeout=10).run()["ok"]


# A stand-in Office script that logs when it starts and ends, so tests can see whether two runs overlapped.
TIMED = r'''
import json, sys, time
log, seconds = sys.argv[1], float(sys.argv[2])
with open(log, "a") as out:
    out.write(f"start {time.time()}\n")
time.sleep(seconds)
with open(log, "a") as out:
    out.write(f"end {time.time()}\n")
print(json.dumps({"ok": True, "word": True, "excel": True, "version": "16.0"}), flush=True)
'''


def timed_script(tmp_path: Path, seconds: float) -> tuple[list[str], Path]:
    script = tmp_path / "timed_office.py"
    script.write_text(TIMED, encoding="utf-8")
    log = tmp_path / "office-runs.log"
    return [sys.executable, str(script), str(log), str(seconds)], log


def intervals(log: Path) -> list[tuple[float, float]]:
    starts, ends = [], []
    for line in log.read_text().splitlines() if log.exists() else []:
        kind, stamp = line.split()
        (starts if kind == "start" else ends).append(float(stamp))
    return list(zip(sorted(starts), sorted(ends)))


@windows_only
def test_many_waiters_take_turns_and_never_overlap(registry: Path, tmp_path: Path) -> None:
    """Six runs asking at once (several agents' Word jobs plus the hub's check): each waits its turn, none starts its
    script while another's runs (the condition B1 depends on), and all finish."""
    command, log = timed_script(tmp_path, 0.3)
    with ThreadPoolExecutor(6) as pool:
        answers = list(pool.map(lambda _: OfficeRun(command, timeout=60, settle=0).run(), range(6)))
    assert all(answer["ok"] for answer in answers), answers
    runs = intervals(log)
    assert len(runs) == 6
    for (_, end), (start, _) in zip(runs, runs[1:]):
        assert start >= end, f"two Office runs overlapped: {runs}"


@windows_only
def test_waiters_each_keep_their_own_time_limit(registry: Path) -> None:
    """A long Word job holds Office. Short waiters each give up at their own limit with `busy: true` (none waits for
    the job to end), and the job itself is unaffected."""
    holder = OfficeRun(fake("hang"), timeout=6)
    holding = threading.Thread(target=holder.run)
    holding.start()
    try:
        time.sleep(0.5)

        def wait(limit: float) -> tuple[float, dict]:
            started = time.monotonic()
            answer = OfficeRun(fake("found"), timeout=limit).run()
            return time.monotonic() - started, answer

        with ThreadPoolExecutor(5) as pool:
            results = list(pool.map(wait, [0.5, 1, 1, 1.5, 2]))
        for (took, answer), limit in zip(results, [0.5, 1, 1, 1.5, 2]):
            assert answer["ok"] is False and answer.get("busy") is True, answer
            assert limit - 0.1 <= took < limit + 1.5, (limit, took)
    finally:
        holder.cancel()
        holding.join(30)
    assert OfficeRun(fake("found"), timeout=20).run()["ok"]


@windows_only
def test_a_garbled_lock_file_still_locks(registry: Path) -> None:
    """The lock is a byte range, not the file's contents: garbage in office.lock changes nothing."""
    registry.mkdir()
    (registry / office_registry.LOCK_NAME).write_bytes(os.urandom(4096))
    assert OfficeRun(fake("found"), timeout=20).run()["ok"]


@windows_only
def test_the_lock_file_cant_be_deleted_from_under_a_run(registry: Path) -> None:
    """If deleting office.lock while it's held worked, the next run would make a new file, lock it and overlap.
    While a run holds it the file can't be removed, and another run is told Office is busy."""
    holder = OfficeRun(fake("hang"), timeout=6)
    holding = threading.Thread(target=holder.run)
    holding.start()
    try:
        lock_file = registry / office_registry.LOCK_NAME
        deadline = time.monotonic() + 10
        while not lock_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        with pytest.raises(PermissionError):
            lock_file.unlink()
        assert OfficeRun(fake("found"), timeout=1).run().get("busy") is True
    finally:
        holder.cancel()
        holding.join(30)
    lock_file.unlink()  # free again: deleting it then is harmless
    assert OfficeRun(fake("found"), timeout=20).run()["ok"]


@windows_only
@pytest.mark.parametrize("how", ["a file in the way", "a folder that can't be written"])
def test_an_unusable_lock_folder_fails_closed_at_once(tmp_path: Path, monkeypatch, how: str) -> None:
    """Without the lock, a run's cleanup could kill another run's Word. If the folder can't be used the run doesn't
    start Office at all, answers within a moment (no wait, no hang) and names the folder."""
    command, log = timed_script(tmp_path, 0.1)
    if how == "a file in the way":
        blocker = tmp_path / "not-a-folder"
        blocker.write_text("x")
        folder = blocker / "office"
        undo = None
    else:
        folder = tmp_path / "locked-registry"
        folder.mkdir()
        user = os.environ.get("USERNAME", "")
        denied = subprocess.run(["icacls", str(folder), "/deny", f"{user}:(OI)(CI)(W,AD)"], capture_output=True)
        if denied.returncode != 0:
            pytest.skip("couldn't change the folder's permissions")
        undo = ["icacls", str(folder), "/remove:d", user]
    monkeypatch.setenv(office_registry.REGISTRY_ENV, str(folder))
    try:
        started = time.monotonic()
        answer = OfficeRun(command, timeout=30).run()
        assert time.monotonic() - started < 3
        assert answer["ok"] is False and "HUB_OFFICE_REGISTRY" in answer["error"], answer
        assert not log.exists(), "Office was started without the lock"
    finally:
        if undo:
            subprocess.run(undo, capture_output=True)


@windows_only
def test_the_hubs_check_and_a_word_job_contend_without_losing_found(engine, tmp_path: Path, registry: Path) -> None:
    """The user clicks Check again while an agent's `docx pages` holds Office longer than the check's limit. The
    check gets `busy`, and that mustn't turn "found" into "not found": agents would lose their Word commands."""
    with _client(office_app(engine, tmp_path, fake("found"), timeout=1)) as client:
        deadline = time.monotonic() + 15
        while client.get("/api/office").json()["status"] != "found":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        job = OfficeRun(fake("hang"), timeout=8)  # stands in for hub-doc's Word job
        holding = threading.Thread(target=job.run)
        holding.start()
        try:
            time.sleep(0.5)
            body = client.post("/api/office/check").json()
            assert body["status"] == "found" and "busy" in (body["error"] or ""), body
            assert client.app.state.documents.access(tmp_path).office is True
        finally:
            job.cancel()
            holding.join(30)
        after = client.post("/api/office/check").json()
        assert after["status"] == "found" and after["error"] is None, after


def _client(app) -> TestClient:
    from support.hub import make_client

    return make_client(app)


def test_an_unusable_lock_folder_fails_closed_with_a_visible_error(hub_doc_exe: str, tmp_path: Path) -> None:
    """S5: if an agent's sandbox can't use the hub's lock folder, Word isn't started and the agent is told why (not
    that Office is unlicensed, S6)."""
    session = make_session(tmp_path / "attachments")
    document = session.original("report.docx", samples.formatted_docx())
    not_a_folder = tmp_path / "lock-is-a-file"
    not_a_folder.write_text("x")
    started = time.monotonic()
    reply = HubDoc(hub_doc_exe, session.outputs).run("docx", "pages", document,
                                                     env={office_registry.REGISTRY_ENV: str(not_a_folder)})
    assert reply.code == 2 and "lock folder" in reply.data["error"], reply
    assert "licensed" not in reply.data["error"]
    assert time.monotonic() - started < 15
    assert not any(session.outputs.iterdir())


@pytest.mark.parametrize("answer, licence", [
    ({"ok": False, "busy": True, "error": "Office stayed busy"}, False),
    ({"ok": False, "lock_folder": True, "error": "its lock folder can't be used"}, False),
    ({"ok": False, "error": "Class not registered"}, True),
])
def test_only_a_real_office_failure_mentions_the_licence(tmp_path: Path, answer: dict, licence: bool) -> None:
    with pytest.raises(Failed) as failed:
        office._require(answer, tmp_path / "missing.pdf")
    assert ("licensed Office" in str(failed.value)) is licence
    assert answer["error"] in str(failed.value)


def test_agents_get_the_hubs_resolved_lock_folder(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "hub-office"
    monkeypatch.setenv(office_registry.REGISTRY_ENV, str(folder))
    env = DocumentAccess(outputs=tmp_path).env()
    assert env[office_registry.REGISTRY_ENV] == os.path.realpath(folder)
    monkeypatch.delenv(office_registry.REGISTRY_ENV)
    assert DocumentAccess(outputs=tmp_path).env()[office_registry.REGISTRY_ENV] == \
        os.path.realpath(office_registry.folder())  # the default %TEMP% folder, resolved by the hub


def _record(folder: Path, owner: int, owner_created: int | None, office: list[tuple[int, int | None]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"run-{owner}-{uuid.uuid4().hex[:8]}.json").write_text(json.dumps(
        {"owner": owner, "owner_created": owner_created, "office": [{"pid": p, "created": c} for p, c in office]}))


def _start(exe: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen([str(exe), "-c", "import time; time.sleep(120)", *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@windows_only
def test_only_registered_office_of_dead_runs_is_reaped(office_fakes: Path, registry: Path) -> None:
    orphan = _start(office_fakes / "WINWORD.EXE", "/Automation", "-Embedding")
    live_runs = _start(office_fakes / "EXCEL.EXE", "/Automation", "-Embedding")
    unregistered = _start(office_fakes / "WINWORD.EXE", "/Automation", "-Embedding")
    users_word = _start(office_fakes / "WINWORD.EXE")
    reused = _start(office_fakes / "EXCEL.EXE", "/Automation", "-Embedding")
    procs = [orphan, live_runs, unregistered, users_word, reused]
    try:
        time.sleep(0.5)
        dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        dead_pid = int(dead.stdout)
        created = {p.pid: winproc.creation_time(p.pid) for p in procs}
        # A dead owner: its Office is reaped, except an entry whose creation time no longer matches (PID reused)
        # and the user's own Word (no automation flag), even though both are listed.
        _record(registry, dead_pid, 1234, [(orphan.pid, created[orphan.pid]), (reused.pid, 99),
                                           (users_word.pid, created[users_word.pid])])
        # A live owner (this test process): its Office is left alone.
        _record(registry, os.getpid(), winproc.creation_time(os.getpid()), [(live_runs.pid, created[live_runs.pid])])
        run = OfficeRun(fake("missing"), timeout=20)
        run.run()
        assert run.reaped == [orphan.pid]
        time.sleep(0.3)
        assert orphan.poll() is not None
        assert all(p.poll() is None for p in (live_runs, unregistered, users_word, reused))
        assert len(list(registry.glob("run-*.json"))) == 1  # the live owner's record stays
    finally:
        for process in procs:
            process.kill()


@windows_only
def test_office_of_a_run_killed_midway_is_reaped_by_the_next_run(office_fakes: Path, registry: Path,
                                                                 tmp_path: Path) -> None:
    """QA's S1 repro without real Office: hub-doc is killed while its Office runs (Stop, a Bash timeout). Its Word
    is left behind; the next run, inside the lock, kills it."""
    pid_file = tmp_path / "pids.txt"
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(BACKEND)!r})
        os.environ[{office_registry.REGISTRY_ENV!r}] = {str(registry)!r}
        from app.doctool.office import OfficeRun
        OfficeRun({fake("leftover", office_fakes, pid_file)!r}, timeout=120).run()
    """)
    runner = subprocess.Popen([sys.executable, "-c", script])
    left: set[int] = set()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            left = pids(pid_file)
            records = [json.loads(p.read_text()) for p in registry.glob("run-*.json")]
            if len(left) == 2 and any(len(r["office"]) == 2 for r in records):
                break
            time.sleep(0.2)
        assert len(left) == 2, "the fake Office didn't start"
        subprocess.run(["taskkill", "/PID", str(runner.pid), "/T", "/F"], capture_output=True)
        runner.wait(10)
        assert all(alive(pid) for pid in left)  # orphaned, as COM-started Office is
        run = OfficeRun(fake("missing"), timeout=20)
        run.run()
        assert set(run.reaped) == left
        time.sleep(0.3)
        assert not any(alive(pid) for pid in left)
        assert not list(registry.glob("run-*.json"))
    finally:
        runner.kill()
        kill_all(left)


def _dead_owner() -> tuple[int, int]:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    created = winproc.creation_time(gone.pid) or 1
    gone.wait()
    return gone.pid, created


@windows_only
def test_garbled_and_partial_records_kill_nothing(office_fakes: Path, registry: Path) -> None:
    """A record cut short by a crash mid-write (the `.part` file), or garbled, can't be trusted: nothing it seems to
    name is killed. A garbled record is removed, and the `.part` file is never read as a record."""
    word = _start(office_fakes / "WINWORD.EXE", "/Automation", "-Embedding")
    try:
        time.sleep(0.3)
        registry.mkdir()
        owner, owner_created = _dead_owner()
        good = json.dumps({"owner": owner, "owner_created": owner_created,
                           "office": [{"pid": word.pid, "created": winproc.creation_time(word.pid)}]})
        (registry / "run-1-aaaa.json").write_text(good[: len(good) // 2])
        (registry / "run-2-bbbb.json").write_text('{"owner": "nobody", "office": 7}')
        (registry / "run-3-cccc.part").write_text(good)
        (registry / "run-4-dddd.json").write_bytes(b"\xff\xfe\x00garbage")
        assert office_registry.reap(is_automation=is_automation) == []
        assert word.poll() is None
        assert sorted(p.name for p in registry.iterdir()) == ["run-3-cccc.part"]
    finally:
        word.kill()


@windows_only
def test_a_record_held_open_is_skipped_not_deleted(office_fakes: Path, registry: Path) -> None:
    """A record that can't be read at this moment (its writer is replacing it, an antivirus scan holds it) is left
    for a later run. Before the fix it was deleted unread, and its orphaned Word could never be reaped."""
    word = _start(office_fakes / "WINWORD.EXE", "/Automation", "-Embedding")
    try:
        time.sleep(0.3)
        registry.mkdir()
        owner, owner_created = _dead_owner()
        record = registry / f"run-{owner}-eeee.json"
        record.write_text(json.dumps({"owner": owner, "owner_created": owner_created,
                                      "office": [{"pid": word.pid, "created": winproc.creation_time(word.pid)}]}))
        with Held(record, 0):
            assert office_registry.reap(is_automation=is_automation) == []
        assert record.exists() and word.poll() is None
        assert office_registry.reap(is_automation=is_automation) == [word.pid]  # readable again: reaped
        deadline = time.monotonic() + 10
        while word.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert word.poll() is not None and not record.exists()
    finally:
        word.kill()


@windows_only
def test_concurrent_writers_keep_their_own_records(registry: Path) -> None:
    """Records are one file per run, replaced whole: runs recording at the same time (each in the lock-holder's
    process tree or not) never mix or truncate each other's file."""
    records = [office_registry.RunRecord() for _ in range(4)]
    me = os.getpid()

    def write(index: int) -> None:
        for round_ in range(40):
            records[index].record({me, 100_000 + index * 1000 + round_})

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(write, range(4)))
    for index, record in enumerate(records):
        data = json.loads(record.path.read_text(encoding="utf-8"))
        assert data["owner"] == me
        assert {e["pid"] for e in data["office"]} == {me, 100_000 + index * 1000 + 39}
        record.clear()
    assert list(registry.glob("run-*")) == []


@windows_only
def test_a_cleanup_racing_a_live_run_spares_it(office_fakes: Path, registry: Path, tmp_path: Path) -> None:
    """Another process's cleanup runs while a run in a second process is still waiting for its Word: the run's
    owner is alive, so its Word is spared (and a new run is told Office is busy). Only when that process is killed
    does the next cleanup reap the Word it left."""
    pid_file = tmp_path / "leftover-pids.txt"
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(BACKEND)!r})
        os.environ[{office_registry.REGISTRY_ENV!r}] = {str(registry)!r}
        from app.doctool.office import OfficeRun
        OfficeRun({fake("leftover", office_fakes, pid_file)!r}, 60).run()
    """)
    runner = subprocess.Popen([sys.executable, "-c", script])
    office: set[int] = set()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            recorded = [json.loads(p.read_text()) for p in registry.glob("run-*.json")]
            office = {e["pid"] for r in recorded for e in r["office"]}
            if len(office) >= 2:
                break
            time.sleep(0.1)
        assert len(office) >= 2, "the run never recorded its Office processes"
        assert office_registry.reap(is_automation=is_automation) == []
        assert all(alive(pid) for pid in office)
        assert OfficeRun(fake("found"), timeout=1).run().get("busy") is True
        subprocess.run(["taskkill", "/PID", str(runner.pid), "/T", "/F"], capture_output=True)
        runner.wait(30)
        answer = OfficeRun(fake("found"), timeout=20)
        assert answer.run()["ok"] and set(answer.reaped) == office
    finally:
        runner.kill()
        for pid in office | {int(p) for p in pid_file.read_text().split()} if pid_file.exists() else office:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
