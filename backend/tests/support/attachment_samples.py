"""Small real files for the attachment tests: a PNG, a one-page PDF with known text, a Word document, a workbook."""

import io
import struct
import zlib

PDF_WORD = "PELICAN"


def png(rgb: tuple[int, int, int] = (255, 0, 0), width: int = 8, height: int = 8) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def pdf(text: str = f"The secret word is {PDF_WORD}") -> bytes:
    stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def docx(heading: str = "Quarterly report", body: str = "Revenue grew by 12 percent.") -> bytes:
    from docx import Document

    document = Document()
    document.add_heading(heading, 1)
    document.add_paragraph(body)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "region"
    table.cell(0, 1).text = "sales"
    table.cell(1, 0).text = "north"
    table.cell(1, 1).text = "42"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Fruit"
    sheet.append(["name", "qty"])
    sheet.append(["apple", 3])
    sheet.append(["pear", 7])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
