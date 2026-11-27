"""hub-doc's Excel walk: only filled cells, in bands, with a 1,000,000-cell limit (`truncated` past it), and
`check`, `edit` and `csv` streaming instead of taking a full snapshot (`app.doctool.xlsx`).
"""

import time
from pathlib import Path

import openpyxl
import pytest

from app.doctool import xlsx

from support import document_samples as samples
from support.documents import HubDoc, make_session


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
