"""PNG page images of a PDF with pypdfium2 (decided 2026-11-23), for agents that can't read a PDF themselves (Codex,
the local model). PNGs are written with zlib directly, so no imaging library (Pillow) is needed."""

import struct
import zlib
from pathlib import Path
from typing import Any

from app.doctool.contract import Failed

# 1.5 x 72 dpi: a Letter page is 918 x 1188 pixels, readable without being huge.
BASE_SCALE = 1.5


def write_png(path: Path, width: int, height: int, stride: int, rgb: bytes) -> None:
    rows = b"".join(b"\x00" + rgb[y * stride: y * stride + width * 3] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def render(pdf_path: str, staging: str, max_pages: int, scale: float = 1.0) -> dict[str, Any]:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(pdf_path)
    except pdfium.PdfiumError as exc:
        raise Failed(f"the PDF couldn't be opened: {exc}") from None
    try:
        total = len(pdf)
        staged = []
        for number in range(min(total, max_pages)):
            page = pdf[number]
            bitmap = page.render(scale=BASE_SCALE * scale, rev_byteorder=True)  # RGB
            if bitmap.n_channels != 3:
                raise Failed(f"page {number + 1} rendered in an unexpected format ({bitmap.format}).")
            target = Path(staging) / f"page-{number + 1}.png"
            write_png(target, bitmap.width, bitmap.height, bitmap.stride, bytes(bitmap.buffer))
            staged.append(str(target))
            bitmap.close()
            page.close()
    finally:
        pdf.close()
    reply: dict[str, Any] = {"staged": staged, "page_count": total}
    if total > max_pages:
        reply["warnings"] = [f"Only the first {max_pages} of {total} pages were drawn (use --max-pages)."]
    return reply
