"""Writes the text copy of one document. Run by `app.extract` in its own capped process; imports nothing from the hub.

Reads one JSON request from stdin, `{kind, src, dst, max_chars, memory_limit}`, writes the text to `dst` and prints one JSON
line: `{"ok": true, "pages": n|null, "chars": n, "truncated": bool}` or `{"ok": false, "error": "..."}`.
After reading the request and before parsing anything, the worker caps its own memory (`cap_memory`, its own
job object on Windows) and refuses to parse if it can't. The request carries the limit as `memory_limit`.
"""

import csv
import io
import json
import os
import sys
from collections.abc import Iterator

CELL_CHARS = 2_000
CSV_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


class _Writer:
    """Collects text up to `max_chars`, then stops the reader by raising _Full."""

    def __init__(self, out: io.TextIOBase, max_chars: int) -> None:
        self.out, self.max_chars, self.chars, self.truncated = out, max_chars, 0, False

    def write(self, text: str) -> None:
        room = self.max_chars - self.chars
        if len(text) > room:
            self.out.write(text[:room])
            self.chars = self.max_chars
            self.truncated = True
            raise _Full()
        self.out.write(text)
        self.chars += len(text)


class _Full(Exception):
    pass


def _row(values: Iterator[object]) -> str:
    cells = []
    for value in values:
        text = "" if value is None else str(value)
        text = text.replace("\r\n", " ").replace("\n", " ").replace("\t", " ")
        cells.append(text[:CELL_CHARS])
    while cells and not cells[-1]:
        cells.pop()
    return "\t".join(cells)


def pdf(src: str, w: _Writer) -> int:
    from pypdf import PdfReader

    reader = PdfReader(src)
    if reader.is_encrypted:
        # decrypt() reports a wrong password by returning NOT_DECRYPTED (0), not by raising; it raises for an
        # encryption it can't do (AES needs the `cryptography` package, which isn't installed).
        try:
            decrypted = reader.decrypt("")
        except Exception:
            raise ValueError("the PDF is encrypted in a way the reader can't open") from None
        if not decrypted:
            raise ValueError("the PDF is password-protected")
    pages = len(reader.pages)
    try:
        for number, page in enumerate(reader.pages, 1):
            w.write(f"--- Page {number} of {pages} ---\n")
            w.write((page.extract_text() or "").strip() + "\n\n")
    except _Full:
        pass
    return pages


def word(src: str, w: _Writer) -> None:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(src)
    body = document.element.body
    try:
        # Paragraphs and tables in the order they appear.
        for child in body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                paragraph = Paragraph(child, document)
                style = (paragraph.style.name or "") if paragraph.style is not None else ""
                text = paragraph.text
                if style.startswith("Heading") and text.strip():
                    level = style.removeprefix("Heading").strip()
                    w.write("#" * (int(level) if level.isdigit() else 1) + " " + text + "\n\n")
                elif text.strip():
                    w.write(text + "\n\n")
            elif tag == "tbl":
                for row in Table(child, document).rows:
                    w.write(_row(cell.text for cell in row.cells) + "\n")
                w.write("\n")
    except _Full:
        pass


def excel(src: str, w: _Writer) -> None:
    if src.lower().endswith(".xls"):
        return _xls(src, w)
    from itertools import zip_longest

    from openpyxl import load_workbook

    # read_only streams rows. data_only gives the values Excel last calculated; a workbook saved by another tool
    # may have none stored, so a second, formula view of the same sheet is read in step and a cell with no value
    # shows its formula (=SUM(B2:B9)) instead of nothing.
    values = load_workbook(src, read_only=True, data_only=True)
    formulas = load_workbook(src, read_only=True, data_only=False)
    try:
        for sheet in values.worksheets:
            w.write(f"--- Sheet: {sheet.title} ---\n")
            formula_rows = formulas[sheet.title].iter_rows(values_only=True)
            for row, formula_row in zip_longest(sheet.iter_rows(values_only=True), formula_rows, fillvalue=()):
                cells = [value if value is not None else _formula(formula)
                         for value, formula in zip_longest(row or (), formula_row or ())]
                line = _row(iter(cells))
                if line:
                    w.write(line + "\n")
            w.write("\n")
    except _Full:
        pass
    finally:
        values.close()
        formulas.close()


def _formula(cell: object) -> object:
    """A formula's text when the cell holds one (a plain string starting with =, or an array formula), else None."""
    if isinstance(cell, str) and cell.startswith("="):
        return cell
    text = getattr(cell, "text", None)  # openpyxl's ArrayFormula
    return text if isinstance(text, str) and text.startswith("=") else None


def _xls(src: str, w: _Writer) -> None:
    import xlrd

    book = xlrd.open_workbook(src, on_demand=True)
    try:
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            w.write(f"--- Sheet: {sheet.name} ---\n")
            for r in range(sheet.nrows):
                line = _row(iter(sheet.row_values(r)))
                if line:
                    w.write(line + "\n")
            w.write("\n")
            book.unload_sheet(index)
    except _Full:
        pass
    finally:
        book.release_resources()


def csv_file(src: str, w: _Writer) -> None:
    raw = open(src, "rb").read()
    for encoding in CSV_ENCODINGS:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        for row in csv.reader(io.StringIO(text, newline=""), dialect):
            line = _row(iter(row))
            if line:
                w.write(line + "\n")
    except _Full:
        pass


READERS = {"pdf": pdf, "word": word, "excel": excel, "csv": csv_file}
# This process's own job; kept referenced for the process's lifetime.
_SELF_JOB = None


def cap_memory(limit: int) -> None:
    """Cap this process's committed memory at `limit` before any file is parsed. Raises OSError if it can't.

    The hub also puts the worker in a job, but in a virtual environment `python.exe` is a launcher whose child (the
    real interpreter, this process) can start before that job is assigned and so end up outside it. Capping from
    inside, after the request is read and before parsing, holds however the process was started.
    """
    global _SELF_JOB
    if os.name == "nt":
        # One process (no children); not killed on close: the handle lives as long as the process.
        _SELF_JOB = WindowsJob(limit, active_processes=1, kill_on_close=False)
        _SELF_JOB.assign_current()
    else:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def main() -> None:
    try:
        request = json.loads(sys.stdin.buffer.read())
        try:
            cap_memory(int(request["memory_limit"]))
        except (OSError, KeyError, ValueError) as exc:  # fail closed: no cap, no parsing
            raise RuntimeError(f"the reader couldn't be given a memory limit ({exc})") from None
        reader = READERS[request["kind"]]
        with open(request["dst"], "w", encoding="utf-8", newline="\n") as out:
            writer = _Writer(out, int(request["max_chars"]))
            pages = reader(request["src"], writer)
            if writer.truncated:
                out.write(f"\n\n[Text copy cut at {writer.max_chars:,} characters; open the original for the rest.]\n")
        result = {"ok": True, "pages": pages, "chars": writer.chars, "truncated": writer.truncated}
    except MemoryError:
        result = {"ok": False, "error": "the file needed more memory than allowed"}
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        result = {"ok": False, "error": message[:300]}
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()




class WindowsJob:
    """A job object capping each process's committed memory and how many processes may run in it, optionally killing
    them when the handle closes. Raises OSError when it can't be made or assigned."""

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x0008
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
    JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0400
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    def __init__(self, memory_limit: int, active_processes: int = 1, kill_on_close: bool = True) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._kernel32 = kernel32

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = (
            self.JOB_OBJECT_LIMIT_PROCESS_MEMORY | self.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | self.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            | (self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE if kill_on_close else 0)
        )
        limits.BasicLimitInformation.ActiveProcessLimit = active_processes
        limits.ProcessMemoryLimit = memory_limit
        if not kernel32.SetInformationJobObject(self.handle, self.JobObjectExtendedLimitInformation,
                                                ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, pid: int) -> None:
        ctypes, kernel32 = self._ctypes, self._kernel32
        process = kernel32.OpenProcess(self.PROCESS_SET_QUOTA | self.PROCESS_TERMINATE, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process)

    def assign_current(self) -> None:
        """Put this process in the job (nested jobs work since Windows 8, so it may already be in another)."""
        ctypes, kernel32 = self._ctypes, self._kernel32
        if not kernel32.AssignProcessToJobObject(self.handle, kernel32.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


if __name__ == "__main__":
    main()
