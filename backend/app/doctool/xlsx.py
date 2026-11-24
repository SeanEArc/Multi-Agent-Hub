"""Excel through BetterOffice (`betteroffice-xlsx`): every read recalculates first, so the answers are right even for
a workbook another tool saved without results (probe 2026-11-21: openpyxl-made files store none).

- check: formula errors (walked cell by cell after `recalculate()`; BetterOffice has no "list errors" call), charts,
  and notes such as pivot tables, which are kept but not refreshed.
- read: the computed values as a Markdown grid (`export_markdown`).
- edit: `--set` cells as a user would type them, recalculate, save a copy that stores the new results (so it opens
  correct anywhere); the reply lists the changed cells (values and formulas, the edited ones and those that changed
  because of them) and the errors left.
- render: a PNG of a sheet or range. csv: computed values, for the agent's own analysis (no pandas, by decision).
- `.xls`: BetterOffice refuses it, so xlrd reads its values into a workbook built with openpyxl, which BetterOffice
  then opens; formulas in an `.xls` aren't kept (xlrd sees results only) and edits are saved as `.xlsx`.
"""

import csv
import io
import re
from typing import Any

from app.doctool.contract import Failed

# Filled cells one walk reads: a time guard (about 18 s per million cells), reported as `truncated`. Memory is kept
# lean instead of bounded by it: check keeps only errors, csv streams rows, edit keeps only formula cells.
FILLED_LIMIT = 1_000_000
EXPORT_CELLS = 20_000  # cells per structured export (BetterOffice allows 100,000; smaller bands hold ~450 MB less at 1.5M cells)
EXPORT_BYTES = 16 * 1024 * 1024
CSV_PAD_LIMIT = 5_000_000  # fields in a rectangular CSV; past that, rows end at their last filled cell
CHANGED_LIMIT = 500  # changed cells listed in a reply; the count is always exact
ERROR_LIMIT = 500
MARKDOWN_LIMIT = 200_000
XLS_WARNING = ("This is an old .xls file, read through a fallback (xlrd) that sees values but not formulas: "
               "formulas aren't kept, and edits are saved as .xlsx.")
PIVOT_WARNING = "pivot tables are kept, but their values aren't refreshed until Excel refreshes them"
_CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d{1,7})$")
_RANGE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d{1,7})(?::\$?([A-Za-z]{1,3})\$?(\d{1,7}))?$")


def run(action: str, command: Any, output: str | None) -> dict[str, Any]:
    book = Book.open(command.files[0])
    match action:
        case "check":
            return book.check()
        case "read":
            return book.read(command.sheet, command.range)
        case "edit":
            return book.edit(command.sets, output)
        case "render":
            return book.render(command.sheet, command.range, command.scale, output)
        case "csv":
            return book.csv(command.sheet, command.range, output)
    raise Failed(f"unknown command xlsx {action}")


# --- Cell addresses ---------------------------------------------------------------------------------------------


def column_number(letters: str) -> int:
    number = 0
    for char in letters.upper():
        number = number * 26 + ord(char) - 64
    return number


def column_letters(number: int) -> str:
    letters = ""
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def parse_range(text: str) -> tuple[int, int, int, int]:
    """(first row, first column, last row, last column) of `A1` or `A1:F20`. Raises Failed."""
    match = _RANGE.match(text.strip())
    if not match:
        raise Failed(f"{text!r} isn't a cell or range like A1:F20.")
    c1, r1 = column_number(match[1]), int(match[2])
    c2, r2 = (column_number(match[3]), int(match[4])) if match[3] else (c1, r1)
    return min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2)


class ErrorCode(str):
    """A formula error's code (`#DIV/0!`), kept apart from text that happens to look like one."""


def plain(value: Any) -> Any:
    """A cell value as JSON: numbers, text, booleans, an error's code (`#DIV/0!`) or None."""
    code = getattr(value, "code", None)
    if code is not None:
        return ErrorCode(code)
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return int(value)
    return value


def text(value: Any) -> str:
    value = plain(value)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


# --- The workbook -----------------------------------------------------------------------------------------------


class Book:
    def __init__(self, workbook: Any, path: str, warnings: list[str]) -> None:
        self.wb = workbook
        self.path = path
        self.warnings = warnings
        self.is_xls = path.lower().endswith(".xls")
        self.filled = 0  # filled cells read so far in this command
        self.truncated = False  # FILLED_LIMIT was reached: lists may be incomplete

    @classmethod
    def open(cls, path: str) -> "Book":
        import betteroffice_xlsx as bx

        warnings: list[str] = []
        if path.lower().endswith(".xls"):
            workbook = bx.Workbook.open(xls_as_xlsx(path))
            warnings.append(XLS_WARNING)
        else:
            workbook = bx.Workbook.open_path(path)
        workbook.recalculate()
        book = cls(workbook, path, warnings)
        book.warnings += book._notes()
        return book

    # Sheets

    def sheet_index(self, name: str | None) -> int:
        names = list(self.wb.sheet_names)
        if not names:
            raise Failed("the workbook has no sheets.")
        if name is None:
            return 0
        for index, candidate in enumerate(names):
            if candidate == name:
                return index
        for index, candidate in enumerate(names):
            if candidate.lower() == name.lower():
                return index
        raise Failed(f"there's no sheet named {name!r}. Sheets: {', '.join(names)}.")

    def used_range(self, index: int) -> tuple[int, int, int, int] | None:
        exported = self.wb.export_structured(scope=[{"sheet": index}], max_cells=1)
        sheets = (exported.get("content") or {}).get("sheets") or []
        used = sheets[0].get("usedRange") if sheets else None
        return parse_range(used) if used else None

    def cells(self, index: int, bounds: tuple[int, int, int, int] | None = None):
        """(row, column, address, value, formula) for each **filled** cell in `bounds` (default: the used range), row
        by row. BetterOffice's structured export is sparse, so a sheet with one stray cell at XFD1048576 costs no more
        than its filled cells (a walk over every address took 10 s for 14 million empty ones, QA 2026-11-23). One
        export answers at most 16 MB, so the rows go in bands, and a band the export cut short is split in two.
        Stops after FILLED_LIMIT cells for the whole command, setting `self.truncated`."""
        bounds = bounds or self.used_range(index)
        if bounds is None:
            return
        r1, c1, r2, c2 = bounds
        yield from self._band(index, r1, r2, c1, c2)

    def _band(self, index: int, r1: int, r2: int, c1: int, c2: int):
        if self.filled >= FILLED_LIMIT:
            # Only cut short if something is really left: a workbook of exactly FILLED_LIMIT cells is complete.
            self.truncated = self.truncated or self._any_filled(index, r1, r2, c1, c2)
            return
        exported = self._export(index, r1, r2, c1, c2, EXPORT_CELLS)
        sheets = exported["content"].get("sheets") or []
        sheet = sheets[0] if sheets else {}
        if sheet.get("truncated") or exported["content"].get("truncated"):
            # Split by rows, and a single row by columns, until each part fits in one export.
            if r2 > r1:
                middle = (r1 + r2) // 2
                yield from self._band(index, r1, middle, c1, c2)
                yield from self._band(index, middle + 1, r2, c1, c2)
                return
            if c2 > c1:
                middle = (c1 + c2) // 2
                yield from self._band(index, r1, r2, c1, middle)
                yield from self._band(index, r1, r2, middle + 1, c2)
                return
        if sheet.get("truncated"):
            self.truncated = True  # one row over 16 MB: what came back is all there is
        found = []
        for cell in sheet.get("cells") or []:
            address = (cell.get("anchor") or {}).get("a1") or ""
            value, formula = _exported_value(cell.get("value") or {}), cell.get("formula")
            if value is None and not formula:
                continue
            row, col = _position(address)
            found.append((row, col, address, value, formula))
        for item in sorted(found):
            if self.filled >= FILLED_LIMIT:
                self.truncated = True
                return
            self.filled += 1
            yield item

    def _export(self, index: int, r1: int, r2: int, c1: int, c2: int, max_cells: int) -> dict[str, Any]:
        scope = [{"sheet": index, "range": _range_text((r1, c1, r2, c2))}]
        exported = self.wb.export_structured(scope=scope, max_cells=max_cells, max_bytes=EXPORT_BYTES)
        if not exported.get("ok", True) or "content" not in exported:
            failure = exported.get("failure") or {}
            raise Failed(str(failure.get("message") or "the sheet couldn't be read"))
        return exported

    def _any_filled(self, index: int, r1: int, r2: int, c1: int, c2: int) -> bool:
        exported = self._export(index, r1, r2, c1, c2, 1)
        sheets = exported["content"].get("sheets") or []
        sheet = sheets[0] if sheets else {}
        if sheet.get("truncated") or exported["content"].get("truncated"):
            return True
        return any(_exported_value(c.get("value") or {}) is not None or c.get("formula")
                   for c in sheet.get("cells") or [])

    def snapshot(self) -> dict[tuple[str, str], tuple[Any, str | None]]:
        """Every filled cell of every sheet. Each snapshot gets the whole FILLED_LIMIT: `edit` takes one before and
        one after, and a shared budget left the second one short, listing every cell it missed as changed."""
        self.filled = 0
        state = {}
        for index, name in enumerate(self.wb.sheet_names):
            for _row, _col, address, value, formula in self.cells(index):
                state[(name, address)] = (value, f"={formula}" if formula else None)
        return state

    def _notes(self) -> list[str]:
        notes = []
        for index, name in enumerate(self.wb.sheet_names):
            exported = self.wb.export_structured(scope=[{"sheet": index}], max_cells=1)
            for diagnostic in (exported.get("content") or {}).get("diagnostics") or []:
                if diagnostic.get("code") == "unsupported-content" and "pivot" in str(diagnostic.get("message")):
                    notes.append(f"{name}: {PIVOT_WARNING}")
        return notes

    def charts(self) -> int:
        count = 0
        for index in range(len(self.wb.sheet_names)):
            exported = self.wb.export_structured(scope=[{"sheet": index}], max_cells=1)
            for sheet in (exported.get("content") or {}).get("sheets") or []:
                count += sum(1 for item in sheet.get("objects") or [] if item.get("kind") == "chart")
        return count

    # Actions

    def check(self) -> dict[str, Any]:
        # Streamed: only the error cells are kept (a full snapshot cost about 760 MB per million cells, on top of
        # BetterOffice's own ~850 MB, and aborted under the 2 GB cap at about 1.2M cells).
        self.filled = 0
        found: dict[tuple[str, str], tuple[Any, str | None]] = {}
        for index, name in enumerate(self.wb.sheet_names):
            for _row, _col, address, value, formula in self.cells(index):
                if isinstance(value, ErrorCode):
                    found[(name, address)] = (value, f"={formula}" if formula else None)
        errors = errors_in(found, list(self.wb.sheet_names))
        sheets = []
        for index, name in enumerate(self.wb.sheet_names):
            used = self.used_range(index)
            sheets.append({"name": name, "used_range": _range_text(used) if used else None})
        return self._mark({"errors": errors[:ERROR_LIMIT], "error_count": len(errors), "charts": self.charts(),
                           "sheets": sheets, "warnings": self.warnings})

    def read(self, sheet: str | None, cells: str | None) -> dict[str, Any]:
        indexes = [self.sheet_index(sheet)] if sheet or cells else range(len(self.wb.sheet_names))
        scope = []
        for index in indexes:
            entry: dict[str, Any] = {"sheet": index}
            if cells:
                entry["range"] = _range_text(parse_range(cells))
            scope.append(entry)
        exported = self.wb.export_markdown(scope=scope)
        if not exported.get("ok", True) or "content" not in exported:
            failure = exported.get("failure") or {}
            raise Failed(str(failure.get("message") or "the sheet couldn't be read"))
        markdown = re.sub(r"<!-- xlsx-export:\d+ -->\n?", "", exported["content"].get("markdown") or "").strip()
        truncated = bool(exported["content"].get("truncated"))
        if len(markdown) > MARKDOWN_LIMIT:
            markdown, truncated = markdown[:MARKDOWN_LIMIT], True
        reply = {"markdown": markdown, "sheets": list(self.wb.sheet_names), "warnings": self.warnings}
        if truncated:
            reply["truncated"] = True
            reply["warnings"] = [*self.warnings, "The view was cut short: read a smaller --range."]
        return reply

    def edit(self, sets: list[str], output: str | None) -> dict[str, Any]:
        if output is None:
            raise Failed("xlsx edit needs an output file.")
        # Only cells an edit can change are kept: formula cells (recalculation changes nothing else), cells holding an
        # error constant (so the error list stays complete), and the --set targets. They are read again afterwards
        # one by one, so there is a single walk and no full snapshot.
        targets = [self._parse_set(item) for item in sets]
        names = list(self.wb.sheet_names)
        self.filled = 0
        before: dict[tuple[str, str], tuple[Any, str | None]] = {}
        for index, name in enumerate(names):
            for _row, _col, address, value, formula in self.cells(index):
                if formula or isinstance(value, ErrorCode):
                    before[(name, address)] = (value, f"={formula}" if formula else None)
        for index, address, _value in targets:
            before.setdefault((names[index], address), self._cell(index, address))
        for item, (index, address, value) in zip(sets, targets):
            try:
                self.wb.set(index, address, value)
            except Exception as exc:
                raise Failed(f"--set {item!r} couldn't be applied: {exc}") from None
        self.wb.recalculate()
        after = {key: self._cell(names.index(key[0]), key[1]) for key in before}
        changed = changed_cells(before, after, list(self.wb.sheet_names))
        errors = errors_in(after, list(self.wb.sheet_names))
        self.wb.save_path(output)
        return self._mark({"output": output, "errors": errors[:ERROR_LIMIT], "error_count": len(errors),
                           "changed": changed[:CHANGED_LIMIT], "changed_count": len(changed), "charts": self.charts(),
                           "warnings": self.warnings})

    def render(self, sheet: str | None, cells: str | None, scale: float, output: str | None) -> dict[str, Any]:
        if output is None:
            raise Failed("xlsx render needs an output file.")
        index = self.sheet_index(sheet)
        try:
            png = self.wb.render_png(index, scale=scale, range=cells.strip() if cells else None)
        except Exception as exc:  # RangeError (bad range), RenderError (too large)
            raise Failed(f"couldn't draw the sheet: {exc}") from None
        png.write(output)
        return {"output": output, "width": png.width, "height": png.height,
                "sheet": self.wb.sheet_names[index], "warnings": [*self.warnings, "Charts aren't drawn."]}

    def csv(self, sheet: str | None, cells: str | None, output: str | None) -> dict[str, Any]:
        if output is None:
            raise Failed("xlsx csv needs an output file.")
        index = self.sheet_index(sheet)
        bounds = parse_range(cells) if cells else self.used_range(index)
        self.filled = 0
        rows = 0
        with open(output, "w", encoding="utf-8-sig", newline="") as out:
            writer = csv.writer(out)
            if bounds is not None:
                first_row, first_col, last_row, last_col = bounds
                width = last_col - first_col + 1
                # Rectangular when that stays small; a stray far cell would otherwise mean millions of empty fields.
                pad = width * (last_row - first_row + 1) <= CSV_PAD_LIMIT
                # Streamed: cells come row by row, so each row is written as soon as the next one starts. Blank
                # rows in between are written; blank rows after the last filled one are not.
                current, cols = None, {}

                def flush(row: int, cols: dict[int, str]) -> None:
                    end = width if pad else (max(cols) - first_col + 1 if cols else 0)
                    writer.writerow([cols.get(first_col + i, "") for i in range(end)])

                next_row = first_row
                for row, col, _address, value, _formula in self.cells(index, bounds):
                    if row != current:
                        if current is not None:
                            flush(current, cols)
                            next_row = current + 1
                        for blank in range(next_row, row):
                            flush(blank, {})
                        rows += row - next_row
                        current, cols = row, {}
                        rows += 1
                    cols[col] = text(value)
                if current is not None:
                    flush(current, cols)
        return self._mark({"output": output, "sheet": self.wb.sheet_names[index], "rows": rows,
                           "range": _range_text(bounds) if bounds else None, "warnings": self.warnings})

    def _cell(self, index: int, address: str) -> tuple[Any, str | None]:
        """One cell now, as a snapshot stores it."""
        formula = self.wb.formula(index, address)
        return plain(self.wb.value(index, address)), f"={formula}" if formula else None

    def _mark(self, reply: dict[str, Any]) -> dict[str, Any]:
        """Says so when the cell limit cut the work short."""
        if self.truncated:
            reply["truncated"] = True
            reply["warnings"] = [*reply.get("warnings", []),
                                 f"Only the first {FILLED_LIMIT:,} filled cells were read; lists may be incomplete."]
        return reply

    def _parse_set(self, item: str) -> tuple[int, str, str]:
        """`Sheet!A1=value` (a formula keeps its own `=`: `Sheet!A1==SUM(B2:B5)`); the sheet is optional."""
        target, sep, value = item.partition("=")
        if not sep:
            raise Failed(f"--set {item!r}: write it as Sheet!A1=value (a formula: Sheet!A1==SUM(B2:B5)).")
        sheet_name, bang, address = target.strip().rpartition("!")
        sheet_name = sheet_name.strip().strip("'") if bang else None
        if not _CELL.match(address.strip()):
            raise Failed(f"--set {item!r}: {address.strip()!r} isn't a cell address like E3.")
        return self.sheet_index(sheet_name), address.strip().replace("$", "").upper(), value


def errors_in(state: dict[tuple[str, str], tuple[Any, str | None]], order: list[str]) -> list[dict[str, Any]]:
    found = []
    for (sheet, address), (value, formula) in state.items():
        if isinstance(value, ErrorCode):
            found.append({"sheet": sheet, "cell": address, "error": value, "formula": formula})
    return sorted(found, key=lambda e: (order.index(e["sheet"]), *_position(e["cell"])))


def _exported_value(value: dict[str, Any]) -> Any:
    """A structured export's `{kind, value}` as `plain()` gives a cell's value."""
    kind, raw = value.get("kind"), value.get("value")
    if kind == "error":
        return ErrorCode(str(raw))
    if kind in (None, "empty"):
        return None
    return plain(raw)


def changed_cells(before: dict, after: dict, order: list[str]) -> list[dict[str, Any]]:
    changed = []
    for key in before.keys() | after.keys():
        old, new = before.get(key, (None, None)), after.get(key, (None, None))
        if old != new:
            changed.append({"sheet": key[0], "cell": key[1],
                            "before": {"value": old[0], "formula": old[1]},
                            "after": {"value": new[0], "formula": new[1]}})
    return sorted(changed, key=lambda c: (order.index(c["sheet"]) if c["sheet"] in order else 0,
                                          *_position(c["cell"])))


def _position(address: str) -> tuple[int, int]:
    match = _CELL.match(address)
    return (int(match[2]), column_number(match[1])) if match else (0, 0)


def _range_text(bounds: tuple[int, int, int, int]) -> str:
    r1, c1, r2, c2 = bounds
    first, last = f"{column_letters(c1)}{r1}", f"{column_letters(c2)}{r2}"
    return first if first == last else f"{first}:{last}"


# --- .xls ---------------------------------------------------------------------------------------------------------


def xls_as_xlsx(path: str) -> bytes:
    """An `.xls` workbook's values (xlrd) as `.xlsx` bytes (openpyxl), for BetterOffice to open."""
    import xlrd
    from openpyxl import Workbook

    book = xlrd.open_workbook(path, on_demand=True)
    workbook = Workbook()
    workbook.remove(workbook.active)
    try:
        for index in range(book.nsheets):
            source = book.sheet_by_index(index)
            sheet = workbook.create_sheet(source.name[:31] or f"Sheet{index + 1}")
            for row in range(source.nrows):
                for col in range(source.ncols):
                    cell = source.cell(row, col)
                    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                        continue
                    if cell.ctype == xlrd.XL_CELL_ERROR:
                        value: Any = xlrd.error_text_from_code.get(cell.value, "#N/A")
                    elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                        value = bool(cell.value)
                    else:
                        value = cell.value
                    sheet.cell(row=row + 1, column=col + 1, value=value)
            book.unload_sheet(index)
    finally:
        book.release_resources()
    if not workbook.worksheets:
        workbook.create_sheet("Sheet1")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()

