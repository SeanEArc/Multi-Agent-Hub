"""Revision 4 PR 2, QA round 2 fixes: a lean Excel walk (S2b), the hub's Office lock folder passed to every agent
(S5), honest busy and lock-folder messages (S6), and a missing read limit under the hub not widening reads."""

import os
import time
from pathlib import Path

import pytest

import document_samples as samples
from app.doctool import office, office_registry, xlsx
from app.doctool.contract import Failed
from app.documents import DocumentAccess
from test_documents_acceptance import HubDoc, hub_doc_exe, make_session  # noqa: F401


# --- S2b: the lean walk -------------------------------------------------------------------------------------------


@pytest.fixture
def sales(tmp_path: Path) -> Path:
    path = tmp_path / "sales.xlsx"
    path.write_bytes(samples.sales_xlsx())
    return path


def test_the_walk_limit_is_a_million_cells_and_bands_stay_small() -> None:
    assert xlsx.FILLED_LIMIT == 1_000_000
    assert xlsx.EXPORT_CELLS <= 20_000  # 100,000-cell bands held ~450 MB more at 1.5M cells


def test_check_edit_and_csv_never_take_a_full_snapshot(sales: Path, tmp_path: Path, monkeypatch) -> None:
    """A full snapshot cost ~760 MB per million cells and aborted big workbooks under the 2 GB cap."""
    def refuse(self):
        raise AssertionError("a full snapshot was taken")

    monkeypatch.setattr(xlsx.Book, "snapshot", refuse)
    checked = xlsx.Book.open(str(sales)).check()
    assert {(e["cell"], e["error"]) for e in checked["errors"]} == {c[1:] for c in samples.SALES_ERRORS}
    edited = xlsx.Book.open(str(sales)).edit(list(f"{k}={v}" for k, v in samples.SALES_FIXES.items()),
                                             str(tmp_path / "fixed.xlsx"))
    assert edited["errors"] == []
    assert {c["cell"] for c in edited["changed"]} == {"E3", "F4", "F5", "E7"}
    xlsx.Book.open(str(sales)).csv("Sales", None, str(tmp_path / "s.csv"))


def test_edit_lists_a_changed_constant_and_the_formulas_that_follow(sales: Path, tmp_path: Path) -> None:
    """Only formula cells (and the --set targets) are compared: a constant edit and its dependents are found."""
    reply = xlsx.Book.open(str(sales)).edit(["Sales!B2=20", "Sales!H10=new"], str(tmp_path / "e.xlsx"))
    changed = {c["cell"]: c for c in reply["changed"]}
    assert set(changed) == {"B2", "D2", "D7", "H10"}  # E2 = D2/B2 = C2: unchanged, so not listed
    assert changed["B2"]["before"]["value"] == 10 and changed["B2"]["after"]["value"] == 20
    assert changed["H10"]["before"] == {"value": None, "formula": None}


def test_csv_streams_rows_with_blank_rows_kept(tmp_path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"], sheet["B1"] = "a", "b"
    sheet["A4"], sheet["C4"] = 1, "=A4*2"
    path = tmp_path / "gaps.xlsx"
    workbook.save(path)
    reply = xlsx.Book.open(str(path)).csv(None, None, str(tmp_path / "gaps.csv"))
    lines = (tmp_path / "gaps.csv").read_text(encoding="utf-8-sig").splitlines()
    assert lines == ["a,b,", ",,", ",,", "1,,2"] and reply["rows"] == 4


def test_csv_of_a_range_streams_only_that_range(sales: Path, tmp_path: Path) -> None:
    reply = xlsx.Book.open(str(sales)).csv("Sales", "B2:D3", str(tmp_path / "r.csv"))
    assert (tmp_path / "r.csv").read_text(encoding="utf-8-sig").splitlines() == ["10,2.5,25", "0,3,0"]
    assert reply["rows"] == 2


# --- S5 and S6: the Office lock folder and honest messages ---------------------------------------------------------


def test_agents_get_the_hubs_resolved_lock_folder(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "hub-office"
    monkeypatch.setenv(office_registry.REGISTRY_ENV, str(folder))
    env = DocumentAccess(outputs=tmp_path).env()
    assert env[office_registry.REGISTRY_ENV] == os.path.realpath(folder)
    monkeypatch.delenv(office_registry.REGISTRY_ENV)
    assert DocumentAccess(outputs=tmp_path).env()[office_registry.REGISTRY_ENV] == \
        os.path.realpath(office_registry.folder())  # the default %TEMP% folder, resolved by the hub


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


# --- A missing read limit under the hub --------------------------------------------------------------------------


def test_a_missing_read_limit_under_the_hub_reads_only_the_session(hub_doc_exe: str, tmp_path: Path) -> None:
    """An agent that unsets HUB_DOC_READ_ROOTS can't widen reads: with an outputs folder set, only the session's own
    folder (the outputs folder's parent) can be read. Run by hand with neither variable, any file."""
    session = make_session(tmp_path / "attachments")
    inside = session.original("sales.xlsx", samples.sales_xlsx())
    elsewhere = tmp_path / "private" / "secret.xlsx"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(samples.sales_xlsx())
    tool = HubDoc(hub_doc_exe, session.outputs)  # sets HUB_DOC_OUTPUTS and clears every other HUB_DOC_ variable
    assert tool.xlsx_check(inside).code == 0
    refused = tool.xlsx_check(elsewhere)
    assert refused.code == 1 and "HUB_DOC_READ_ROOTS isn't set" in refused.data["error"]
    assert tool.run("docx", "diff", inside, elsewhere).code in (1, 2)  # a second input outside is refused too
    assert HubDoc(hub_doc_exe, None).xlsx_check(elsewhere).code == 0
