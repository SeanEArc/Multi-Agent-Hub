"""Real sample documents for the PR 2 ("working on documents") acceptance tests, built in code.

- `sales_xlsx()`: a workbook made with openpyxl (so it has no cached values, like many generated files) holding
  formula errors of three kinds, an error that propagates into a total, an XLOOKUP and a bar chart. The same
  shape the 2026-11-21 probe used (`probe.md` section 1), so BetterOffice's behavior on it is known.
- `big_xlsx()`: 20,000 rows of formulas (the probe's "medium" workbook peaked at 248 MB committed in BetterOffice).
- `old_xls()`: a genuine BIFF8 `.xls` in an OLE2 compound file, written byte by byte (no xlwt), which BetterOffice
  refuses (`ParseError: bad zip`) and xlrd reads.
- `formatted_docx()`: a Word file with a real heading style, a direct-formatted "fake heading" with its own
  spacing, a bold Arial 16 pt run, bullet and numbered lists, a hyperlink, a PAGE field and a tracked insertion by
  "Alice", each in its own paragraph with a marker word, so tests can aim at one paragraph.

Only openpyxl and python-docx (PR 1 dependencies) are used here, so importing this module never fails.
"""

import io
import struct

from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

# Sheet "Sales": what the errors are before any fix, as (cell, error).
SALES_ERRORS = {("Sales", "E3", "#DIV/0!"), ("Sales", "F4", "#REF!"), ("Sales", "F5", "#N/A"), ("Sales", "E7", "#DIV/0!")}
# Fixes that clear every error (E7 is a SUM over E2:E5, so it clears with E3).
SALES_FIXES = {
    "Sales!E3": "=IF(B3=0,0,D3/B3)",
    "Sales!F4": "=1+1",
    "Sales!F5": '=IFERROR(VLOOKUP("Nowhere",A2:D5,4,FALSE),0)',
}
# Revenue D2..D5 = units * price; D7 is their sum.
SALES_ROWS = [("North", 10, 2.5), ("South", 0, 3.0), ("East", 7, 4.0), ("West", 12, 1.5)]
SALES_TOTAL = sum(units * price for _, units, price in SALES_ROWS)  # 71.0


def sales_xlsx() -> bytes:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.append(["Region", "Units", "Price", "Revenue", "Share"])
    for row, (region, units, price) in enumerate(SALES_ROWS, start=2):
        sheet.append([region, units, price, f"=B{row}*C{row}", f"=D{row}/B{row}"])  # South: #DIV/0!
    sheet["A7"] = "Total"
    sheet["D7"] = "=SUM(D2:D5)"
    sheet["E7"] = "=SUM(E2:E5)"  # #DIV/0! through E3
    sheet["F2"] = '=_xlfn.XLOOKUP("East",A2:A5,D2:D5)'  # 28
    sheet["F4"] = "=#REF!+1"
    sheet["F5"] = '=VLOOKUP("Nowhere",A2:D5,4,FALSE)'  # #N/A
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=4, min_row=1, max_row=5), titles_from_data=True)
    chart.set_categories(Reference(sheet, min_col=1, min_row=2, max_row=5))
    sheet.add_chart(chart, "H2")
    other = workbook.create_sheet("Notes")
    other["A1"] = "rate"
    other["B1"] = 0.2
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def big_xlsx(rows: int = 20_000) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet.append(["id", "a", "b", "sum", "ratio", "flag"])
    for row in range(2, rows + 2):
        a, b = (row * 7919) % 101, (row * 104729) % 97 + 1
        sheet.append([row - 1, a, b, f"=B{row}+C{row}", f"=B{row}/C{row}", f'=IF(D{row}>100,"high","low")'])
    sheet["H1"] = f"=SUM(D2:D{rows + 1})"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --- .xls (BIFF8 in an OLE2 compound file) -----------------------------------------------------------------

OLD_XLS_SHEET = "Old"
OLD_XLS_ROWS = [("name", "qty"), ("plum", 9.0), ("fig", 4.0)]


def _record(kind: int, data: bytes) -> bytes:
    return struct.pack("<HH", kind, len(data)) + data


def _biff8_workbook() -> bytes:
    def bof(substream: int) -> bytes:
        return _record(0x0809, struct.pack("<HHHHII", 0x0600, substream, 0x0DBB, 0x07CC, 0, 0x06))

    name = OLD_XLS_SHEET.encode("latin-1")
    sheet = [bof(0x0010), _record(0x0200, struct.pack("<IIHHH", 0, len(OLD_XLS_ROWS), 0, 2, 0))]
    for row, values in enumerate(OLD_XLS_ROWS):
        for col, value in enumerate(values):
            if isinstance(value, str):
                text = value.encode("latin-1")
                sheet.append(_record(0x0204, struct.pack("<HHHHB", row, col, 0, len(text), 0) + text))  # LABEL
            else:
                sheet.append(_record(0x0203, struct.pack("<HHHd", row, col, 0, value)))  # NUMBER
    sheet.append(_record(0x000A, b""))

    def globals_(sheet_offset: int) -> bytes:
        boundsheet = _record(0x0085, struct.pack("<IBBBB", sheet_offset, 0, 0, len(name), 0) + name)
        return bof(0x0005) + _record(0x0042, struct.pack("<H", 1252)) + boundsheet + _record(0x000A, b"")

    head = globals_(0)
    stream = globals_(len(head)) + b"".join(sheet)
    return stream + b"\x00" * max(0, 4096 - len(stream))  # at least the mini-stream cutoff: a normal stream


def _ole2(stream_name: str, stream: bytes) -> bytes:
    sector, free, end, fat_sector, no_stream = 512, 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF
    size = len(stream)
    stream = stream + b"\x00" * (-len(stream) % sector)
    data_sectors = len(stream) // sector
    assert data_sectors <= 126
    header = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16
              + struct.pack("<HHHHH", 0x003E, 0x0003, 0xFFFE, 9, 6) + b"\x00" * 6
              + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, end, 0, end, 0)
              + struct.pack("<109I", 0, *([free] * 108)))
    fat = [fat_sector, end] + [2 + i + 1 for i in range(data_sectors - 1)] + [end]
    fat += [free] * (128 - len(fat))

    def entry(name: str, kind: int, child: int, start: int, size: int) -> bytes:
        encoded = (name + "\x00").encode("utf-16-le") if name else b""
        return (encoded.ljust(64, b"\x00") + struct.pack("<HBB", len(encoded), kind, 1 if kind else 0)
                + struct.pack("<III", no_stream, no_stream, child) + b"\x00" * 16 + struct.pack("<I", 0)
                + b"\x00" * 16 + struct.pack("<IQ", start, size))

    directory = (entry("Root Entry", 5, 1, end, 0) + entry(stream_name, 2, no_stream, 2, size)
                 + entry("", 0, no_stream, 0, 0) * 2)
    return header + struct.pack("<128I", *fat) + directory + stream


def old_xls() -> bytes:
    return _ole2("Workbook", _biff8_workbook())


# --- Word ---------------------------------------------------------------------------------------------------

# Marker words: each sits in exactly one paragraph.
PLAIN_NUMBER = "120"  # in the bold Arial 16 pt run of an ordinary paragraph
LINK_WORD = "LINKWORD"  # in a paragraph with a hyperlink
FIELD_WORD = "FIELDWORD"  # in a paragraph with a PAGE field
TRACKED_WORD = "TRACKWORD"  # in a paragraph with a tracked insertion by Alice
FAKE_HEADING = "A fake heading made with direct formatting"
TRACKED_AUTHOR = "Alice"


def formatted_docx() -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.shared import Pt

    document = Document()
    document.add_heading("Quarterly report", level=1)

    plain = document.add_paragraph("Revenue was ")
    number = plain.add_run(PLAIN_NUMBER)
    number.bold = True
    number.font.name = "Arial"
    number.font.size = Pt(16)
    plain.add_run(" million this quarter.")

    fake = document.add_paragraph(FAKE_HEADING)
    fake.runs[0].bold = True
    fake.runs[0].font.size = Pt(14)
    fake.paragraph_format.space_after = Pt(18)
    fake.alignment = WD_ALIGN_PARAGRAPH.CENTER

    document.add_paragraph("First bullet", style="List Bullet")
    document.add_paragraph("Second bullet", style="List Bullet")
    document.add_paragraph("Step one", style="List Number")
    document.add_paragraph("Step two", style="List Number")

    linked = document.add_paragraph(f"See {LINK_WORD} and the ")
    rel = document.part.relate_to("https://example.com/", RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    linked._p.append(parse_xml(
        f'<w:hyperlink {nsdecls("w", "r")} r:id="{rel}"><w:r><w:rPr><w:u w:val="single"/></w:rPr>'
        f"<w:t>website</w:t></w:r></w:hyperlink>"))
    linked.add_run(" for details.")

    field = document.add_paragraph(f"{FIELD_WORD} on page ")
    field._p.append(parse_xml(
        f'<w:fldSimple {nsdecls("w")} w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple>'))
    field.add_run(" of the report.")

    tracked = document.add_paragraph("Tracked: ")
    tracked._p.append(parse_xml(
        f'<w:ins {nsdecls("w")} w:id="901" w:author="{TRACKED_AUTHOR}" w:date="2026-11-01T10:00:00Z">'
        f"<w:r><w:t xml:space=\"preserve\">inserted {TRACKED_WORD}</w:t></w:r></w:ins>"))

    closing = document.add_paragraph("Closing paragraph with ")
    italic = closing.add_run("italic Times New Roman")
    italic.italic = True
    italic.font.name = "Times New Roman"
    closing.add_run(" and normal text.")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
