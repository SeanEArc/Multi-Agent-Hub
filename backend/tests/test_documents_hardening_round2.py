"""Hardening, round 2: the engineer's fixes for QA's round 1 (revision 4 PR 2), under the conditions they were built
for: the PC-wide Office lock and the registry of Office processes (`app.doctool.office_registry`), hub-doc's read
limit (`HUB_DOC_READ_ROOTS`, `app.doctool.paths`) and the sparse Excel walk with `truncated` (`app.doctool.xlsx`).
`tests/test_documents_round2.py` holds the engineer's own tests; these don't repeat them.

Never real Office: the "Office" here is `tests/fake_office.py` or a recording stand-in script, and every test uses its
own registry folder (`HUB_OFFICE_REGISTRY`), so the lock is never shared with the user's real runs.
"""

import ctypes
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

import openpyxl
import pytest
from fastapi.testclient import TestClient

import document_samples as samples
from app.doctool import cli, office_registry, winproc, xlsx
from app.doctool.contract import READ_ROOTS_ENV, Refused, read_roots
from app.doctool.office import OfficeRun, is_automation
from app.doctool.paths import within_any
from test_documents_acceptance import HubDoc, hub_doc_exe  # noqa: F401  (fixture)
from test_documents_acceptance_office import alive, office_app, office_fakes  # noqa: F401  (office_fakes: fixture)
from test_documents_hardening import Held, junction
from test_orchestrator import create, wait_until_idle
from test_worktrees import repo  # noqa: F401  (fixture)

WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows file locks, junctions and processes")
FAKE_OFFICE = Path(__file__).parent / "fake_office.py"


def fake(mode: str, *args: object) -> list[str]:
    return [sys.executable, str(FAKE_OFFICE), mode, *map(str, args)]


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "office-registry"
    monkeypatch.setenv(office_registry.REGISTRY_ENV, str(folder))
    return folder


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


# --- The lock ------------------------------------------------------------------------------------------------------


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
    from conftest import make_client

    return make_client(app)


# --- The registry --------------------------------------------------------------------------------------------------


def _start(exe: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen([str(exe), "-c", "import time; time.sleep(120)", *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
        sys.path.insert(0, {str(Path(__file__).parent.parent)!r})
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


# --- The read limit ------------------------------------------------------------------------------------------------


def short_name(path: Path) -> str:
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value


@pytest.fixture
def folders(tmp_path: Path) -> dict[str, Path]:
    inside = tmp_path / "Session Files"
    sibling = tmp_path / "Session Files-other"  # same prefix, a different folder
    outside = tmp_path / "elsewhere"
    for folder in (inside, sibling, outside):
        folder.mkdir()
        (folder / "book.xlsx").write_bytes(samples.sales_xlsx())
    return {"inside": inside, "sibling": sibling, "outside": outside}


def gate(monkeypatch: pytest.MonkeyPatch, raw: str | None, file: Path | str) -> None:
    """hub-doc's read check for one input, with HUB_DOC_READ_ROOTS set to `raw` (None: unset)."""
    if raw is None:
        monkeypatch.delenv(READ_ROOTS_ENV, raising=False)
    else:
        monkeypatch.setenv(READ_ROOTS_ENV, raw)
    cli.check(cli.parse(["xlsx", "check", str(file)]))


@windows_only
def test_every_spelling_of_a_file_inside_is_allowed(folders: dict[str, Path], monkeypatch) -> None:
    inside = folders["inside"]
    target = inside / "book.xlsx"
    spellings = [str(target), str(target).upper(), "\\\\?\\" + str(target), str(inside / "sub" / ".." / "book.xlsx")]
    short = short_name(target)
    if short and short.upper() != str(target).upper():
        spellings.append(short)
    for spelling in spellings:
        gate(monkeypatch, str(inside), spelling)  # no Refused


@windows_only
@pytest.mark.parametrize("which", ["outside", "sibling prefix", "dotdot", "verbatim", "stream", "trailing dot",
                                   "short name", "junction in"])
def test_every_spelling_of_a_file_outside_is_refused(folders: dict[str, Path], monkeypatch, tmp_path: Path,
                                                      which: str) -> None:
    inside, outside = folders["inside"], folders["outside"]
    target = outside / "book.xlsx"
    spelling = {
        "outside": str(target),
        "sibling prefix": str(folders["sibling"] / "book.xlsx"),
        "dotdot": str(inside / ".." / "elsewhere" / "book.xlsx"),
        "verbatim": "\\\\?\\" + str(target),
        "stream": str(target) + ":Zone.Identifier",
        "trailing dot": str(outside) + ".\\book.xlsx",
        "short name": short_name(target),
        "junction in": "",
    }[which]
    if which == "junction in":
        junction(inside / "link", outside)
        spelling = str(inside / "link" / "book.xlsx")
    if not spelling:
        pytest.skip("no 8.3 names on this volume")
    try:
        with pytest.raises(Refused, match="outside the folders"):
            gate(monkeypatch, str(inside), spelling)
    finally:
        if (inside / "link").exists():
            os.rmdir(inside / "link")


@windows_only
def test_roots_given_by_other_spellings_or_through_a_junction(folders: dict[str, Path], monkeypatch,
                                                              tmp_path: Path) -> None:
    """The hub's roots may themselves be junctions (a user's profile moved to another drive), short names or
    `\\\\?\\` paths: the files really inside are allowed by any spelling, the rest still refused."""
    inside = folders["inside"]
    via = tmp_path / "via-junction"
    junction(via, inside)
    try:
        roots = [str(via), "\\\\?\\" + str(inside).lower() + "\\", short_name(inside) or str(inside)]
        for root in roots:
            gate(monkeypatch, root, inside / "book.xlsx")
            gate(monkeypatch, root, via / "book.xlsx")
            with pytest.raises(Refused):
                gate(monkeypatch, root, folders["outside"] / "book.xlsx")
    finally:
        os.rmdir(via)


def test_an_empty_or_malformed_read_limit_reads_nothing(folders: dict[str, Path], monkeypatch) -> None:
    """Decided here: only an unset HUB_DOC_READ_ROOTS (hub-doc run by hand) allows any file. Set but empty, only
    separators, or only relative parts (which would depend on the agent's current folder) allows nothing; a root
    that doesn't exist allows nothing outside it. Before the fix, an empty value allowed every file."""
    target = folders["outside"] / "book.xlsx"
    gate(monkeypatch, None, target)  # by hand: any file
    assert read_roots() is None
    for raw in ["", " ", os.pathsep * 3, "relative" + os.pathsep + ".", str(folders["inside"] / "missing")]:
        with pytest.raises(Refused):
            gate(monkeypatch, raw, target)
    monkeypatch.chdir(folders["outside"])
    with pytest.raises(Refused):
        gate(monkeypatch, ".", "book.xlsx")  # "." can't name the current folder
    gate(monkeypatch, os.pathsep.join(["relative", str(folders["outside"]), ""]), target)  # the absolute part counts


@windows_only
def test_hub_doc_itself_refuses_with_an_empty_read_limit(hub_doc_exe: str, folders: dict[str, Path]) -> None:
    reply = HubDoc(hub_doc_exe, None).run("xlsx", "check", folders["outside"] / "book.xlsx",
                                          env={READ_ROOTS_ENV: ""})
    assert reply.code == 1 and "outside the folders" in reply.data["error"], reply


def roots_of(client: TestClient, session_id: str) -> tuple[Path, ...]:
    return client.app.state.orchestrator._adapters[session_id].documents.read_roots


def test_roots_for_a_chat(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={"title": "ok", "agent": "claude"}).json()["id"]
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi"})
    wait_until_idle(client, session_id)
    session_folder = client.app.state.orchestrator.attachments.session_dir(session_id)
    chat_folder = Path(client.get(f"/api/sessions/{session_id}").json()["project_path"])
    assert roots_of(client, session_id) == (session_folder, session_folder / "outputs", chat_folder)


def test_roots_for_a_worktree_subfolder_and_after_a_handoff(client: TestClient, repo: Path) -> None:  # noqa: F811
    """A session on `<repo>/src` runs in `<worktree>/src`: that is the working folder hub-doc may read (never the
    user's own checkout), and a handoff to another agent keeps the same roots."""
    session_id = create(client, repo / "src", "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "where are you"})
    wait_until_idle(client, session_id)
    session = client.get(f"/api/sessions/{session_id}").json()
    worktree = Path(session["worktree"]["path"])
    ran_in = Path(json.loads(client.get(f"/api/sessions/{session_id}/events").json()[-1]["payload"]["text"])["cwd"])
    roots = roots_of(client, session_id)
    assert Path(roots[2]).resolve() == (worktree / "src").resolve() == ran_in.resolve()
    assert not any(Path(r).resolve().is_relative_to(repo.resolve()) for r in roots)

    assert client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "codex"}).status_code == 202
    wait_until_idle(client, session_id)
    assert client.get(f"/api/sessions/{session_id}").json()["agent"] == "codex"
    assert roots_of(client, session_id) == roots


# --- The Excel walk ------------------------------------------------------------------------------------------------


@pytest.fixture
def sales(tmp_path: Path) -> Path:
    path = tmp_path / "sales.xlsx"
    path.write_bytes(samples.sales_xlsx())
    return path


def snapshot(path: Path) -> tuple[dict, bool]:
    book = xlsx.Book.open(str(path))
    return book.snapshot(), book.truncated


@pytest.mark.parametrize("cells_per_export", [1, 2, 3, 5, 6, 7, 32, 33, 34])
def test_bands_split_at_any_boundary_lose_nothing(sales: Path, monkeypatch, cells_per_export: int) -> None:
    """Exports cut short are split by rows and, for a single row, by columns: whatever the export's cap, every
    filled cell comes back once (before, a row wider than the cap lost its other cells)."""
    full, _ = snapshot(sales)
    monkeypatch.setattr(xlsx, "EXPORT_CELLS", cells_per_export)
    split, truncated = snapshot(sales)
    assert split == full and truncated is False


def test_exactly_the_limit_is_complete_and_one_more_is_truncated(sales: Path, monkeypatch) -> None:
    """At exactly FILLED_LIMIT filled cells nothing was left out, even when the walk ran in several bands; one cell
    over, the reply says `truncated`."""
    total = len(snapshot(sales)[0])
    monkeypatch.setattr(xlsx, "EXPORT_CELLS", 5)
    monkeypatch.setattr(xlsx, "FILLED_LIMIT", total)
    assert "truncated" not in xlsx.Book.open(str(sales)).check()
    monkeypatch.setattr(xlsx, "FILLED_LIMIT", total - 1)
    reply = xlsx.Book.open(str(sales)).check()
    assert reply["truncated"] is True and any("filled cells" in w for w in reply["warnings"])


def test_an_edit_near_the_limit_lists_only_real_changes(sales: Path, tmp_path: Path, monkeypatch) -> None:
    """`edit` snapshots the workbook before and after. They shared one budget, so past half the limit the second
    came back short and every cell it missed was listed as changed (33 instead of 3 here)."""
    total = len(snapshot(sales)[0])
    for limit in (total, total // 2 + 1):
        monkeypatch.setattr(xlsx, "FILLED_LIMIT", limit)
        reply = xlsx.Book.open(str(sales)).edit(["Sales!B2=11"], str(tmp_path / f"out-{limit}.xlsx"))
        cells = {c["cell"] for c in reply["changed"]}
        assert cells <= {"B2", "D2", "D7", "E2", "E7"}, reply["changed"]
        assert reply["changed_count"] == len(reply["changed"])
        assert reply.get("truncated", False) is (limit < total)


def test_csv_and_changes_under_truncated(sales: Path, tmp_path: Path, monkeypatch) -> None:
    """Past the limit, the CSV holds only what was read and says so, and the changed cells are still real ones."""
    monkeypatch.setattr(xlsx, "FILLED_LIMIT", 8)
    book = xlsx.Book.open(str(sales))
    reply = book.csv("Sales", None, str(tmp_path / "part.csv"))
    assert reply["truncated"] is True and any("8 filled cells" in w for w in reply["warnings"])
    rows = (tmp_path / "part.csv").read_text(encoding="utf-8-sig").splitlines()
    assert 1 <= len(rows) <= 8
    edited = xlsx.Book.open(str(sales)).edit(["Sales!A1=Area"], str(tmp_path / "e.xlsx"))
    assert edited["truncated"] is True and [c["cell"] for c in edited["changed"]] == ["A1"]


def test_merged_cells(tmp_path: Path) -> None:
    """A merged title row: only its first cell holds a value; the walk, check and CSV treat the rest as empty."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Report"
    ws["A1"] = "Quarterly report"
    ws.merge_cells("A1:D1")
    ws.append(["a", "b", "c", "d"])
    ws.append([1, 2, 3, "=A3+B3+C3"])
    path = tmp_path / "merged.xlsx"
    wb.save(path)
    book = xlsx.Book.open(str(path))
    state = book.snapshot()
    assert ("Report", "A1") in state and ("Report", "B1") not in state
    assert state[("Report", "D3")][0] == 6
    reply = book.csv("Report", None, str(tmp_path / "merged.csv"))
    lines = (tmp_path / "merged.csv").read_text(encoding="utf-8-sig").splitlines()
    assert lines[0] == "Quarterly report,,," and lines[2] == "1,2,3,6" and "truncated" not in reply


def test_a_sheet_of_only_formulas(tmp_path: Path) -> None:
    """openpyxl saves formulas with no cached results: every value comes from the recalculation, errors included."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Calc"
    ws["A1"] = "=2+3"
    ws["A2"] = "=A1*10"
    ws["A3"] = "=1/0"
    ws["B1"] = "=SUM(A1:A2)"
    ws["B2"] = '=UPPER("ok")'
    path = tmp_path / "formulas.xlsx"
    wb.save(path)
    reply = xlsx.Book.open(str(path)).check()
    assert [(e["cell"], e["error"]) for e in reply["errors"]] == [("A3", "#DIV/0!")]
    state = xlsx.Book.open(str(path)).snapshot()
    assert state[("Calc", "A2")][0] == 50 and state[("Calc", "B1")][0] == 55 and state[("Calc", "B2")][0] == "OK"
    assert all(formula for _, formula in state.values())
