"""The hub's side of revision 4 PR 2: what adapters do with `DocumentAccess`, the hub-doc launcher copy, the outputs
store behind the API, the Office detector's parts, and the session folder name."""

import asyncio
import json
import os
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.codex import CodexAdapter
from app.adapters.local_tools import LocalTools, ToolError
from app.attachments import AttachmentError, AttachmentStore, openable_name
from app.doctool import office, winproc
from app.documents import CLAUDE_RULES, DocumentAccess, DocumentToolkit, prepare_bin
from app.office import OfficeDetector
from app.outputs import OutputsStore
from conftest import FAKE_CODEX


def access(tmp_path: Path, office_found: bool = False, toolkit: bool = True) -> DocumentAccess:
    outputs = tmp_path / "attachments" / uuid.uuid4().hex / "outputs"
    outputs.mkdir(parents=True)
    return DocumentAccess(outputs=outputs, bin_dir=tmp_path / "bin" if toolkit else None, office=office_found)


# --- DocumentAccess ---------------------------------------------------------------------------------------------


def test_env_puts_hub_doc_first_and_names_the_outputs_folder(tmp_path: Path) -> None:
    given = access(tmp_path)
    env = given.env()
    assert env["HUB_DOC_OUTPUTS"] == str(given.outputs)
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / "bin")
    assert "PATH" not in access(tmp_path, toolkit=False).env()


def test_instructions_offer_office_commands_only_when_office_was_found(tmp_path: Path) -> None:
    without = access(tmp_path).instructions("Claude")
    assert "hub-doc xlsx edit" in without and "docx pages" not in without
    with_office = access(tmp_path, office_found=True).instructions("Codex")
    assert "docx pages" in with_office and "--author Codex" in with_office
    bare = access(tmp_path, toolkit=False).instructions("Claude")
    assert "outputs folder" in bare and "hub-doc" not in bare


def test_codex_writable_root_is_valid_toml_for_any_path(tmp_path: Path) -> None:
    folder = tmp_path / "O'Brien – résumé" / "outputs"
    folder.mkdir(parents=True)
    adapter = CodexAdapter(tmp_path, command=[sys.executable, str(FAKE_CODEX), "ok"])
    adapter.documents = DocumentAccess(outputs=folder)
    options = adapter._options()
    value = options[options.index(next(o for o in options if o.startswith("sandbox_workspace_write")))]
    assert tomllib.loads("v = " + value.split("=", 1)[1])["v"] == [str(folder)]
    # A summary is read-only and gets no writable root; the app server gets the same root as a turn.
    assert not any(o.startswith("sandbox_workspace_write") for o in adapter._options("read-only", "x"))
    assert any(o.startswith("sandbox_workspace_write") for o in adapter.server_config())


def test_claude_pre_approves_hub_doc_only_when_it_is_installed(tmp_path: Path) -> None:
    from app.adapters.claude import ClaudeAdapter

    seen: list[list[str]] = []

    class Recorder:
        async def start(self, command, cwd, env):
            seen.append(list(command))
            raise RuntimeError("stop here")

    for toolkit in (True, False):
        adapter = ClaudeAdapter(tmp_path, command=["claude.exe"], runner=Recorder())
        adapter.documents = access(tmp_path, toolkit=toolkit)

        async def run() -> None:
            with pytest.raises(RuntimeError):
                async for _ in adapter.start_task("hi"):
                    pass

        asyncio.run(run())
        args = seen[-1]
        assert (args.count("--allowedTools") == 1) is toolkit
        if toolkit:
            start = args.index("--allowedTools") + 1
            assert tuple(args[start:start + 2]) == CLAUDE_RULES
        # The outputs folder is readable and writable even without the attachments folder as an --add-dir.
        assert str(adapter.documents.outputs) in args


def test_local_tools_write_root(tmp_path: Path) -> None:
    work, outputs, elsewhere = tmp_path / "work", tmp_path / "out", tmp_path / "elsewhere"
    for folder in (work, outputs, elsewhere):
        folder.mkdir()
    tools = LocalTools(work, write_roots=[outputs])
    assert tools.resolve(str(outputs / "new.txt"), writing=True) == (outputs / "new.txt").resolve()
    with pytest.raises(ToolError):
        tools.resolve(str(elsewhere / "x.txt"), writing=True)
    if os.name == "nt":  # a junction inside outputs leads elsewhere: refused
        subprocess.run(["cmd", "/c", "mklink", "/J", str(outputs / "link"), str(elsewhere)], capture_output=True,
                       check=True)
        try:
            with pytest.raises(ToolError):
                tools.resolve(str(outputs / "link" / "x.txt"), writing=True)
        finally:
            os.rmdir(outputs / "link")


# --- The launcher copy ------------------------------------------------------------------------------------------


def test_prepare_bin_copies_the_launcher_once_and_never_python(tmp_path: Path) -> None:
    bin_dir = prepare_bin(tmp_path / "bin")
    assert bin_dir == tmp_path / "bin"
    launcher = bin_dir / ("hub-doc.exe" if os.name == "nt" else "hub-doc")
    assert launcher.is_file() and not (bin_dir / "python.exe").exists()
    stamp = launcher.stat().st_mtime_ns
    assert prepare_bin(tmp_path / "bin") == bin_dir and launcher.stat().st_mtime_ns == stamp  # unchanged: no copy
    found = subprocess.run([str(launcher), "help"], capture_output=True, timeout=60)
    assert json.loads(found.stdout)["ok"] is True


def test_prepare_bin_without_the_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-venv" / "python.exe"))
    assert prepare_bin(tmp_path / "bin") is None
    toolkit = DocumentToolkit(tmp_path / "bin")
    toolkit.install()
    assert toolkit.access(tmp_path).toolkit is False


# --- The outputs store ------------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> tuple[OutputsStore, str]:
    session = uuid.uuid4().hex
    outputs = OutputsStore(AttachmentStore(tmp_path / "attachments"))
    outputs.folder(session, create=True)
    return outputs, session


def test_list_hides_staging_and_includes_subfolders(store: tuple[OutputsStore, str]) -> None:
    outputs, session = store
    folder = outputs.folder(session)
    (folder / "a.txt").write_text("a")
    (folder / "sub").mkdir()
    (folder / "sub" / "b.csv").write_text("b")
    (folder / ".~hub-doc-123").mkdir()
    (folder / ".~hub-doc-123" / "partial.xlsx").write_text("p")
    assert [f.name for f in outputs.list(session)] == ["a.txt", "sub/b.csv"]
    assert outputs.file(session, "sub/b.csv").name == "b.csv"
    with pytest.raises(AttachmentError):
        outputs.file(session, ".~hub-doc-123/partial.xlsx")


def test_save_copy_never_into_the_attachments_folder(store: tuple[OutputsStore, str], tmp_path: Path) -> None:
    outputs, session = store
    (outputs.folder(session) / "r.docx").write_bytes(b"PK")
    with pytest.raises(AttachmentError) as refused:
        outputs.save_copy(session, "r.docx", str(tmp_path / "attachments" / "other" / "x.docx"))
    assert refused.value.status == 403
    with pytest.raises(AttachmentError):
        outputs.save_copy(session, "r.docx", "relative.docx")
    with pytest.raises(AttachmentError):
        outputs.save_copy(session, "r.docx", str(tmp_path / "no-folder" / "x.docx"))
    saved = outputs.save_copy(session, "r.docx", str(tmp_path / "copy.docx"))
    assert saved.read_bytes() == b"PK"


def test_openable_names() -> None:
    assert openable_name("report (edited).docx") and openable_name("sales.csv") and openable_name("notes.md")
    assert not openable_name("fix.ps1") and not openable_name("macros.xlsm") and not openable_name("tool.exe")


def test_session_folder_is_the_stored_id_and_old_dashed_folders_are_kept(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    new = uuid.uuid4()
    assert store.session_dir(str(new)) == tmp_path / new.hex == store.session_dir(new.hex)
    old = uuid.uuid4()
    (tmp_path / str(old)).mkdir()  # made by a PR 1 development build
    assert store.session_dir(old.hex) == tmp_path / str(old)


def test_sweep_removes_an_empty_outputs_folder_of_a_deleted_session(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    gone, known = uuid.uuid4().hex, uuid.uuid4().hex
    for session in (gone, known):
        (tmp_path / session / "outputs").mkdir(parents=True)
    store.sweep_unsent(known_sessions={known})
    assert not (tmp_path / gone).exists() and (tmp_path / known / "outputs").exists()


# --- Office -----------------------------------------------------------------------------------------------------


def test_automation_flag_pattern() -> None:
    assert office.AUTOMATION.search('"C:\\Office\\WINWORD.EXE" /Automation -Embedding')
    assert office.AUTOMATION.search("EXCEL.EXE -Embedding")
    assert not office.AUTOMATION.search('"C:\\Office\\WINWORD.EXE" /n "C:\\docs\\Embedding notes.docx"')


@pytest.mark.skipif(os.name != "nt", reason="Windows process APIs")
def test_command_lines_are_read_without_powershell() -> None:
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "/Automation", "-Embedding"])
    try:
        found = {}
        for _ in range(50):  # in a venv, python.exe is a launcher: its child is the interpreter, same image name
            found = {pid: winproc.command_line(pid) for pid in winproc.processes({"PYTHON.EXE"})}
            if any(line and "-Embedding" in line for line in found.values()):
                break
        assert sleeper.pid in found and office.AUTOMATION.search(found[sleeper.pid] or "")
    finally:
        sleeper.kill()
    assert winproc.command_line(999_999_999) is None


def test_parse_answer() -> None:
    assert office.parse_answer(b'noise\n{"ok": true, "version": "16.0"}\n', b"")["version"] == "16.0"
    assert office.parse_answer(b"New-Object : failed", b"")["ok"] is False
    assert office.parse_answer(b'{"no": "ok key"}', b"")["ok"] is False


def test_the_check_switched_off_never_runs_and_reports_not_found(tmp_path: Path) -> None:
    detector = OfficeDetector([str(tmp_path / "would-fail.exe")], enabled=False)
    assert asyncio.run(detector.check())["status"] == "not found"
    assert "HUB_OFFICE_CHECK" in detector.snapshot()["error"]


def test_office_routes_need_the_token(client: TestClient) -> None:
    anonymous = TestClient(client.app, base_url=str(client.base_url))
    assert anonymous.get("/api/office").status_code in (401, 403)
    assert anonymous.post("/api/office/check").status_code in (401, 403)
    assert client.get("/api/office").json()["status"] in ("checking", "not found")
