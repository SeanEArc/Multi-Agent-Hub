"""Opt-in: revision 4 PR 2 with the real Microsoft Office on this PC. Run with `$env:HUB_LIVE_OFFICE="1"`.

Free (local Office, no agent). Starts hidden Word and Excel through the hub's own check and hub-doc; needs a
licensed Office (probe 2026-11-21: Microsoft 365 Apps 16.0.20527 passed the test conversion). Close your own Word
and Excel documents first: the tests refuse to run while Office is already open, so a cleanup bug can't cost work.

Plan: PR 2 > Office detection, Word ("Page images only when licensed Office passes the test conversion: Word saves
a PDF ... Codex and the local model get page images"; "Showing edits: Word's Compare makes a tracked-changes copy
with the agent as author"), Decided 2026-11-23 ("Page images: pypdfium2"; "Compare sets the author with
RevisedAuthor"; cleanup kills only automation-started Office).

Seams assumed: `GET/POST /api/office[/check]` as in test_documents_acceptance_office.py; `hub-doc docx pages <file>`
-> {pdf, pages: [png paths]} and `hub-doc docx compare <original> <edited> --author <name> --out <name>` ->
{output}, both writing only to the outputs folder.
"""

import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from app.main import create_app

from support import document_samples as samples
from support.documents import HubDoc, fingerprint, make_session, png_size, same_file
from support.hub import TEST_TOKEN, fake_adapter_factory, make_client, opened_files

pytestmark = pytest.mark.skipif(os.environ.get("HUB_LIVE_OFFICE") != "1", reason="set HUB_LIVE_OFFICE=1 to use real Office")


def office_processes() -> set[tuple[int, str]]:
    script = ("Get-CimInstance Win32_Process -Filter \"Name='WINWORD.EXE' or Name='EXCEL.EXE'\" | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], capture_output=True, text=True,
                         timeout=60).stdout
    return {(int(pid), line) for pid, _, line in (row.partition("\t") for row in out.splitlines() if row.strip())}


@pytest.fixture(autouse=True)
def no_office_open() -> None:
    if office_processes():
        pytest.skip("Word or Excel is already running; close it first")


def wait_for_office_to_close(seconds: float = 15) -> set:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        left = office_processes()
        if not left:
            return set()
        time.sleep(0.5)
    return office_processes()


def test_real_office_is_found_and_nothing_is_left_running(engine: Engine, tmp_path: Path) -> None:
    app = create_app(engine, fake_adapter_factory, token=TEST_TOKEN, worktrees_dir=tmp_path / "w",
                     chats_dir=tmp_path / "c", codex_home=tmp_path / "h", skills_dir=tmp_path / "s",
                     claude_home=tmp_path / "ch", discover_native=False, attachments_dir=tmp_path / "a",
                     open_file=opened_files.append_call)
    with make_client(app) as client:
        response = client.post("/api/office/check")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "found", body
        assert str(body["version"]).startswith("16."), body
    assert wait_for_office_to_close() == set(), "the Office check left Word or Excel running"


def test_word_pages_and_compare(hub_doc_exe: str, tmp_path: Path) -> None:  # noqa: F811
    session = make_session(tmp_path / "attachments")
    tool = HubDoc(hub_doc_exe, session.outputs)
    original = session.original("report.docx", samples.formatted_docx())  # read-only, with the Mark of the Web
    before = fingerprint(original)

    pages = tool.run("docx", "pages", original)
    assert pages.code == 0, pages
    pdf = Path(pages.data["pdf"])
    assert pdf.read_bytes().startswith(b"%PDF-") and pdf.parent.samefile(session.outputs)
    assert pages.data["pages"], pages
    for page in pages.data["pages"]:
        assert Path(page).parent.samefile(session.outputs)
        width, height = png_size(Path(page).read_bytes())
        assert width > 300 and height > width  # a portrait page

    edited = tool.docx_replace(original, "report (edited).docx", samples.PLAIN_NUMBER, "150")
    assert edited.code == 0, edited
    compared = tool.run("docx", "compare", original, edited.data["output"], "--author", "Claude",
                        "--out", "report (changes).docx")
    assert compared.code == 0, compared
    output = Path(compared.data["output"])
    assert same_file(output, session.outputs / "report (changes).docx")
    with zipfile.ZipFile(output) as document:
        xml = document.read("word/document.xml").decode("utf-8")
    assert 'w:author="Claude"' in xml and ("<w:ins " in xml or "<w:del " in xml)

    assert fingerprint(original) == before
    assert wait_for_office_to_close() == set(), "hub-doc left Word running"
