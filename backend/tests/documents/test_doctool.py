"""Unit and integration tests for `hub-doc` (app/doctool) beyond the acceptance contract: the outputs guard's edge
cases, the coordinator's overwrite rule, default names, the careful replace across runs, the diff, the report's
issues, `.xls` edits, PDF page images and the child's memory cap setting."""

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.attachments import make_read_only
from app.doctool import cli, contract
from app.doctool.contract import Failed, Refused
from app.doctool.docx_edit import diff, replace
from app.doctool.docx_report import report
from app.doctool.outputs import OutputsFolder, Staging
from app.doctool.xlsx import parse_range

from support import document_samples as samples
from support.documents import HubDoc, make_session, png_size


@pytest.fixture
def outputs(tmp_path: Path) -> Path:
    folder = tmp_path / "session" / "outputs"
    folder.mkdir(parents=True)
    return folder


# --- The outputs guard ------------------------------------------------------------------------------------------


def test_target_accepts_names_and_subfolders_inside(outputs: Path, tmp_path: Path) -> None:
    folder = OutputsFolder(str(outputs))
    assert folder.target("a.csv", "x.csv", [], (".csv",)) == outputs.resolve() / "a.csv"
    assert folder.target("sub/b.csv", "x.csv", [], (".csv",)) == outputs.resolve() / "sub" / "b.csv"
    assert folder.target(None, "default.csv", [], (".csv",)).name == "default.csv"


@pytest.mark.parametrize("name", ["../x.csv", "..\\x.csv", "C:/x.csv", "/x.csv", "a.csv:stream", "NUL", ".",
                                  "sub/../../x.csv", ".~hub-doc-abc/x.csv"])
def test_target_refuses_everything_that_leaves_the_folder(outputs: Path, name: str) -> None:
    with pytest.raises((Refused, Failed)):
        OutputsFolder(str(outputs)).target(name, "x.csv", [], ())


def test_an_earlier_output_is_replaced_unless_read_only_or_an_input(outputs: Path) -> None:
    """Coordinator's decision: writing a name that exists in outputs overwrites it (the agent's own earlier output);
    a read-only file or the command's own input never."""
    folder = OutputsFolder(str(outputs))
    earlier = outputs / "report (edited).docx"
    earlier.write_text("old")
    assert folder.target("report (edited).docx", "x", [], (".docx",)) == earlier.resolve()
    with pytest.raises(Refused, match="inputs"):
        folder.target("report (edited).docx", "x", [earlier], (".docx",))
    make_read_only(earlier)
    with pytest.raises(Refused, match="read-only"):
        folder.target("report (edited).docx", "x", [], (".docx",))


def test_wrong_extension_is_a_failure_not_a_refusal(outputs: Path) -> None:
    with pytest.raises(Failed, match=".png"):
        OutputsFolder(str(outputs)).target("picture.jpg", "x.png", [], (".png",))


def test_no_or_missing_outputs_folder_means_nothing_is_written(tmp_path: Path) -> None:
    for raw in (None, "", str(tmp_path / "missing")):
        with pytest.raises(Refused, match="outputs folder"):
            OutputsFolder(raw).target("a.csv", "a.csv", [], ())


def test_staging_commits_and_cleans_up(outputs: Path) -> None:
    folder = OutputsFolder(str(outputs))
    staging = Staging(folder)
    staged = staging.file(".txt")
    staged.write_text("done")
    target = folder.target("done.txt", "x", [], ())
    staging.commit(staged, target)
    staging.remove()
    assert [p.name for p in outputs.iterdir()] == ["done.txt"]


def test_memory_limit_can_only_be_lowered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(contract.MEMORY_ENV, str(10 * 1024**3))
    assert contract.memory_limit() == contract.DEFAULT_MEMORY_LIMIT
    monkeypatch.setenv(contract.MEMORY_ENV, str(256 * 1024**2))
    assert contract.memory_limit() == 256 * 1024**2
    monkeypatch.setenv(contract.MEMORY_ENV, "1")
    assert contract.memory_limit() == contract.MIN_MEMORY_LIMIT
    monkeypatch.setenv(contract.MEMORY_ENV, "lots")
    assert contract.memory_limit() == contract.DEFAULT_MEMORY_LIMIT


# --- Parsing ------------------------------------------------------------------------------------------------------


def test_bad_arguments_fail_with_a_hint() -> None:
    with pytest.raises(Failed, match="hub-doc help"):
        cli.parse(["xlsx", "check", "a.xlsx", "--bogus"])
    with pytest.raises(Failed, match="takes 2 files"):
        cli.check(cli.Command("docx", "diff", ["only-one.docx"]))


def test_ranges() -> None:
    assert parse_range("A1:F20") == (1, 1, 20, 6)
    assert parse_range("$B$2") == (2, 2, 2, 2)
    assert parse_range("F20:A1") == (1, 1, 20, 6)
    with pytest.raises(Failed):
        parse_range("Sheet1!A1")


def test_help_and_default_names(hub_doc_exe: str, tmp_path: Path) -> None:
    session = make_session(tmp_path / "attachments")
    tool = HubDoc(hub_doc_exe, session.outputs)
    assert "hub-doc xlsx check" in tool.run("help").data["usage"]
    original = session.original("sales.xlsx", samples.sales_xlsx())
    edited = tool.run("xlsx", "edit", original, "--set", "Sales!F4==1+1")
    assert Path(edited.data["output"]).name == "sales (edited).xlsx"
    picture = tool.run("xlsx", "render", original, "--sheet", "Notes")
    assert Path(picture.data["output"]).name == "sales (Notes).png"
    assert not any(p.name.startswith(contract.STAGING_PREFIX) for p in session.outputs.iterdir())


def test_overwriting_an_earlier_output_through_hub_doc(hub_doc_exe: str, tmp_path: Path) -> None:
    session = make_session(tmp_path / "attachments")
    tool = HubDoc(hub_doc_exe, session.outputs)
    original = session.original("sales.xlsx", samples.sales_xlsx())
    first = tool.xlsx_csv(original, "sales.csv", "Sales")
    second = tool.xlsx_csv(original, "sales.csv", "Notes")
    assert first.code == second.code == 0
    assert Path(second.data["output"]).read_text(encoding="utf-8-sig").startswith("rate")


def test_xls_edit_is_saved_as_xlsx(hub_doc_exe: str, tmp_path: Path) -> None:
    session = make_session(tmp_path / "attachments")
    tool = HubDoc(hub_doc_exe, session.outputs)
    original = session.original("old.xls", samples.old_xls())
    reply = tool.run("xlsx", "edit", original, "--set", "Old!C2==B2*2")
    assert reply.code == 0, reply
    assert reply.data["output"].endswith("old (edited).xlsx")
    assert any(".xls" in w for w in reply.data["warnings"])
    assert tool.run("xlsx", "edit", original, "--out", "x.xls", "--set", "Old!C2=1").code == 2


def test_word_text_not_found_is_a_failure(hub_doc_exe: str, tmp_path: Path) -> None:
    session = make_session(tmp_path / "attachments")
    tool = HubDoc(hub_doc_exe, session.outputs)
    original = session.original("report.docx", samples.formatted_docx())
    reply = tool.docx_replace(original, "x.docx", "no such words", "y")
    assert reply.code == 2 and "wasn't found" in reply.data["error"]


def test_pdf_pages(hub_doc_exe: str, tmp_path: Path) -> None:
    from pypdf import PdfWriter

    session = make_session(tmp_path / "attachments")
    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    writer.add_blank_page(612, 792)
    pdf = session.root / "doc.pdf"
    with open(pdf, "wb") as out:
        writer.write(out)
    tool = HubDoc(hub_doc_exe, session.outputs)
    reply = tool.run("pdf", "pages", pdf, "--max-pages", "1")
    assert reply.code == 0, reply
    assert [Path(p).name for p in reply.data["pages"]] == ["doc (page 1).png"]
    assert reply.data["page_count"] == 2 and reply.data["warnings"]
    width, height = png_size(Path(reply.data["pages"][0]).read_bytes())
    assert (width, height) == (918, 1188)
    assert HubDoc(hub_doc_exe, None).run("pdf", "pages", pdf).code == 1  # nowhere to write


def test_the_child_caps_its_memory_before_reading(tmp_path: Path) -> None:
    """The worker refuses to open anything when it can't be capped (fail closed), like PR 1's extract worker."""
    request = {"command": cli.Command("xlsx", "check", [str(tmp_path / "x.xlsx")]).to_json(), "memory_limit": "x"}
    done = subprocess.run([sys.executable, "-I", "-m", "app.doctool", contract.CHILD_FLAG],
                          input=json.dumps(request).encode(), capture_output=True, timeout=60)
    reply = json.loads(done.stdout.decode().strip().splitlines()[-1])
    assert reply["ok"] is False and "memory limit" in reply["error"]


# --- Word internals ---------------------------------------------------------------------------------------------


def _docx(tmp_path: Path, build) -> Path:
    from docx import Document

    document = Document()
    build(document)
    path = tmp_path / "doc.docx"
    document.save(str(path))
    return path


def test_replace_across_runs_keeps_the_first_runs_formatting(tmp_path: Path) -> None:
    from docx import Document

    def build(document) -> None:
        paragraph = document.add_paragraph()
        first = paragraph.add_run("Total: 12")
        first.bold = True
        paragraph.add_run("0 units")
        paragraph.add_run(" and 120 more")

    source = _docx(tmp_path, build)
    out = tmp_path / "out.docx"
    result = replace(str(source), str(out), [["120", "150"]])
    assert result["replaced"][0]["count"] == 2
    paragraph = Document(str(out)).paragraphs[0]
    assert paragraph.text == "Total: 150 units and 150 more"
    assert paragraph.runs[0].text == "Total: 150" and paragraph.runs[0].bold
    assert paragraph.runs[1].text == " units"


def test_replace_refuses_a_run_with_a_picture_mark(tmp_path: Path) -> None:
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    def build(document) -> None:
        paragraph = document.add_paragraph()
        run = paragraph.add_run("see note")
        run._r.append(parse_xml(f'<w:footnoteReference {nsdecls("w")} w:id="1"/>'))

    with pytest.raises(Refused, match="footnote"):
        replace(str(_docx(tmp_path, build)), str(tmp_path / "out.docx"), [["note", "x"]])


def test_diff_lists_added_and_removed_paragraphs(tmp_path: Path) -> None:
    a = _docx(tmp_path, lambda d: [d.add_paragraph(t) for t in ("one", "two", "three")])
    b_path = tmp_path / "b"
    b_path.mkdir()
    b = _docx(b_path, lambda d: [d.add_paragraph(t) for t in ("one", "three", "four")])
    kinds = [(c["kind"], c["before"], c["after"]) for c in diff(str(a), str(b))["changed"]]
    assert ("removed", "two", "") in kinds and ("added", "", "four") in kinds


def test_report_flags_skipped_heading_levels_and_spacing_by_hand(tmp_path: Path) -> None:
    def build(document) -> None:
        document.add_heading("Top", level=1)
        document.add_heading("Too deep", level=3)
        document.add_paragraph("Body text that is a full sentence.")
        document.add_paragraph("")
        document.add_paragraph("")
        document.add_paragraph("More body text after the gap.")

    result = report(str(_docx(tmp_path, build)))
    kinds = {issue["kind"] for issue in result["issues"]}
    assert {"heading-skip", "empty-paragraphs"} <= kinds
    assert result["paragraphs"][1]["heading_level"] == 3
    assert result["sections"] and result["sections"][0]["page_width"] == 612


def test_tracked_text_is_in_the_report_but_deleted_text_is_marked(tmp_path: Path) -> None:
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    def build(document) -> None:
        paragraph = document.add_paragraph("Kept ")
        paragraph._p.append(parse_xml(f'<w:del {nsdecls("w")} w:id="5" w:author="Bob" w:date="2026-11-01T10:00:00Z">'
                                      '<w:r><w:delText>gone</w:delText></w:r></w:del>'))

    paragraph = report(str(_docx(tmp_path, build)))["paragraphs"][0]
    assert paragraph["text"] == "Kept "
    assert any(r.get("deleted") and r["text"] == "gone" for r in paragraph["runs"])
    assert paragraph["revisions"][0]["author"] == "Bob"


def test_report_counts_paragraphs_in_tables(tmp_path: Path) -> None:
    def build(document) -> None:
        document.add_paragraph("Before")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "A"
        table.cell(0, 1).text = "B"
        document.add_paragraph("After")

    paragraphs = report(str(_docx(tmp_path, build)))["paragraphs"]
    assert [(p["text"], p["in_table"]) for p in paragraphs] == [("Before", False), ("A", True), ("B", True),
                                                                ("After", False)]


def test_saved_copies_keep_every_part(tmp_path: Path, outputs: Path) -> None:
    source = tmp_path / "report.docx"
    source.write_bytes(samples.formatted_docx())
    out = outputs / "r.docx"
    replace(str(source), str(out), [["Closing", "Final"]])
    with zipfile.ZipFile(source) as a, zipfile.ZipFile(out) as b:
        assert set(a.namelist()) == set(b.namelist())
    assert os.path.getsize(out) > 0
