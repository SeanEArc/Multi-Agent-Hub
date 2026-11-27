"""Acceptance tests for revision 4, attachments PR 2 ("working on documents"): the `hub-doc` command.

Written before the feature from `docs/plan.md` (Revision 4, "PR 2: working on documents", including the
"Decided 2026-11-23 with the user, after the live probe" bullets, which win over the older ones) and from the
live probe (`sub-agents/runs/2026-11-21-rev4-attachments-pr2/probe.md`). They test only the outside: `hub-doc` run
as a program, located by the one `hub_doc_exe` fixture (in `conftest.py`).

Each test names the plan bullet it covers in its docstring ("Plan: ...").

The command contract these tests assume (the seams, all in `HubDoc` in `support/documents.py` so a rename is one edit):
- `hub-doc <format> <action> <file> [options]`, formats `xlsx` and `docx`.
- stdout is exactly one JSON object; `ok` is true or false; a false reply has a human `error` string.
- exit code 0 = ok, 1 = refused (a write outside the outputs folder, over an input, over a read-only file, a Word
  paragraph it won't edit), 2 = failed (missing or unreadable file, bad range, out of memory, bad arguments).
- the outputs folder comes from the `HUB_DOC_OUTPUTS` environment variable (the hub sets it for each agent);
  `--out` takes a file name (relative to the outputs folder) or an absolute path inside it; nothing else is written.
- `HUB_DOC_MEMORY_LIMIT` (bytes) lowers the memory cap (tests use it to hit the cap without a huge file).
- reply keys: `output` (absolute path written), `errors` ([{sheet, cell, error}]), `charts` (count), `changed`
  ([{sheet, cell, ...}] for xlsx; [{before, after}] for a docx diff), `width`/`height` (PNG), `markdown` (read),
  `paragraphs`/`styles`/`issues` (the Word formatting report, see `test_word_formatting_report`).
"""

import csv
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.files.attachments import make_read_only

from support import document_samples as samples
from support.documents import HubDoc, Reply, SessionFiles, fingerprint, make_session, png_size, same_file

FILE_ATTRIBUTE_READONLY = 0x1


# --- A session's folder, as PR 1 stores it -------------------------------------------------------------------------


@pytest.fixture
def session(tmp_path: Path) -> SessionFiles:
    return make_session(tmp_path / "attachments")


@pytest.fixture
def hub_doc(hub_doc_exe: str, session: SessionFiles) -> HubDoc:
    return HubDoc(hub_doc_exe, session.outputs)


def error_cells(reply: Reply) -> set[tuple[str, str, str]]:
    return {(e["sheet"], e["cell"], e["error"]) for e in reply.data["errors"]}


def every_file(folder: Path) -> set[Path]:
    return {p for p in folder.rglob("*") if p.is_file()} if folder.exists() else set()


# --- Excel ------------------------------------------------------------------------------------------------------


def test_xlsx_check_recalculates_and_lists_every_error(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: PR 2 > Excel ("every change is recalculated, errors left are listed"); probe s.1: a file written by
    openpyxl has no cached values, so the check must recalculate rather than read stored results."""
    original = session.original("sales.xlsx", samples.sales_xlsx())
    reply = hub_doc.xlsx_check(original)
    assert reply.code == 0, reply
    assert error_cells(reply) == samples.SALES_ERRORS, reply
    assert reply.data["charts"] == 1, reply


def test_xlsx_edit_fixes_errors_keeps_the_chart_and_stores_results(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: PR 2 acceptance ("asked to fix a sheet's errors, the agent returns an edited copy with the error list
    empty ... the original files are unchanged") and Excel ("charts are kept and the saved copy stores the new
    results, so it opens correct anywhere"; "the hub also lists changed cells (values and formulas)")."""
    original = session.original("sales.xlsx", samples.sales_xlsx())
    before = fingerprint(original)

    reply = hub_doc.xlsx_edit(original, "sales (edited).xlsx", samples.SALES_FIXES)
    assert reply.code == 0, reply
    assert reply.data["errors"] == [], reply
    output = Path(reply.data["output"])
    assert same_file(output, session.outputs / "sales (edited).xlsx"), reply
    assert fingerprint(original) == before

    # Charts and drawings kept.
    with zipfile.ZipFile(output) as edited, zipfile.ZipFile(original) as source:
        charts = {n for n in source.namelist() if n.startswith(("xl/charts/", "xl/drawings/"))}
        assert charts and charts <= set(edited.namelist())

    # The results are stored in the file: any reader that doesn't calculate sees the new values.
    from openpyxl import load_workbook

    values = load_workbook(output, data_only=True)["Sales"]
    assert values["E3"].value == 0
    assert values["F4"].value == 2
    assert values["F5"].value == 0
    assert values["D7"].value == pytest.approx(samples.SALES_TOTAL)
    assert values["F2"].value == pytest.approx(28)
    assert isinstance(values["E7"].value, (int, float))  # the propagated #DIV/0! is gone too
    formulas = load_workbook(output)["Sales"]
    assert formulas["E3"].value == "=IF(B3=0,0,D3/B3)"

    # Changed cells: the three edited ones, and E7, whose value changed because E3 did.
    changed = {(c["sheet"], c["cell"]) for c in reply.data["changed"]}
    assert {("Sales", "E3"), ("Sales", "F4"), ("Sales", "F5")} <= changed, reply
    assert ("Sales", "E7") in changed, reply
    assert ("Sales", "D2") not in changed, reply  # unchanged cells aren't listed
    e3 = next(c for c in reply.data["changed"] if c["cell"] == "E3")
    assert "IF(B3=0,0,D3/B3)" in json.dumps(e3) and "#DIV/0!" in json.dumps(e3), e3  # formula and value, before/after

    # A fresh check of the copy agrees.
    assert hub_doc.xlsx_check(output).data["errors"] == []


def test_xlsx_edit_lists_the_errors_it_leaves(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: PR 2 > Excel ("errors left are listed"). A partial fix, or a new mistake, is still a saved copy (exit 0)
    whose reply names what is still wrong, so the agent knows it isn't done."""
    original = session.original("sales.xlsx", samples.sales_xlsx())
    reply = hub_doc.xlsx_edit(original, "partial.xlsx", {"Sales!F4": "=1+1", "Sales!C2": "=1/0"})
    assert reply.code == 0, reply
    errors = error_cells(reply)
    assert ("Sales", "F4", "#REF!") not in errors
    assert {("Sales", "E3", "#DIV/0!"), ("Sales", "F5", "#N/A"), ("Sales", "C2", "#DIV/0!")} <= errors, reply
    assert ("Sales", "D2", "#DIV/0!") in errors  # D2 = B2*C2 now fails too: recalculated, not just the edited cells


def test_xlsx_read_gives_an_agent_readable_view(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: PR 2 > Toolkit / Excel: agents read workbooks through the toolkit (computed values, not formulas only)."""
    original = session.original("sales.xlsx", samples.sales_xlsx())
    reply = hub_doc.xlsx_read(original, sheet="Sales")
    assert reply.code == 0, reply
    text = reply.data["markdown"]
    assert "North" in text and "Revenue" in text
    assert "71" in text  # D7's computed total
    narrow = hub_doc.xlsx_read(original, sheet="Sales", cells="A1:B3")
    assert narrow.code == 0 and "South" in narrow.data["markdown"] and "West" not in narrow.data["markdown"], narrow


def test_xlsx_sheet_and_range_pngs(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: PR 2 > Excel ("sheet or range PNGs let an agent look at it")."""
    original = session.original("sales.xlsx", samples.sales_xlsx())
    whole = hub_doc.xlsx_render(original, "sales.png", "Sales")
    assert whole.code == 0, whole
    assert same_file(whole.data["output"], session.outputs / "sales.png")
    full_size = png_size(Path(whole.data["output"]).read_bytes())
    assert full_size == (whole.data["width"], whole.data["height"]) and min(full_size) > 0

    part = hub_doc.xlsx_render(original, "corner.png", "Sales", cells="A1:B3")
    assert part.code == 0, part
    part_size = png_size(Path(part.data["output"]).read_bytes())
    assert part_size[0] < full_size[0] and part_size[1] < full_size[1]

    bad = hub_doc.xlsx_render(original, "bad.png", "Sales", cells="ZZZ")
    assert bad.code == 2, bad
    assert not (session.outputs / "bad.png").exists()


def test_xlsx_csv_exports_computed_values(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: Decided 2026-11-23 ("No pandas ... hub-doc exports computed values as CSV instead")."""
    original = session.original("sales.xlsx", samples.sales_xlsx())
    reply = hub_doc.xlsx_csv(original, "sales.csv", "Sales")
    assert reply.code == 0, reply
    output = Path(reply.data["output"])
    assert same_file(output, session.outputs / "sales.csv")
    rows = list(csv.reader(io.StringIO(output.read_text(encoding="utf-8-sig"))))
    assert rows[0][:5] == ["Region", "Units", "Price", "Revenue", "Share"]
    north = next(r for r in rows if r and r[0] == "North")
    assert float(north[3]) == 25  # =B2*C2, computed
    total = next(r for r in rows if r and r[0] == "Total")
    assert float(total[3]) == pytest.approx(samples.SALES_TOTAL)
    assert not any(cell.startswith("=") for row in rows for cell in row), "formulas instead of values"


def test_xls_is_read_through_the_fallback(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: Decided 2026-11-23 ("openpyxl only to read .xls: BetterOffice ... refuses only .xls"). The file is a
    real BIFF8 workbook (BetterOffice: `ParseError: bad zip`; xlrd reads it), so this fails unless hub-doc falls back."""
    original = session.original("old.xls", samples.old_xls())
    before = fingerprint(original)
    reply = hub_doc.xlsx_read(original)
    assert reply.code == 0, reply
    assert "plum" in reply.data["markdown"] and "9" in reply.data["markdown"], reply
    exported = hub_doc.xlsx_csv(original, "old.csv", samples.OLD_XLS_SHEET)
    assert exported.code == 0, exported
    rows = list(csv.reader(io.StringIO(Path(exported.data["output"]).read_text(encoding="utf-8-sig"))))
    assert rows[0] == ["name", "qty"] and rows[1][0] == "plum" and float(rows[1][1]) == 9
    assert fingerprint(original) == before


# --- Word ----------------------------------------------------------------------------------------------------------


def _paragraph(report: dict, contains: str) -> tuple[int, dict]:
    for index, paragraph in enumerate(report["paragraphs"]):
        if contains in paragraph["text"]:
            return index, paragraph
    raise AssertionError(f"no paragraph with {contains!r} in the report: {[p['text'] for p in report['paragraphs']]}")


def test_word_formatting_report(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: PR 2 > Word ("a formatting report (always, no Office) ... styles per paragraph, fonts and sizes,
    spacing, list numbering, direct formatting instead of styles, tracked changes, inconsistencies") and Decided
    2026-11-23 ("Formatting report from two sources": BetterOffice for styles, numbering, links, tracked changes;
    python-docx for fonts, sizes and spacing, which BetterOffice's export lacks).

    Report shape assumed: `paragraphs` in document order, each {text, style, alignment, space_before, space_after
    (points), runs: [{text, font, size (points), bold, italic}], list: {format} or null, revisions: [{kind,
    author}]}; `styles`: [{name, font, size}] (inherited values come from here); `issues`: [{paragraph (index into
    paragraphs), message}]."""
    original = session.original("report.docx", samples.formatted_docx())
    before = fingerprint(original)
    reply = hub_doc.docx_report(original)
    assert reply.code == 0, reply
    report = reply.data

    # Styles per paragraph.
    _, heading = _paragraph(report, "Quarterly report")
    assert "heading" in str(heading["style"]).lower() and "1" in str(heading["style"]), heading
    heading_style = next(s for s in report["styles"] if str(s["name"]).replace(" ", "").lower() == "heading1")
    assert heading_style["size"] == 14, heading_style  # inherited size: from the style definition (python-docx)

    # Direct run formatting: font and size (BetterOffice's object API / python-docx).
    _, plain = _paragraph(report, samples.PLAIN_NUMBER)
    run = next(r for r in plain["runs"] if samples.PLAIN_NUMBER in r["text"])
    assert (run["font"], run["size"], run["bold"]) == ("Arial", 16, True), run
    _, closing = _paragraph(report, "Closing paragraph")
    run = next(r for r in closing["runs"] if "Times" in r["text"])
    assert (run["font"], run["italic"]) == ("Times New Roman", True), run

    # Spacing and alignment (python-docx: BetterOffice's export has no spacing at all).
    fake_index, fake = _paragraph(report, samples.FAKE_HEADING)
    assert fake["space_after"] == 18, fake
    assert str(fake["alignment"]).lower() == "center", fake
    assert any(r["bold"] and r["size"] == 14 for r in fake["runs"]), fake
    assert "heading" not in str(fake["style"]).lower(), fake

    # List numbering.
    assert _paragraph(report, "First bullet")[1]["list"]["format"] == "bullet"
    assert _paragraph(report, "Step two")[1]["list"]["format"] == "decimal"
    assert _paragraph(report, "Revenue was")[1]["list"] is None

    # Tracked changes, with their author (BetterOffice; python-docx can't even see the inserted text).
    _, tracked = _paragraph(report, "Tracked:")
    assert {"kind": "insertion", "author": samples.TRACKED_AUTHOR}.items() <= tracked["revisions"][0].items(), tracked
    assert samples.TRACKED_WORD in json.dumps(report)

    # Inconsistencies: direct formatting standing in for a heading style is called out.
    assert any(issue["paragraph"] == fake_index and issue["message"] for issue in report["issues"]), report["issues"]

    assert fingerprint(original) == before


def test_word_read_shows_tracked_changes(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: Decided 2026-11-23 (BetterOffice's Word export has "links and tracked changes"); probe s.5: an agent
    reading through python-docx misses text inside `w:ins`, so the toolkit's reading view must show it with its
    author."""
    original = session.original("report.docx", samples.formatted_docx())
    reply = hub_doc.docx_read(original)
    assert reply.code == 0, reply
    text = reply.data["markdown"]
    assert samples.TRACKED_WORD in text and samples.TRACKED_AUTHOR in text
    assert "Quarterly report" in text and "https://example.com/" in text


def _document_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as document:
        return document.read("word/document.xml").decode("utf-8")


def test_word_replace_keeps_run_formatting_and_everything_else(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: Decided 2026-11-23 ("Word edits: a careful replace inside runs that keeps their formatting"). Probe s.5:
    python-docx's `paragraph.text = ...` would flatten the bold Arial run; links, fields and tracked changes in
    other paragraphs must survive too."""
    original = session.original("report.docx", samples.formatted_docx())
    before = fingerprint(original)
    reply = hub_doc.docx_replace(original, "report (edited).docx", samples.PLAIN_NUMBER, "150")
    assert reply.code == 0, reply
    output = Path(reply.data["output"])
    assert same_file(output, session.outputs / "report (edited).docx")
    assert fingerprint(original) == before

    from docx import Document

    edited = Document(str(output))
    paragraph = next(p for p in edited.paragraphs if p.text.startswith("Revenue was"))
    assert paragraph.text == "Revenue was 150 million this quarter."
    run = next(r for r in paragraph.runs if "150" in r.text)
    assert run.bold and run.font.name == "Arial" and run.font.size.pt == 16

    xml, source_xml = _document_xml(output), _document_xml(original)
    for marker in ("<w:hyperlink", "<w:fldSimple", "<w:ins ", samples.TRACKED_WORD, samples.LINK_WORD):
        assert xml.count(marker) == source_xml.count(marker), marker
    with zipfile.ZipFile(output) as a, zipfile.ZipFile(original) as b:
        assert set(a.namelist()) == set(b.namelist())  # no part lost or added


@pytest.mark.parametrize("word, reason", [(samples.LINK_WORD, "link"), (samples.FIELD_WORD, "field"),
                                          (samples.TRACKED_WORD, "track")])
def test_word_replace_refuses_paragraphs_it_would_damage(hub_doc: HubDoc, session: SessionFiles, word: str,
                                                         reason: str) -> None:
    """Plan: Decided 2026-11-23 (the careful replace "refuses paragraphs with links, fields or tracked changes, since
    python-docx's paragraph.text silently drops them")."""
    original = session.original("report.docx", samples.formatted_docx())
    reply = hub_doc.docx_replace(original, "edited.docx", word, "CHANGED")
    assert reply.code == 1, reply
    assert reason in reply.data["error"].lower(), reply
    assert not (session.outputs / "edited.docx").exists()


def test_word_diff_lists_changed_paragraphs(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: PR 2 > Word ("Showing edits: ... otherwise the hub lists the changed paragraphs"), which works with no
    Office."""
    original = session.original("report.docx", samples.formatted_docx())
    edited = hub_doc.docx_replace(original, "edited.docx", samples.PLAIN_NUMBER, "150")
    assert edited.code == 0, edited
    reply = hub_doc.docx_diff(original, Path(edited.data["output"]))
    assert reply.code == 0, reply
    assert len(reply.data["changed"]) == 1, reply
    change = reply.data["changed"][0]
    assert "120" in change["before"] and "150" in change["after"], change


# --- The outputs folder: the only place hub-doc writes -------------------------------------------------------------


def _escapes(session: SessionFiles, tmp_path: Path, original: Path, ext: str) -> dict[str, tuple[str, Path]]:
    """--out values that must be refused, with the file each would have created."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    other = make_session(session.root.parent)  # another session's outputs
    return {
        "parent": (f"../escape{ext}", session.root / f"escape{ext}"),
        "parent-backslash": (f"..\\escape{ext}", session.root / f"escape{ext}"),
        "dotdot-inside": (f"sub/../../escape{ext}", session.root / f"escape{ext}"),
        "absolute-elsewhere": (str(elsewhere / f"x{ext}"), elsewhere / f"x{ext}"),
        "other-session": (str(other.outputs / f"x{ext}"), other.outputs / f"x{ext}"),
        "other-session-relative": (f"../../{other.root.name}/outputs/x{ext}", other.outputs / f"x{ext}"),
        "next-to-the-original": (str(original.parent / f"copy{ext}"), original.parent / f"copy{ext}"),
    }


ESCAPES = ["parent", "parent-backslash", "dotdot-inside", "absolute-elsewhere", "other-session",
           "other-session-relative", "next-to-the-original"]


@pytest.mark.parametrize("case", ESCAPES)
def test_writes_outside_the_outputs_folder_are_refused(hub_doc: HubDoc, session: SessionFiles, tmp_path: Path,
                                                       case: str) -> None:
    """Plan: Decided 2026-11-23 ("One hub-doc command ... writes only to the session's outputs folder"); probe s.2:
    an allowed command can write anywhere the agent's user can, so hub-doc enforces the rule itself."""
    original = session.original("sales.xlsx", samples.sales_xlsx())
    before = fingerprint(original)
    commands = [(".xlsx", lambda out: hub_doc.xlsx_edit(original, out, {"Sales!F4": "=1+1"})),
                (".csv", lambda out: hub_doc.xlsx_csv(original, out, "Sales")),
                (".png", lambda out: hub_doc.xlsx_render(original, out, "Sales"))]
    for ext, command in commands:
        out, target = _escapes(session, tmp_path, original, ext)[case]
        existed = target.exists()
        everything = every_file(tmp_path)
        reply = command(out)
        assert reply.code == 1, (ext, reply)
        assert existed or not target.exists()
        assert every_file(tmp_path) == everything  # nothing written anywhere
    assert fingerprint(original) == before


def test_the_original_itself_is_refused_as_an_output(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: PR 2 > Outputs folder ("never over the original")."""
    original = session.original("sales.xlsx", samples.sales_xlsx())
    before = fingerprint(original)
    reply = hub_doc.xlsx_edit(original, original, samples.SALES_FIXES)
    assert reply.code == 1, reply
    assert fingerprint(original) == before


def test_a_junction_inside_outputs_doesnt_lead_out(hub_doc: HubDoc, session: SessionFiles, tmp_path: Path) -> None:
    """Plan: Decided 2026-11-23 (writes only to the outputs folder). An agent with a shell can make a junction in
    outputs pointing elsewhere; the check must follow it."""
    if os.name != "nt":
        pytest.skip("junctions are Windows-only")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = session.outputs / "link"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)], capture_output=True)
    assert made.returncode == 0, made.stderr
    try:
        original = session.original("sales.xlsx", samples.sales_xlsx())
        reply = hub_doc.xlsx_csv(original, "link/x.csv", "Sales")
        assert reply.code == 1, reply
        assert not any(elsewhere.iterdir())
    finally:
        os.rmdir(link)  # removes the junction only


def test_relative_and_absolute_names_inside_outputs_work(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: PR 2 > Outputs folder ("Agents write new files there (`report (edited).docx`)"). Agents are told the
    folder's full path, so an absolute path inside it is as good as a bare name."""
    original = session.original("sales.xlsx", samples.sales_xlsx())
    by_name = hub_doc.xlsx_csv(original, "a.csv", "Sales")
    by_path = hub_doc.xlsx_csv(original, session.outputs / "b.csv", "Sales")
    assert by_name.code == 0 and by_path.code == 0, (by_name, by_path)
    assert {p.name for p in session.outputs.iterdir()} >= {"a.csv", "b.csv"}


def test_never_writes_over_its_own_input_or_a_read_only_file(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: Decided 2026-11-23 / PR 2 > Outputs folder ("never over the original"); probe s.2 ("never to a path
    equal to an input ... never changes a read-only file"). Inputs can be earlier outputs, so the rule holds inside
    outputs too."""
    original = session.original("sales.xlsx", samples.sales_xlsx())
    first = hub_doc.xlsx_edit(original, "step1.xlsx", {"Sales!F4": "=1+1"})
    assert first.code == 0, first
    step1 = Path(first.data["output"])
    saved = fingerprint(step1)
    for out in ("step1.xlsx", step1):
        again = hub_doc.xlsx_edit(step1, out, {"Sales!F5": "=0"})
        assert again.code == 1, again
    assert fingerprint(step1) == saved

    locked = session.outputs / "locked.csv"
    locked.write_text("keep me", encoding="utf-8")
    make_read_only(locked)
    reply = hub_doc.xlsx_csv(original, "locked.csv", "Sales")
    assert reply.code == 1, reply
    assert locked.read_text(encoding="utf-8") == "keep me"


def test_without_an_outputs_folder_nothing_is_written_but_reading_works(hub_doc_exe: str, session: SessionFiles,
                                                                        tmp_path: Path) -> None:
    """Plan: Decided 2026-11-23 (writes only to the session's outputs folder): with none set (hub-doc run outside a
    hub session) there is nowhere it may write; read-only commands still answer."""
    tool = HubDoc(hub_doc_exe, outputs=None)
    original = session.original("sales.xlsx", samples.sales_xlsx())
    everything = every_file(tmp_path)
    assert tool.xlsx_check(original).code == 0
    refused = tool.xlsx_csv(original, str(tmp_path / "x.csv"), "Sales")
    assert refused.code == 1, refused
    assert every_file(tmp_path) == everything


def test_originals_are_never_changed_by_any_command(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: PR 2 acceptance ("the original files are unchanged"); probe s.3 suggested checking SHA-256, size, the
    read-only attribute and the Zone.Identifier."""
    sheet = session.original("sales.xlsx", samples.sales_xlsx())
    document = session.original("report.docx", samples.formatted_docx())
    old = session.original("old.xls", samples.old_xls())
    before = {p: fingerprint(p) for p in (sheet, document, old)}
    hub_doc.xlsx_check(sheet)
    hub_doc.xlsx_read(sheet)
    hub_doc.xlsx_edit(sheet, "e.xlsx", samples.SALES_FIXES)
    hub_doc.xlsx_render(sheet, "s.png", "Sales")
    hub_doc.xlsx_csv(sheet, "s.csv", "Sales")
    hub_doc.xlsx_read(old)
    hub_doc.docx_report(document)
    hub_doc.docx_read(document)
    hub_doc.docx_replace(document, "d.docx", samples.PLAIN_NUMBER, "150")
    hub_doc.docx_replace(document, "refused.docx", samples.LINK_WORD, "x")
    assert {p: fingerprint(p) for p in before} == before
    assert before[sheet]["read_only"] and (os.name != "nt" or before[sheet]["zone"])


# --- Failures are JSON too, and memory is capped -------------------------------------------------------------------


def test_failures_are_json_with_exit_code_2(hub_doc: HubDoc, session: SessionFiles, tmp_path: Path) -> None:
    """Plan: Decided 2026-11-23 ("It replies in JSON"): a missing file, a file that isn't what it says and an unknown
    action all answer with one JSON object, never a traceback."""
    missing = hub_doc.xlsx_check(tmp_path / "nope.xlsx")
    assert missing.code == 2 and "nope.xlsx" in missing.data["error"], missing
    broken = session.original("broken.xlsx", b"PK\x03\x04 not really a zip")
    assert hub_doc.xlsx_check(broken).code == 2
    assert hub_doc.run("xlsx", "explode", broken).code == 2
    assert hub_doc.run("pptx", "check", broken).code == 2


def test_memory_cap_is_real(hub_doc: HubDoc, session: SessionFiles) -> None:
    """Plan: Decided 2026-11-23 ("It replies in JSON, is memory-capped"). Not mocked: a real 20,000-row workbook
    (BetterOffice peaked at 248 MB committed on it, probe s.1) under a 128 MB cap. Found while writing these tests
    (appended to probe.md): BetterOffice is Rust, and running out of memory under a job-object cap ABORTS the
    process ("memory allocation of N bytes failed", no Python exception), so hub-doc must watch the capped work from
    outside to still answer in JSON."""
    original = session.original("big.xlsx", samples.big_xlsx())
    capped = hub_doc.run("xlsx", "edit", original, "--out", "big (edited).xlsx", "--set", "Data!H2==1",
                         env={"HUB_DOC_MEMORY_LIMIT": str(128 * 1024 * 1024)})
    assert capped.code == 2, capped
    assert any(word in capped.data["error"].lower() for word in ("memory", "too large")), capped
    assert not (session.outputs / "big (edited).xlsx").exists()  # no partial file

    # The same work fits under the default cap, so it was the cap that stopped it.
    fine = hub_doc.xlsx_check(original)
    assert fine.code == 0 and fine.data["errors"] == [], fine
