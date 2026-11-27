"""Hardening tests for revision 4, attachments PR 2 ("working on documents"), written after the feature existed, to
target what only shows up in the chosen design on Windows: the hub-doc parent/child pair dying or timing out mid
work, files locked by another program, races on one output name, junctions planted after the path check, names
Windows can't create, the Office check's processes, every way a turn can start (handoff, waiting switch, a deleted
outputs folder), Codex's TOML quoting, the outputs routes' destinations, and PR 1's dashed session folders.

Everything runs against fakes and temp folders: never real Claude, Codex, Ollama or Office, never the real
%LOCALAPPDATA%\\MultiAgentHub (conftest's guard checks that at the end of the run).
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.adapters.claude import ClaudeAdapter
from app.adapters.codex import CodexAdapter, toml_value
from app.adapters.base import EventType
from app.attachments import AttachmentStore, make_read_only
from app.doctool import cli, winproc
from app.doctool.contract import STAGING_PREFIX, Failed, Refused
from app.doctool.outputs import OutputsFolder, Staging
from app.documents import CLAUDE_RULES, DocumentAccess

from support import document_samples as samples
from support.attachments import upload
from support.documents import FILE_SHARE_READ, Held, HubDoc, junction
from support.hub import opened_files
from support.office import alive, office_app
from support.paths import FAKE_CODEX
from support.sessions import wait_until_idle
from support.wired import (
    OutputsApi,
    WiredHub,
    allowed_tools,
    make_chat_with_original,
    outputs_existed,
    same_path,
    writable_roots,
)

WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows file locks, junctions and processes")


def staging_dirs(folder: Path) -> list[Path]:
    return [p for p in folder.rglob(f"{STAGING_PREFIX}*")] if folder.exists() else []


def files_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.exists() else []


# --- hub-doc: the supervising parent and its child -----------------------------------------------------------------

# A stand-in for hub-doc's child (`python -m app.doctool --hub-doc-child`): it reads the same request, starts writing
# the staged output the parent named, and then dies or misbehaves the ways a real child can.
CHILD = r'''
import ctypes, json, os, sys, time
mode = sys.argv[1]
request = json.loads(sys.stdin.buffer.read())
output = request.get("output")
if output and mode != "ok-no-file":
    with open(output, "wb") as partial:
        partial.write(b"PK\x03\x04 half a workbook")
        partial.flush()
if mode == "out-of-memory":  # what a capped BetterOffice ends in, with nothing on stderr
    ctypes.windll.kernel32.ExitProcess(0xC0000017)
if mode == "crash":
    sys.stderr.write("Fatal Python error: boom\n")
    sys.stderr.flush()
    os._exit(1)
if mode == "garbage":
    print("this is not the JSON reply", flush=True)
    sys.exit(0)
if mode == "ok-no-file":
    print(json.dumps({"ok": True}), flush=True)
    sys.exit(0)
if mode == "hang":
    time.sleep(120)
'''


@pytest.fixture
def child_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "child_stub.py"
    script.write_text(CHILD, encoding="utf-8")

    def use(mode: str, *extra: str) -> None:
        monkeypatch.setattr(cli, "child_command", lambda: [sys.executable, str(script), mode, *extra])

    return use


def run_main(argv: list[str], outputs: Path | None, monkeypatch: pytest.MonkeyPatch,
             capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    """hub-doc's entry point in this process: (exit code, the one JSON object it printed)."""
    if outputs is None:
        monkeypatch.delenv("HUB_DOC_OUTPUTS", raising=False)
    else:
        monkeypatch.setenv("HUB_DOC_OUTPUTS", str(outputs))
    monkeypatch.setattr(sys, "argv", ["hub-doc", *argv])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) == 1, printed
    return exited.value.code, json.loads(printed[0])


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    original = tmp_path / "originals" / "sales.xlsx"
    original.parent.mkdir()
    original.write_bytes(samples.sales_xlsx())
    make_read_only(original)
    return original


@pytest.fixture
def outputs(tmp_path: Path) -> Path:
    # Beside the originals, as in a real session (`<session>/<attachment>/...` and `<session>/outputs`): with
    # HUB_DOC_OUTPUTS set and no HUB_DOC_READ_ROOTS, hub-doc reads only the outputs folder's parent (round 3).
    folder = tmp_path / "outputs"
    folder.mkdir(parents=True)
    return folder


@pytest.mark.parametrize("mode, message", [
    ("out-of-memory", "too large"),
    ("crash", "stopped unexpectedly"),
    ("garbage", "stopped unexpectedly"),
    ("ok-no-file", "didn't write the file"),
])
def test_a_child_that_dies_mid_write_leaves_no_file(child_stub, workbook: Path, outputs: Path, mode: str,
                                                    message: str, monkeypatch, capsys) -> None:
    """The child is where documents are opened, so it is what dies (out of memory under its cap, a crash, a garbled
    reply). Whatever it wrote stays in the staging folder: no partial file reaches outputs, the staging folder is
    removed, and the agent gets one JSON error with exit code 2."""
    child_stub(mode)
    code, reply = run_main(["xlsx", "edit", str(workbook), "--set", "Sales!B2=5", "--out", "fixed.xlsx"], outputs,
                           monkeypatch, capsys)
    assert code == 2 and reply["ok"] is False and message in reply["error"], reply
    assert files_in(outputs) == [] and staging_dirs(outputs) == []


@windows_only
def test_out_of_memory_is_recognised_by_its_exit_code_alone(child_stub, workbook: Path) -> None:
    """Windows reports exit codes unsigned (STATUS_NO_MEMORY is 3221225495, not -1073741801): an abort with nothing
    on stderr must still read as "too large", not as an unexplained crash."""
    child_stub("out-of-memory")
    with pytest.raises(Failed, match="too large"):
        cli.run_child({"command": {}}, 256 * 1024**2, timeout=60)


def test_a_timed_out_child_and_its_interpreter_are_killed(child_stub, workbook: Path, outputs: Path,
                                                         monkeypatch, capsys) -> None:
    """A stuck job is stopped at the time limit: exit 2 and JSON, nothing in outputs, and no process left behind
    (in a venv `python.exe` is a launcher whose child is the real interpreter; both must go)."""
    marker = f"hub-doc-timeout-{uuid.uuid4().hex}"
    child_stub("hang", marker)
    real_run_child = cli.run_child
    monkeypatch.setattr(cli, "run_child", lambda request, limit, timeout=None: real_run_child(request, limit, 1.5))
    for _ in range(3):
        started = time.monotonic()
        code, reply = run_main(["xlsx", "edit", str(workbook), "--set", "Sales!B2=5"], outputs, monkeypatch, capsys)
        assert code == 2 and "longer than" in reply["error"], reply
        assert time.monotonic() - started < 30
        assert files_in(outputs) == [] and staging_dirs(outputs) == []
    if WINDOWS:
        deadline = time.monotonic() + 5
        left = []
        while time.monotonic() < deadline:
            left = [pid for pid in winproc.processes({"PYTHON.EXE"}) if marker in (winproc.command_line(pid) or "")]
            if not left:
                break
            time.sleep(0.1)
        for pid in left:
            winproc.kill(pid)
        assert left == [], "a timed-out hub-doc child was left running"


def test_the_parent_failing_to_start_its_child_is_a_json_error(workbook: Path, outputs: Path, tmp_path: Path,
                                                             monkeypatch, capsys) -> None:
    """The parent's own failure (its child program missing, as in a broken install) is still one JSON object with
    exit code 2, and the staging folder it made is removed."""
    monkeypatch.setattr(cli, "child_command", lambda: [str(tmp_path / "missing" / "hub-doc.exe")])
    code, reply = run_main(["xlsx", "edit", str(workbook), "--set", "Sales!B2=5"], outputs, monkeypatch, capsys)
    assert code == 2 and reply["ok"] is False and reply["error"].startswith("hub-doc failed"), reply
    assert files_in(outputs) == [] and staging_dirs(outputs) == []
    code, reply = run_main(["xlsx", "check", str(workbook)], outputs, monkeypatch, capsys)  # reading, no staging
    assert code == 2 and reply["ok"] is False


@pytest.mark.parametrize("name", ["a?.xlsx", "a*.xlsx", "a<b>.xlsx", 'say "hi".xlsx', "a|b.xlsx", "NUL.xlsx",
                                  "CON.xlsx", "sub/COM1.xlsx", "tab\there.xlsx", "x.xlsx:stream"])
@windows_only
def test_names_windows_cant_create_are_refused_before_any_work(workbook: Path, outputs: Path, name: str,
                                                               monkeypatch) -> None:
    """Invalid characters failed only at the final rename, after the work (an unexplained OSError), and device names
    such as NUL.xlsx open the device on Windows 10 and Server 2022 (CI). Both are refused up front now."""
    def never(*args, **kwargs):
        raise AssertionError("the child ran for a name that can't be written")

    monkeypatch.setenv("HUB_DOC_OUTPUTS", str(outputs))
    monkeypatch.setattr(cli, "run_child", never)
    command = cli.parse(["xlsx", "edit", str(workbook), "--set", "Sales!B2=5", "--out", name])
    with pytest.raises((Failed, Refused)):
        cli.execute(command)
    assert files_in(outputs) == [] and staging_dirs(outputs) == []


@windows_only
def test_unusual_but_valid_names_still_work(outputs: Path) -> None:
    folder = OutputsFolder(str(outputs))
    for name in ["résumé – O'Brien (edited).xlsx", "日本語 😀.xlsx", "a.b.c.xlsx", "sub/deeper/x.xlsx",
                 "[draft] #1 & more; ok!.xlsx"]:
        assert folder.target(name, "x.xlsx", [], (".xlsx",)).name == Path(name).name


@windows_only
def test_a_junction_planted_after_the_check_doesnt_take_the_file_out(outputs: Path, tmp_path: Path) -> None:
    """Time of check vs time of use: the path is checked before the work, which can take minutes. A junction made
    meanwhile in place of a folder on the way (by another process the agent runs) must not carry the finished file
    out of outputs. The commit checks again; only the rename itself is left as a window (not a sandbox)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    folder = OutputsFolder(str(outputs))
    target = folder.target("sub/report.csv", "x.csv", [], (".csv",))
    staging = Staging(folder)
    try:
        staged = staging.file(".csv")
        staged.write_text("a,b\n", encoding="utf-8")
        junction(outputs / "sub", elsewhere)  # planted after the check, before the move
        with pytest.raises(Refused, match="changed"):
            staging.commit(staged, target)
        assert list(elsewhere.iterdir()) == []
    finally:
        staging.remove()
        if (outputs / "sub").exists():
            os.rmdir(outputs / "sub")


# --- hub-doc as agents run it: real processes ------------------------------------------------------------------


def _set_b2(reply_value: int) -> list[str]:
    return ["--set", f"Sales!B2={reply_value}"]


@windows_only
def test_concurrent_runs_writing_one_name_leave_one_whole_file(hub_doc_exe: str, workbook: Path,
                                                              outputs: Path) -> None:
    """Two agents (or one agent's parallel tool calls) writing the same output name at once: each run stages its own
    file and renames it into place, so the result is one complete workbook from one of the runs, never a mix; a run
    that loses the race gets a JSON reply, not a crash; no staging folder is left."""
    tool = HubDoc(hub_doc_exe, outputs)
    values = [11, 22, 33, 44]
    with ThreadPoolExecutor(len(values)) as pool:
        replies = list(pool.map(lambda v: (v, tool.run("xlsx", "edit", workbook, "--out", "same.xlsx", *_set_b2(v))),
                                values))
    ok = [value for value, reply in replies if reply.code == 0]
    assert ok, replies
    assert all(reply.code in (0, 1) for _, reply in replies), replies  # 1: replaced while another run held it
    final = openpyxl.load_workbook(outputs / "same.xlsx")
    assert final["Sales"]["B2"].value in ok
    assert files_in(outputs) == [outputs / "same.xlsx"] and staging_dirs(outputs) == []


@windows_only
def test_an_input_locked_by_another_program(hub_doc_exe: str, workbook: Path, outputs: Path) -> None:
    """Open in Excel (others may read) still works; held exclusively, hub-doc answers with a JSON failure (exit 2),
    never a hang or a traceback."""
    tool = HubDoc(hub_doc_exe, outputs)
    with Held(workbook, FILE_SHARE_READ):
        assert tool.run("xlsx", "check", workbook).code == 0
        edited = tool.run("xlsx", "edit", workbook, "--out", "while-open.xlsx", *_set_b2(3))
        assert edited.code == 0, edited
    with Held(workbook, 0):
        reply = tool.run("xlsx", "check", workbook)
        assert reply.code == 2, reply
        reply = tool.run("xlsx", "edit", workbook, "--out", "locked-input.xlsx", *_set_b2(3))
        assert reply.code == 2, reply
    assert not (outputs / "locked-input.xlsx").exists() and staging_dirs(outputs) == []


@windows_only
def test_an_output_open_in_another_program_isnt_replaced(hub_doc_exe: str, workbook: Path, outputs: Path) -> None:
    """The agent's earlier output is open in Excel (the user is looking at it) and the agent writes the same name:
    the rename can't happen. The reply says why (JSON, not ok), the open file is untouched, nothing is left staged,
    and the same command works once the file is closed."""
    tool = HubDoc(hub_doc_exe, outputs)
    first = tool.run("xlsx", "edit", workbook, "--out", "report.xlsx", *_set_b2(1))
    assert first.code == 0, first
    before = (outputs / "report.xlsx").read_bytes()
    with Held(outputs / "report.xlsx", FILE_SHARE_READ):
        reply = tool.run("xlsx", "edit", workbook, "--out", "report.xlsx", *_set_b2(2))
        assert reply.code != 0 and "open in another program" in reply.data["error"], reply
    assert (outputs / "report.xlsx").read_bytes() == before and staging_dirs(outputs) == []
    assert tool.run("xlsx", "edit", workbook, "--out", "report.xlsx", *_set_b2(2)).code == 0
    assert openpyxl.load_workbook(outputs / "report.xlsx")["Sales"]["B2"].value == 2


@windows_only
def test_unicode_names_and_folders(hub_doc_exe: str, tmp_path: Path) -> None:
    """A user name with an apostrophe and accents, and an emoji, in the outputs folder, the input and --out: the
    reply is UTF-8 JSON whatever the console code page, and the files land where it says."""
    outputs = tmp_path / "O'Brien – résumé 😀" / "outputs"
    outputs.mkdir(parents=True)
    original = tmp_path / "O'Brien – résumé 😀" / "données 日本.xlsx"
    original.write_bytes(samples.sales_xlsx())
    document = tmp_path / "O'Brien – résumé 😀" / "lettre é.docx"
    document.write_bytes(samples.formatted_docx())
    tool = HubDoc(hub_doc_exe, outputs)
    reply = tool.run("xlsx", "edit", original, "--out", "données (édité) 😀.xlsx", *_set_b2(4))
    assert reply.code == 0, reply
    assert Path(reply.data["output"]) == (outputs / "données (édité) 😀.xlsx").resolve()
    assert Path(reply.data["output"]).is_file()
    reply = tool.run("docx", "edit", document, "--out", "lettre (é).docx", "--replace", samples.PLAIN_NUMBER, "121")
    assert reply.code == 0, reply
    assert tool.run("docx", "report", outputs / "lettre (é).docx").code == 0


@windows_only
def test_a_path_longer_than_260_characters(hub_doc_exe: str, tmp_path: Path) -> None:
    """A deep outputs folder (a long user name, nested projects): with long paths off in Windows (the default on a
    home PC) the libraries can't open it. Either way the agent gets one JSON reply, and a failure leaves nothing."""
    deep = tmp_path.joinpath(*["a-rather-long-folder-name-for-depth"] * 7)
    verbatim = "\\\\?\\" + str(deep)
    os.makedirs(verbatim + "\\outputs", exist_ok=True)
    original = Path(verbatim) / "sales.xlsx"
    original.write_bytes(samples.sales_xlsx())
    assert len(str(deep / "outputs" / "sales (edited).xlsx")) > 260
    tool = HubDoc(hub_doc_exe, deep / "outputs")
    try:
        reply = tool.run("xlsx", "edit", deep / "sales.xlsx", *_set_b2(4))
        if reply.code == 0:
            assert Path("\\\\?\\" + reply.data["output"]).is_file()
        else:
            assert reply.code in (1, 2), reply
            leftover = [name for name in os.listdir(verbatim + "\\outputs")]
            assert leftover == [], leftover
    finally:
        shutil.rmtree("\\\\?\\" + str(tmp_path / "a-rather-long-folder-name-for-depth"), ignore_errors=True)


# --- Office ------------------------------------------------------------------------------------------------------

# A stand-in Office check that records each start (its PID) and then answers or hangs.
CHECK = r'''
import json, os, sys, time
with open(sys.argv[1], "a") as pids:
    pids.write(f"{os.getpid()}\n")
time.sleep(float(sys.argv[2]))
print(json.dumps({"ok": True, "word": True, "excel": True, "version": "16.0"}), flush=True)
'''


def recording_check(tmp_path: Path, seconds: float) -> tuple[list[str], Path]:
    script = tmp_path / "check_stub.py"
    script.write_text(CHECK, encoding="utf-8")
    pid_file = tmp_path / "check-pids.txt"
    return [sys.executable, str(script), str(pid_file), str(seconds)], pid_file


def pids(pid_file: Path) -> list[int]:
    return [int(p) for p in pid_file.read_text().split()] if pid_file.exists() else []


def wait_until(condition, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


@windows_only
def test_checks_asked_for_at_once_share_one_run(engine: Engine, tmp_path: Path) -> None:
    """Settings polls, the user clicks Check again twice, and the startup check is still running: one Office run,
    never several hidden Words at once (each cleanup would also count the others' processes as its own)."""
    command, pid_file = recording_check(tmp_path, 2)
    with _client(office_app(engine, tmp_path, command)) as client:
        wait_until(lambda: len(pids(pid_file)) == 1)  # the startup check
        with ThreadPoolExecutor(5) as pool:
            answers = list(pool.map(lambda _: client.post("/api/office/check").json(), range(5)))
        assert {a["status"] for a in answers} == {"found"}, answers
        assert len(pids(pid_file)) == 1, "concurrent checks started more than one run"
        assert client.post("/api/office/check").json()["status"] == "found"
        assert len(pids(pid_file)) == 2  # a later check is a new run
        wait_until(lambda: not any(alive(pid) for pid in pids(pid_file)))


@windows_only
def test_repeated_stuck_checks_leave_no_processes(engine: Engine, tmp_path: Path) -> None:
    """Each check that times out ends its own script (and the interpreter behind the venv launcher), so clicking
    Check again on a PC where Office hangs never piles up processes; the backend keeps answering meanwhile."""
    command, pid_file = recording_check(tmp_path, 120)
    with _client(office_app(engine, tmp_path, command, timeout=1)) as client:
        wait_until(lambda: client.get("/api/office").json()["status"] == "not found")
        for _ in range(3):
            done = threading.Event()
            answers: list[dict] = []

            def ask() -> None:
                answers.append(client.post("/api/office/check").json())
                done.set()

            threading.Thread(target=ask, daemon=True).start()
            started = time.monotonic()
            assert client.get("/api/health").status_code == 200  # the check runs off the event loop
            assert time.monotonic() - started < 2
            assert done.wait(20) and answers[0]["status"] == "not found"
        assert len(pids(pid_file)) == 4  # the startup check and three more
        wait_until(lambda: not any(alive(pid) for pid in pids(pid_file)), timeout=10)


@windows_only
def test_shutdown_ends_a_stuck_check(engine: Engine, tmp_path: Path) -> None:
    """Closing the app while hidden Office is stuck on a dialog: the backend stops within seconds and the check's
    process doesn't outlive it."""
    command, pid_file = recording_check(tmp_path, 120)
    client = _client(office_app(engine, tmp_path, command, timeout=60))
    client.__enter__()
    try:
        wait_until(lambda: len(pids(pid_file)) == 1)
    finally:
        started = time.monotonic()
        client.__exit__(None, None, None)
    assert time.monotonic() - started < 10
    wait_until(lambda: not any(alive(pid) for pid in pids(pid_file)), timeout=10)


@windows_only
def test_word_the_user_opens_during_a_check_survives(engine: Engine, tmp_path: Path, office_fakes: Path) -> None:
    """The cleanup kills Office started during the check only when automation started it: the user opening Word
    themselves while the check runs (no -Embedding) is never touched; the automation instance is."""
    pid_file = tmp_path / "started.txt"
    script = tmp_path / "check_with_users_word.py"
    script.write_text(r'''
import json, subprocess, sys
office, pid_file = sys.argv[1], sys.argv[2]
flags = 0x00000008 | 0x00000200  # detached, its own group: not a child DCOM-style
started = []
for args in ([], ["/Automation", "-Embedding"]):
    for extra in (0x01000000, 0):  # break away from a job when allowed
        try:
            p = subprocess.Popen([office + "\\WINWORD.EXE", "-c", "import time; time.sleep(120)", *args],
                                 creationflags=flags | extra, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            break
        except OSError:
            continue
    started.append(p.pid)
open(pid_file, "w").write(" ".join(map(str, started)))
print(json.dumps({"ok": True, "word": True, "excel": False, "version": "16.0"}), flush=True)
''', encoding="utf-8")
    users = automation = None
    try:
        with _client(office_app(engine, tmp_path, [sys.executable, str(script), str(office_fakes), str(pid_file)],
                                timeout=10)) as client:
            wait_until(lambda: client.get("/api/office").json()["status"] == "found")
            users, automation = pids(pid_file)
            wait_until(lambda: not alive(automation), timeout=10)
        assert alive(users), "the user's own Word, opened during the check, was killed"
    finally:
        for pid in (users, automation):
            if pid:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


def _client(app) -> TestClient:
    from support.hub import make_client

    return make_client(app)


# --- Agents: the outputs folder and toolkit on every way a turn starts --------------------------------------------


def test_outputs_exist_on_handoff_waiting_switch_and_after_deletion(wired: WiredHub) -> None:
    """The folder must exist when each agent process starts (Claude adds only folders that exist; Codex's sandbox
    needs its writable root): after the user deletes it mid-session, on a manual handoff that starts a turn at once,
    and on a waiting switch whose turn starts with the next message."""
    session_id = wired.chat("claude")
    outputs = wired.outputs(session_id)
    assert wired.send(session_id, "First, Claude")["status"] == "completed"

    shutil.rmtree(outputs)
    assert wired.send(session_id, "Again, after the folder was deleted")["status"] == "completed"
    assert outputs_existed(wired.launches[-1], outputs)

    shutil.rmtree(outputs)
    response = wired.client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "codex"})
    assert response.status_code == 202, response.text
    assert wait_until_idle(wired.client, session_id)["status"] == "completed"
    launch = wired.launches[-1]
    assert launch.command[1:3] == ["exec", "--json"] and outputs_existed(launch, outputs)
    assert same_path(writable_roots(launch.command[1:])[0][0], outputs)

    response = wired.client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "claude", "wait": True})
    assert response.status_code == 202, response.text
    assert wait_until_idle(wired.client, session_id)["status"] == "ready"
    shutil.rmtree(outputs)
    assert wired.send(session_id, "Now Claude again")["status"] == "completed"
    launch = wired.launches[-1]
    assert "--session-id" in launch.command and outputs_existed(launch, outputs)
    assert set(allowed_tools(launch.command[1:])) == set(CLAUDE_RULES)


def test_hub_doc_goes_first_on_path_and_the_users_path_is_kept(wired: WiredHub) -> None:
    """hub-doc's folder is put in front of the user's PATH, never in place of it: the agent still finds git, node and
    everything else the user has."""
    users_path = os.environ["PATH"]
    session_id = wired.chat("codex")
    assert wired.send(session_id, "hi")["status"] == "completed"
    added = wired.launches[-1].env["PATH"]
    first, _, rest = added.partition(os.pathsep)
    assert (Path(first) / ("hub-doc.exe" if WINDOWS else "hub-doc")).is_file()
    assert rest == users_path


def run_adapter(adapter, prompt: str = "hi", reply: bool = False) -> list:
    async def go() -> list:
        events = []
        stream = adapter.send_reply(prompt) if reply else adapter.start_task(prompt)
        async for event in stream:
            events.append(event)
        return events

    return asyncio.run(go())


@pytest.mark.parametrize("folder", ["Sean O'Brien", "Jürgen – Résumé", "space and 😀 emoji", "a\"quote"])
def test_codex_accepts_the_writable_root_for_any_folder_name(tmp_path: Path, folder: str) -> None:
    """The probe found a Windows path in a double-quoted TOML string fails; JSON quoting fixed backslashes, but its
    ASCII escapes write an emoji as a surrogate pair, which TOML refuses, so Codex stopped every turn with a config
    error. Run through the fake CLI, which parses the override as codex-cli does, on new and resumed turns."""
    if folder == "a\"quote" and WINDOWS:
        pytest.skip("Windows folder names can't hold a double quote")
    outputs = tmp_path / folder / "attachments" / uuid.uuid4().hex / "outputs"
    outputs.mkdir(parents=True)
    adapter = CodexAdapter(tmp_path, command=[sys.executable, str(FAKE_CODEX), "ok"])
    adapter.documents = DocumentAccess(outputs=outputs)
    for reply in (False, True):
        events = run_adapter(adapter, "hello", reply=reply)
        types = [event.type for event in events]
        assert EventType.COMPLETED in types and EventType.ERROR not in types, [(e.type, e.data) for e in events]
    instructions = next(o for o in adapter._options() if o.startswith("developer_instructions="))
    import tomllib

    assert str(outputs) in tomllib.loads("v = " + instructions.split("=", 1)[1])["v"]


def test_toml_values_round_trip() -> None:
    import tomllib

    for value in ["C:\\Users\\O'Brien\\😀\\x", "tab\tnew\nline", "del\x7f", ["a\\b", "ü"], "quote \" back \\"]:
        assert tomllib.loads("v = " + toml_value(value))["v"] == value


def test_codex_commands_get_the_same_environment_as_turns(tmp_path: Path) -> None:
    """/code-review and the other server commands run in `codex app-server`; its turns run commands too and are told
    about hub-doc, so the app server gets hub-doc on PATH and HUB_DOC_OUTPUTS like `codex exec`."""
    seen: list[dict] = []

    class Recorder:
        async def start(self, command, cwd, env):
            seen.append(dict(env))
            raise RuntimeError("stop here")

    outputs = tmp_path / "outputs"
    outputs.mkdir()
    adapter = CodexAdapter(tmp_path, command=["codex.exe"], runner=Recorder())
    adapter.documents = DocumentAccess(outputs=outputs, bin_dir=tmp_path / "bin")

    async def go() -> None:
        with pytest.raises(Exception):
            async for _ in adapter.run_command("/status"):
                pass

    asyncio.run(go())
    assert seen and seen[0]["HUB_DOC_OUTPUTS"] == str(outputs)
    assert seen[0]["PATH"].split(os.pathsep)[0] == str(tmp_path / "bin")
    assert seen[0]["GIT_TERMINAL_PROMPT"] == "0"


def test_claude_pre_approval_on_commands_but_not_summaries(tmp_path: Path) -> None:
    """Commands sent as typed (`/context` before a conversation) go through the same launch as a turn, so they carry
    the rule (harmless: they make no model request). Summaries run with `--tools ""`, a one-off read-only call, and
    get no rule: there is nothing to pre-approve."""
    seen: list[list[str]] = []

    class Recorder:
        async def start(self, command, cwd, env):
            seen.append(list(command))
            raise RuntimeError("stop here")

    adapter = ClaudeAdapter(tmp_path, command=["claude.exe"], runner=Recorder())
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    adapter.documents = DocumentAccess(outputs=outputs, bin_dir=tmp_path / "bin")

    async def go(stream) -> None:
        with pytest.raises(RuntimeError):
            async for _ in stream:
                pass

    asyncio.run(go(adapter.run_command("/context")))
    assert set(allowed_tools(seen[-1][1:])) == set(CLAUDE_RULES)
    asyncio.run(go(adapter.summarize("summarize", "be brief")))
    assert "--allowedTools" not in seen[-1] and seen[-1][seen[-1].index("--tools") + 1] == ""


# --- The outputs routes ------------------------------------------------------------------------------------------


def test_save_as_replaces_an_existing_file(client: TestClient, tmp_path: Path) -> None:
    session_id, _, outputs = make_chat_with_original(client)
    (outputs / "report.txt").write_text("new version", encoding="utf-8")
    destination = tmp_path / "report.txt"
    destination.write_text("old version", encoding="utf-8")
    assert OutputsApi(client, session_id).save("report.txt", destination).status_code == 200
    assert destination.read_text(encoding="utf-8") == "new version"
    make_read_only(destination)
    assert OutputsApi(client, session_id).save("report.txt", destination).status_code == 409


@windows_only
def test_save_as_into_a_folder_the_user_cant_write(client: TestClient, tmp_path: Path) -> None:
    """A folder whose permissions deny writing (Windows ignores a folder's read-only attribute, so this is the real
    case): a clear 409, and nothing half-written."""
    session_id, _, outputs = make_chat_with_original(client)
    (outputs / "report.txt").write_text("x", encoding="utf-8")
    locked = tmp_path / "locked"
    locked.mkdir()
    user = os.environ.get("USERNAME", "")
    denied = subprocess.run(["icacls", str(locked), "/deny", f"{user}:(OI)(CI)(W,AD)"], capture_output=True)
    if denied.returncode != 0:
        pytest.skip(f"couldn't change the folder's permissions: {denied.stdout!r}")
    try:
        response = OutputsApi(client, session_id).save("report.txt", locked / "report.txt")
        assert response.status_code == 409, response.text
    finally:
        subprocess.run(["icacls", str(locked), "/remove:d", user], capture_output=True)
    assert list(locked.iterdir()) == []


@windows_only
def test_save_as_to_a_network_path_that_isnt_there(client: TestClient) -> None:
    session_id, _, outputs = make_chat_with_original(client)
    (outputs / "report.txt").write_text("x", encoding="utf-8")
    started = time.monotonic()
    response = OutputsApi(client, session_id).save("report.txt", Path("\\\\127.0.0.1\\hub-test-no-such-share\\r.txt"))
    assert response.status_code == 400, response.text
    assert time.monotonic() - started < 30


@windows_only
def test_save_as_cant_reach_the_attachments_folder_by_another_spelling(client: TestClient) -> None:
    """The copy may never land among the originals. A `\\\\?\\` path, different case or a junction to the folder are
    all the same place."""
    session_id, note, outputs = make_chat_with_original(client)
    (outputs / "report.txt").write_text("x", encoding="utf-8")
    session_folder = Path(note["path"]).parent.parent
    api = OutputsApi(client, session_id)
    for destination in ["\\\\?\\" + str(session_folder / "copy.txt"), str(session_folder / "copy.txt").upper(),
                        "\\\\?\\" + str(Path(note["path"]).parent / "notes.txt")]:
        response = api.save("report.txt", Path(destination))
        assert response.status_code in (400, 403), (destination, response.text)
    assert not (session_folder / "copy.txt").exists()
    assert Path(note["path"]).read_bytes() == b"the original\n"


@pytest.mark.parametrize("name", ["CON", "NUL.txt", "copy.txt:hidden", "\\\\.\\NUL"])
@windows_only
def test_save_as_refuses_devices_and_streams(client: TestClient, tmp_path: Path, name: str) -> None:
    session_id, _, outputs = make_chat_with_original(client)
    (outputs / "report.txt").write_text("x", encoding="utf-8")
    destination = name if name.startswith("\\\\") else str(tmp_path / name)
    response = client.post(f"/api/sessions/{session_id}/outputs/save",
                           json={"name": "report.txt", "destination": destination})
    assert response.status_code == 400, response.text
    assert list(tmp_path.glob("copy.txt")) == []


@pytest.mark.parametrize("name", ["fix.ps1", "run.bat", "run.cmd", "macro.vbs", "tool.js", "setup.exe", "go.lnk",
                                  "site.url", "app.hta", "report.docx.exe", "macros.xlsm", "letter.docm",
                                  "page.html", "notes.txt:payload.ps1"])
@windows_only
def test_open_never_runs_scripts_or_programs(client: TestClient, name: str) -> None:
    session_id, _, outputs = make_chat_with_original(client)
    (outputs / name).write_bytes(b"echo pwned\n")
    opened_files.clear()
    response = OutputsApi(client, session_id).open(name)
    assert response.status_code in (200, 400, 404), response.text
    assert all(reveal for _, reveal in opened_files), opened_files


@windows_only
def test_the_list_shows_only_plain_files_inside(client: TestClient, tmp_path: Path) -> None:
    session_id, _, outputs = make_chat_with_original(client)
    (outputs / "a.txt").write_text("a", encoding="utf-8")
    (outputs / "a.txt:hidden").write_text("stream", encoding="utf-8")
    (outputs / f"{STAGING_PREFIX}abc").mkdir()
    (outputs / f"{STAGING_PREFIX}abc" / "half.xlsx").write_bytes(b"PK")
    (outputs / f"{STAGING_PREFIX}file.tmp").write_bytes(b"x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text("s", encoding="utf-8")
    junction(outputs / "link", elsewhere)
    try:
        assert [row["name"] for row in OutputsApi(client, session_id).list().json()] == ["a.txt"]
    finally:
        os.rmdir(outputs / "link")


# --- PR 1's dashed session folders, and the new undashed ones -----------------------------------------------------


def attachments_root(client: TestClient) -> Path:
    return client.app.state.orchestrator.attachments.root


def test_new_sessions_use_the_id_as_stored(client: TestClient) -> None:
    response = client.post("/api/sessions", json={"title": "ok", "agent": "claude"})
    session_id = response.json()["id"]
    assert len(session_id) == 32 and "-" not in session_id
    note = upload(client, session_id, "notes.txt", b"hello\n")
    assert Path(note["path"]).parent.parent == attachments_root(client) / session_id
    assert not (attachments_root(client) / str(uuid.UUID(session_id))).exists()
    # A dashed id names the same folder.
    store = AttachmentStore(attachments_root(client))
    assert store.session_dir(str(uuid.UUID(session_id))) == attachments_root(client) / session_id


def test_a_dashed_folder_from_a_pr1_build_keeps_working(client: TestClient) -> None:
    """A session made by a PR 1 development build has `<attachments>/<dashed uuid>`: uploads, serving, discarding,
    the outputs folder and deleting the session all use that folder, and no second (undashed) folder appears."""
    response = client.post("/api/sessions", json={"title": "ok", "agent": "claude"})
    session_id = response.json()["id"]
    legacy = attachments_root(client) / str(uuid.UUID(session_id))
    legacy.mkdir()
    new = attachments_root(client) / session_id

    kept = upload(client, session_id, "kept.txt", b"kept\n")
    dropped = upload(client, session_id, "dropped.txt", b"dropped\n")
    assert Path(kept["path"]).parent.parent == legacy
    served = client.get(f"/api/sessions/{session_id}/attachments/{kept['id']}/file")
    assert served.status_code == 200 and served.content == b"kept\n"
    assert client.delete(f"/api/sessions/{session_id}/attachments/{dropped['id']}").status_code in (200, 204)
    assert not Path(dropped["path"]).exists()

    response = client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi", "attachments": [kept["id"]]})
    assert response.status_code == 202, response.text
    wait_until_idle(client, session_id)
    assert (legacy / "outputs").is_dir() and not new.exists()
    (legacy / "outputs" / "made.txt").write_text("x", encoding="utf-8")
    assert [r["name"] for r in OutputsApi(client, session_id).list().json()] == ["made.txt"]

    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert not legacy.exists() and not new.exists()


def test_the_sweep_knows_both_folder_names(tmp_path: Path) -> None:
    """At startup the sweep removes empty folders of deleted sessions in either spelling (outputs made before a turn
    doesn't count as content), keeps live sessions' folders in either spelling, and never removes one with files."""
    store = AttachmentStore(tmp_path)
    live_old, live_new, gone_old, gone_new, gone_full = (uuid.uuid4() for _ in range(5))
    for folder in (tmp_path / str(live_old), tmp_path / live_new.hex, tmp_path / str(gone_old),
                   tmp_path / gone_new.hex, tmp_path / str(gone_full)):
        (folder / "outputs").mkdir(parents=True)
    (tmp_path / str(gone_full) / "outputs" / "result.docx").write_bytes(b"PK")
    store.sweep_unsent(known_sessions={live_old.hex, live_new.hex})
    assert (tmp_path / str(live_old) / "outputs").is_dir() and (tmp_path / live_new.hex / "outputs").is_dir()
    assert not (tmp_path / str(gone_old)).exists() and not (tmp_path / gone_new.hex).exists()
    assert (tmp_path / str(gone_full) / "outputs" / "result.docx").is_file()
