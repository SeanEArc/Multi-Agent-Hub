"""Revision 4 PR 2, QA round 1 fixes: one Office run at a time on the PC (B1), Office left by a killed run is reaped
(S1), the Excel walk visits only filled cells (S2), hub-doc reads only the session's folders (S4), and Save as…
can't reach the attachments folder by another route."""

import ctypes
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path

import pytest

from app.attachments import AttachmentStore
from app.doctool import office_registry, winproc
from app.doctool.office import OfficeRun
from app.doctool.paths import within
from app.documents import DocumentAccess
from app.outputs import OutputsStore
from test_documents_acceptance import HubDoc, hub_doc_exe, make_session  # noqa: F401
from test_documents_acceptance_office import alive, office_fakes  # noqa: F401
import document_samples as samples

FAKE_OFFICE = Path(__file__).parent / "fake_office.py"
windows = pytest.mark.skipif(os.name != "nt", reason="Windows processes and paths")


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


# --- B1: one Office run at a time ---------------------------------------------------------------------------------


@windows
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


@windows
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


@windows
def test_a_killed_process_never_keeps_the_lock(registry: Path) -> None:
    script = textwrap.dedent(f"""
        import os, sys, threading, time
        sys.path.insert(0, {str(Path(__file__).parent.parent)!r})
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


# --- S1: the registry ---------------------------------------------------------------------------------------------


def _record(folder: Path, owner: int, owner_created: int | None, office: list[tuple[int, int | None]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"run-{owner}-{uuid.uuid4().hex[:8]}.json").write_text(json.dumps(
        {"owner": owner, "owner_created": owner_created, "office": [{"pid": p, "created": c} for p, c in office]}))


def _start(exe: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen([str(exe), "-c", "import time; time.sleep(120)", *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@windows
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


@windows
def test_office_of_a_run_killed_midway_is_reaped_by_the_next_run(office_fakes: Path, registry: Path,
                                                                 tmp_path: Path) -> None:
    """QA's S1 repro without real Office: hub-doc is killed while its Office runs (Stop, a Bash timeout). Its Word
    is left behind; the next run, inside the lock, kills it."""
    pid_file = tmp_path / "pids.txt"
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(Path(__file__).parent.parent)!r})
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


# --- S2: only filled cells ----------------------------------------------------------------------------------------


def test_a_stray_far_cell_doesnt_make_the_walk_slow(hub_doc_exe: str, tmp_path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "S"
    sheet["A1"] = 1
    sheet["B2"] = "=A1*2"
    sheet["XFD1048576"] = "=A1/0"
    session = make_session(tmp_path / "attachments")
    path = session.root / "stray.xlsx"
    workbook.save(path)
    tool = HubDoc(hub_doc_exe, session.outputs)
    started = time.monotonic()
    checked = tool.xlsx_check(path)
    assert checked.code == 0 and [(e["cell"], e["error"]) for e in checked.data["errors"]] == [("XFD1048576", "#DIV/0!")]
    edited = tool.run("xlsx", "edit", path, "--set", "S!A1=5")
    assert edited.code == 0 and {c["cell"] for c in edited.data["changed"]} == {"A1", "B2"}
    exported = tool.xlsx_csv(path, "stray.csv", "S")
    assert exported.code == 0
    assert time.monotonic() - started < 30
    rows = Path(exported.data["output"]).read_text(encoding="utf-8-sig").splitlines()
    assert rows[0] == "1" and rows[1] == ",2" and len(rows) == 1_048_576


# --- S4: what hub-doc may read ------------------------------------------------------------------------------------


def _short_name(path: Path) -> str:
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value


@windows
def test_hub_doc_reads_only_inside_its_read_roots(hub_doc_exe: str, tmp_path: Path) -> None:
    session = make_session(tmp_path / "attachments")
    inside = session.original("sales file.xlsx", samples.sales_xlsx())
    elsewhere = tmp_path / "private"
    elsewhere.mkdir()
    secret = elsewhere / "secret.xlsx"
    secret.write_bytes(samples.sales_xlsx())
    tool = HubDoc(hub_doc_exe, session.outputs)
    roots = {"HUB_DOC_READ_ROOTS": os.pathsep.join([str(session.root), str(session.outputs)])}

    def check(path: object) -> int:
        return tool.run("xlsx", "check", path, env=roots).code

    assert check(inside) == 0
    short = _short_name(inside)
    if short and short != str(inside):
        assert check(short) == 0  # an 8.3 spelling of a file inside
    assert check(secret) == 1
    assert check(f"\\\\?\\{secret}") == 1
    assert check(elsewhere / "missing.xlsx") == 1  # refused before saying whether it exists
    assert check(session.root / ".." / "private" / "secret.xlsx") == 1
    link = session.root / "link"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)], capture_output=True, check=True)
    try:
        assert check(link / "secret.xlsx") == 1  # a junction inside that leads out
    finally:
        os.rmdir(link)
    share = "\\\\localhost\\" + str(secret).replace(":", "$", 1)
    if os.path.exists(share):  # the admin share needs an elevated token: checked when it's reachable
        assert check(share) == 1
        assert check("\\\\localhost\\" + str(inside).replace(":", "$", 1)) == 0
    reply = tool.run("docx", "report", secret, env=roots)
    assert reply.code == 1 and "outside the folders hub-doc may read" in reply.data["error"]
    # Under the hub (outputs set) with the read limit unset, only the session's folder: unsetting it can't widen reads.
    assert tool.xlsx_check(secret).code == 1 and tool.xlsx_check(inside).code == 0
    assert HubDoc(hub_doc_exe, None).xlsx_check(secret).code == 0  # run by hand, neither variable set: any file


def test_agents_get_the_sessions_folders_as_read_roots(client, tmp_path: Path) -> None:
    session_id = client.post("/api/sessions", json={"title": "ok", "agent": "claude"}).json()["id"]
    orchestrator = client.app.state.orchestrator

    class Stub:
        cwd = tmp_path / "work"
        documents = None

    stub = Stub()
    orchestrator._equip(session_id, stub)
    outputs = orchestrator.outputs.folder(session_id)
    assert stub.documents.read_roots == (outputs.parent, outputs, tmp_path / "work")
    roots = stub.documents.env()["HUB_DOC_READ_ROOTS"].split(os.pathsep)
    assert roots == [str(outputs.parent), str(outputs), str(tmp_path / "work")]


def test_read_roots_are_left_out_when_there_are_none(tmp_path: Path) -> None:
    assert "HUB_DOC_READ_ROOTS" not in DocumentAccess(outputs=tmp_path).env()


# --- Save as… by another route ------------------------------------------------------------------------------------


@windows
def test_within_matches_short_names_junctions_and_prefixes(tmp_path: Path) -> None:
    root = tmp_path / "a long folder name"
    (root / "sub").mkdir(parents=True)
    assert within(root / "sub" / "x.txt", root)
    assert within(f"\\\\?\\{root}\\sub\\x.txt", root)
    short = _short_name(root)
    if short and short != str(root):
        assert within(Path(short) / "x.txt", root)
    other = tmp_path / "other"
    other.mkdir()
    link = other / "into"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(root)], capture_output=True, check=True)
    try:
        assert within(link / "x.txt", root)
        assert not within(other / "x.txt", root)
    finally:
        os.rmdir(link)


@windows
def test_save_as_through_a_junction_into_attachments_is_refused(tmp_path: Path) -> None:
    store = OutputsStore(AttachmentStore(tmp_path / "attachments"))
    session = uuid.uuid4().hex
    (store.folder(session, create=True) / "r.txt").write_text("copy")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = elsewhere / "looks-harmless"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tmp_path / "attachments")], capture_output=True,
                   check=True)
    try:
        from app.attachments import AttachmentError

        with pytest.raises(AttachmentError) as refused:
            store.save_copy(session, "r.txt", str(link / "copy.txt"))
        assert refused.value.status == 403
    finally:
        os.rmdir(link)
