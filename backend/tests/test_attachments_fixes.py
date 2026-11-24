"""Attachments, round 2: the risks the fixes for QA's first review brought in. Codex's greedy `--image`, the worker
capping its own memory, Claude's per-request block budget, read-only originals, the Mark of the Web, formula
fallbacks in workbooks, the startup sweep, inline fences, the extraction gate and deleting with a file held open."""

import asyncio
import io
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import attachments as attachments_module
from app import extract, extract_worker
from app.adapters.claude import user_line
from app.adapters.codex import CodexAdapter
from app.attachments import (
    IMAGE_SENT_BYTES,
    MB,
    META_FILE,
    PDF_BLOCK_BYTES,
    PDF_BLOCK_PAGES,
    Attachment,
    AttachmentStore,
    claude_blocks,
    with_files,
)
from app.extract import make_text_copy, text_copy_path

import attachment_samples as samples
from conftest import FAKE_CODEX
from test_attachments_delivery import codex_echo, collect, stored, upload
from test_orchestrator import create, wait_until_idle

SESSION = str(uuid.uuid4())
BACKEND = Path(__file__).resolve().parent.parent
windows_only = pytest.mark.skipif(os.name != "nt", reason="job objects and NTFS streams are Windows-only")


def save(store: AttachmentStore, name: str, data: bytes, session: str = SESSION) -> Attachment:
    async def chunks():
        yield data

    return asyncio.run(store.save(session, name, chunks()))


# --- Codex: every image path must be followed by a flag ----------------------------------------------------


def images_end_at_a_flag(args: list[str]) -> list[str]:
    """The image paths in `args`, checking that clap's greedy `--image` list ends right after each path: the next
    argument is a flag (a lone "-" is a value to clap, so it doesn't count)."""
    images = []
    for index, arg in enumerate(args):
        if arg in ("--image", "-i"):
            images.append(args[index + 1])
            following = args[index + 2]
            assert following.startswith("-") and following != "-", (args, index)
    return images


@pytest.mark.parametrize("count", [0, 1, 6])
@pytest.mark.parametrize("options", [{}, {"model": "gpt-5.5", "effort": "high"}])
def test_codex_image_lists_always_end_at_a_flag(tmp_path: Path, count: int, options: dict) -> None:
    files = tmp_path / "files"
    images = [stored(files, f"shot{i}.png", samples.png((i, 0, 0))) for i in range(count)]
    note = stored(files, "notes.txt", b"not an image")
    codex = CodexAdapter(tmp_path, command=[sys.executable, str(FAKE_CODEX), "ok"], codex_home=tmp_path / "home",
                         **options)
    first = codex_echo(collect(codex.start_task("Look", attachments=[note, *images])))
    assert images_end_at_a_flag(first["args"]) == [a.path for a in images]
    assert first["images"] == [a.path for a in images] and first["image_errors"] == []
    assert first["args"][-1] == "-"
    again = codex_echo(collect(codex.send_reply("Again", attachments=[*images, note])))
    assert again["args"][:2] == ["exec", "resume"]
    assert images_end_at_a_flag(again["args"]) == [a.path for a in images]
    assert again["images"] == [a.path for a in images] and again["image_errors"] == []
    assert again["args"][-2:] == [codex.native_session_id, "-"]


def test_codex_images_named_like_flags_are_still_images(tmp_path: Path, monkeypatch) -> None:
    # A relative attachments folder still stores absolute paths, so no path can start with "-".
    monkeypatch.chdir(tmp_path)
    store = AttachmentStore(Path("attachments"))
    weird = [save(store, name, samples.png()) for name in ("-rf.png", "--help.png", "-.png", "- -.png")]
    for attachment in weird:
        assert Path(attachment.path).is_absolute() and not attachment.path.startswith("-")
        assert Path(attachment.path).name == attachment.name
    codex = CodexAdapter(tmp_path, command=[sys.executable, str(FAKE_CODEX), "ok"], codex_home=tmp_path / "home")
    first = codex_echo(collect(codex.start_task("Look", attachments=weird)))
    assert images_end_at_a_flag(first["args"]) == [a.path for a in weird]
    assert first["images"] == [a.path for a in weird] and first["image_errors"] == []
    again = codex_echo(collect(codex.send_reply("Again", attachments=weird[:1])))
    assert again["images"] == [weird[0].path] and again["image_errors"] == []


def test_codex_images_through_the_api_on_new_and_resumed_turns(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok", agent="codex")
    shots = [upload(client, session_id, f"shot{i}.png", samples.png((0, i, 0))) for i in range(3)]
    for text in ("first", "second"):
        client.post(f"/api/sessions/{session_id}/messages",
                    json={"text": text, "attachments": [s["id"] for s in shots]})
        wait_until_idle(client, session_id)
        events = client.get(f"/api/sessions/{session_id}/events").json()
        sent = json.loads([e for e in events if e["type"] == "message"][-1]["payload"]["text"])
        assert sent["images"] == [s["path"] for s in shots] and sent["image_errors"] == [], text
        assert images_end_at_a_flag(sent["args"]) == [s["path"] for s in shots]
        shots = shots[:1]


# --- The worker's own memory cap ---------------------------------------------------------------------------


def run_worker_main(monkeypatch, capsys, request: dict) -> dict:
    """extract_worker.main() in this process, with `request` on stdin; returns the JSON line it prints."""
    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(json.dumps(request).encode())})())
    extract_worker.main()
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.fixture
def parsed(monkeypatch) -> list[str]:
    """Readers replaced by a recorder, so a test sees whether anything was parsed (and nothing real runs)."""
    calls: list[str] = []

    def reader(src: str, writer) -> int:
        calls.append(src)
        writer.write("parsed")
        return 1

    monkeypatch.setattr(extract_worker, "READERS", {kind: reader for kind in extract_worker.READERS})
    return calls


def _request(tmp_path: Path, **extra) -> dict:
    return {"kind": "csv", "src": str(tmp_path / "in.csv"), "dst": str(tmp_path / "out.txt"), "max_chars": 1000,
            "memory_limit": 256 * MB, **extra}


@pytest.mark.parametrize("failure", ["create", "assign"])
def test_the_worker_parses_nothing_when_it_cant_cap_itself(tmp_path: Path, monkeypatch, capsys, parsed,
                                                           failure: str) -> None:
    class FailingJob:
        def __init__(self, memory_limit: int, **options) -> None:
            if failure == "create":
                raise OSError("CreateJobObject failed")

        def assign_current(self) -> None:
            raise OSError("AssignProcessToJobObject failed")

    monkeypatch.setattr(extract_worker, "WindowsJob", FailingJob)
    monkeypatch.setattr(extract_worker.os, "name", "nt")
    result = run_worker_main(monkeypatch, capsys, _request(tmp_path))
    assert result["ok"] is False and "memory limit" in result["error"]
    assert parsed == [] and not (tmp_path / "out.txt").exists()


@pytest.mark.parametrize("limit", ["missing", None, "lots", -1])
def test_the_worker_parses_nothing_without_a_usable_limit(tmp_path: Path, monkeypatch, capsys, parsed,
                                                          limit) -> None:
    capped: list[int] = []

    def cap(value: int) -> None:
        if value <= 0:
            raise OSError("invalid limit")
        capped.append(value)

    monkeypatch.setattr(extract_worker, "cap_memory", cap)
    request = _request(tmp_path)
    if limit == "missing":
        del request["memory_limit"]
    else:
        request["memory_limit"] = limit
    result = run_worker_main(monkeypatch, capsys, request)
    assert result["ok"] is False and parsed == [] and capped == []


def test_the_worker_caps_itself_with_the_requested_limit_before_parsing(tmp_path: Path, monkeypatch, capsys,
                                                                         parsed) -> None:
    order: list[str] = []
    jobs: list[dict] = []

    class RecordingJob:
        def __init__(self, memory_limit: int, **options) -> None:
            jobs.append({"memory_limit": memory_limit, **options})

        def assign_current(self) -> None:
            order.append("capped")

    monkeypatch.setattr(extract_worker, "WindowsJob", RecordingJob)
    monkeypatch.setattr(extract_worker.os, "name", "nt")
    original_reader = extract_worker.READERS["csv"]
    monkeypatch.setitem(extract_worker.READERS, "csv", lambda src, w: (order.append("parsed"), original_reader(src, w))[1])
    result = run_worker_main(monkeypatch, capsys, _request(tmp_path, memory_limit=123 * MB))
    assert result["ok"] is True and order == ["capped", "parsed"]
    # Its own job: the requested limit, one process, and not killed when the handle closes (it lives with the process).
    assert jobs == [{"memory_limit": 123 * MB, "active_processes": 1, "kill_on_close": False}]


def test_the_hub_passes_the_memory_limit_to_the_worker_and_its_job(tmp_path: Path, monkeypatch) -> None:
    jobs: list[tuple[int, dict]] = []
    real_job = extract.WindowsJob

    def recording_job(memory_limit: int, **options):
        jobs.append((memory_limit, options))
        return real_job(memory_limit, **options)

    script = tmp_path / "echo_worker.py"
    script.write_text(textwrap.dedent("""
        import json, sys
        request = json.loads(sys.stdin.buffer.read())
        open(request["dst"], "w").write(str(request["memory_limit"]))
        print(json.dumps({"ok": True}))
    """), encoding="utf-8")
    monkeypatch.setattr(extract, "worker_command", lambda: [sys.executable, "-I", str(script)])
    if os.name == "nt":
        monkeypatch.setattr(extract, "WindowsJob", recording_job)
    original = tmp_path / "data.csv"
    original.write_text("a,b\n")
    copy = asyncio.run(make_text_copy("csv", original, memory_limit=300 * MB))
    assert copy.path.read_text() == str(300 * MB)
    if os.name == "nt":
        assert jobs == [(300 * MB, {"active_processes": extract.ACTIVE_PROCESSES})]


def _process_ended(pid: int, wait_ms: int = 5000) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not handle:
        return True  # already gone
    try:
        return kernel32.WaitForSingleObject(handle, wait_ms) == 0
    finally:
        kernel32.CloseHandle(handle)


@windows_only
@pytest.mark.parametrize("attempt", range(3))
def test_a_self_capped_worker_is_still_killed_at_the_timeout(tmp_path: Path, monkeypatch, attempt: int) -> None:
    """The worker's own job isn't killed on close; the hub's job (and kill) must still end the interpreter, which
    in a venv is the launcher's child."""
    pid_file = tmp_path / "pid"
    script = tmp_path / "stuck_worker.py"
    script.write_text(textwrap.dedent(f"""
        import json, os, sys, time
        sys.path.insert(0, {str(BACKEND)!r})
        from app.extract_worker import cap_memory
        request = json.loads(sys.stdin.buffer.read())
        cap_memory(int(request["memory_limit"]))
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        open(request["dst"], "w").write("partial")
        time.sleep(120)
    """), encoding="utf-8")
    monkeypatch.setattr(extract, "worker_command", lambda: [sys.executable, "-I", str(script)])
    original = tmp_path / "data.csv"
    original.write_text("a,b\n")
    started = time.monotonic()
    copy = asyncio.run(make_text_copy("csv", original, timeout=4))
    assert copy.path is None and "longer than 4 seconds" in copy.error
    assert time.monotonic() - started < 15
    assert pid_file.exists(), "the stuck worker never got going"
    assert _process_ended(int(pid_file.read_text()))
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("data")) == ["data.csv"]


def test_the_bundled_app_runs_the_worker_from_its_own_exe(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert extract.worker_command() == [sys.executable, extract.WORKER_FLAG]
    monkeypatch.delattr(sys, "frozen")
    assert extract.worker_command() == [sys.executable, "-I", str(extract.WORKER)]


def test_hub_backend_extract_worker_entry_point(tmp_path: Path) -> None:
    """hub_backend.py --extract-worker is what the bundled exe runs; it must cap itself and write the copy."""
    source = tmp_path / "data.csv"
    source.write_text("name;qty\napple;3\n", encoding="utf-8")
    destination = tmp_path / "data.csv.txt.part"
    request = {"kind": "csv", "src": str(source), "dst": str(destination), "max_chars": 10_000,
               "memory_limit": 256 * MB}
    result = subprocess.run([sys.executable, "hub_backend.py", "--extract-worker"], cwd=BACKEND,
                            input=json.dumps(request).encode(), capture_output=True, timeout=60)
    reply = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert reply["ok"] is True, (reply, result.stderr.decode())
    assert "apple\t3" in destination.read_text(encoding="utf-8")
    # Without a limit it refuses, as the worker file does.
    del request["memory_limit"]
    result = subprocess.run([sys.executable, "hub_backend.py", "--extract-worker"], cwd=BACKEND,
                            input=json.dumps(request).encode(), capture_output=True, timeout=60)
    assert json.loads(result.stdout.decode().strip().splitlines()[-1])["ok"] is False


# --- Claude's per-request block budget ---------------------------------------------------------------------


def fake(name: str, size: int, pages: int | None = None, folder: Path | None = None, **extra) -> Attachment:
    """An attachment with a declared size (the budget reads `size`, not the file)."""
    kind = "image" if name.endswith(".png") else "pdf"
    mime = "image/png" if kind == "image" else "application/pdf"
    path = (folder / name) if folder else Path(f"C:/x/{name}")
    if folder:
        path.write_bytes(samples.png() if kind == "image" else samples.pdf())
    return Attachment(id=uuid.uuid4().hex[:12], name=name, mime=mime, size=size, path=str(path),
                      text_path=extra.pop("text_path", None), kind=kind, pages=pages, **extra)


def b64(size: int) -> int:
    return (size + 2) // 3 * 4


def test_page_budget_is_exact_at_one_hundred_pages() -> None:
    a, b = fake("a.pdf", 1000, pages=60), fake("b.pdf", 1000, pages=40)
    assert claude_blocks([a, b]) == [a, b]  # exactly PDF_BLOCK_PAGES
    c = fake("c.pdf", 1000, pages=1)
    assert claude_blocks([a, b, c]) == [a, b]
    # Images don't use pages: they still go after the pages ran out.
    shot = fake("shot.png", 1000)
    assert claude_blocks([a, b, c, shot]) == [a, b, shot]
    assert PDF_BLOCK_PAGES == 100


def test_byte_budget_mixes_pdfs_and_images_in_order() -> None:
    pdf = fake("big.pdf", 20 * MB, pages=10)  # about 26.7 MB of base64
    image = fake("photo.png", 20 * MB)  # counts as IMAGE_SENT_BYTES (Claude Code downsizes it)
    second = fake("photo2.png", 20 * MB)  # would pass 32 MB
    tiny = fake("icon.png", 3000)  # counts its real base64 size, and still fits
    late_pdf = fake("late.pdf", 4 * MB, pages=2)  # 5.3 MB of base64: over
    assert b64(20 * MB) + IMAGE_SENT_BYTES <= PDF_BLOCK_BYTES < b64(20 * MB) + 2 * IMAGE_SENT_BYTES
    assert claude_blocks([pdf, image, second, tiny, late_pdf]) == [pdf, image, tiny]
    # First come, first served: the same files the other way round keep the images and drop the big PDF.
    assert claude_blocks([image, second, tiny, late_pdf, pdf]) == [image, second, tiny, late_pdf]


def test_byte_budget_is_exact_at_the_limit() -> None:
    raw = PDF_BLOCK_BYTES // 4 * 3  # base64 of exactly PDF_BLOCK_BYTES
    exact = fake("exact.pdf", raw, pages=1)
    assert b64(raw) == PDF_BLOCK_BYTES and claude_blocks([exact]) == [exact]
    assert claude_blocks([fake("over.pdf", raw + 1, pages=1)]) == []
    # An image counts at most IMAGE_SENT_BYTES however big, but its real size when smaller.
    images = [fake(f"s{i}.png", IMAGE_SENT_BYTES // 4 * 3) for i in range(PDF_BLOCK_BYTES // IMAGE_SENT_BYTES)]
    room = PDF_BLOCK_BYTES - len(images) * IMAGE_SENT_BYTES
    filler = fake("filler.pdf", room // 4 * 3, pages=1)  # fills the request to exactly PDF_BLOCK_BYTES
    assert claude_blocks([*images, filler]) == [*images, filler]
    assert claude_blocks([*images, filler, fake("one-more.png", 3)]) == [*images, filler]


def test_overflowed_files_are_listed_by_path_with_their_text_copy(tmp_path: Path) -> None:
    first = fake("first.pdf", 2000, pages=70, folder=tmp_path, text_path="C:/t/first.pdf.txt")
    second = fake("second.pdf", 2000, pages=70, folder=tmp_path, text_path="C:/t/second.pdf.txt")
    shot = fake("shot.png", 2000, folder=tmp_path)
    content = json.loads(user_line("Compare", [first, second, shot]))["message"]["content"]
    assert [b["type"] for b in content] == ["text", "document", "image"]
    text = content[0]["text"]
    assert f"`{first.path}` (also attached to this message); text copy: `C:/t/first.pdf.txt`" in text
    assert f"`{second.path}`; text copy: `C:/t/second.pdf.txt`" in text  # by path only
    assert f"`{shot.path}` (also attached to this message)" in text


# --- Read-only originals -----------------------------------------------------------------------------------


def test_originals_resist_the_ways_agents_write(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    report = save(store, "report.pdf", samples.pdf())
    replacement = tmp_path / "new.pdf"
    replacement.write_bytes(b"%PDF-1.4 changed")
    for target in (Path(report.path), Path(report.text_path)):
        with pytest.raises(PermissionError):
            open(target, "r+b").close()  # in-place edit
        with pytest.raises(PermissionError):
            open(target, "ab").close()  # append
        with pytest.raises(PermissionError):
            os.replace(replacement, target)  # atomic save over it
        with pytest.raises(PermissionError):
            target.unlink()
    assert Path(report.path).read_bytes() == samples.pdf()
    # The hub's own metadata stays writable, for mark_sent.
    store.mark_sent(SESSION, [report])
    assert store._read_meta(SESSION, report.id)[1] is True


def test_the_sweep_removes_read_only_uploads_with_their_text_copies(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    old = save(store, "report.pdf", samples.pdf())
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(Path(old.path).parent / META_FILE, (long_ago, long_ago))
    assert store.sweep_unsent() == 1
    assert not Path(old.path).parent.exists()


# --- Mark of the Web ---------------------------------------------------------------------------------------


@windows_only
def test_only_originals_are_marked_as_downloaded(tmp_path: Path) -> None:
    """The text copy is the hub's own plain text, opened by nothing that honours Protected View, so it has no mark."""
    store = AttachmentStore(tmp_path)
    for name, data in (("letter.docx", samples.docx()), ("fruit.xlsx", samples.xlsx()), ("shot.png", samples.png())):
        attachment = save(store, name, data)
        assert "ZoneId=3" in Path(f"{attachment.path}:Zone.Identifier").read_text(encoding="ascii")
        assert Path(attachment.path).read_bytes() == data  # the stream doesn't touch the contents
        if attachment.text_path:
            assert not Path(f"{attachment.text_path}:Zone.Identifier").exists()
        assert not Path(f"{Path(attachment.path).parent / META_FILE}:Zone.Identifier").exists()


def test_a_drive_without_streams_still_takes_uploads(tmp_path: Path, monkeypatch) -> None:
    real_open = open

    def no_streams(file, *args, **kwargs):
        if str(file).endswith(":Zone.Identifier"):
            raise OSError(22, "The parameter is incorrect")  # what FAT32 or a network share answers
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(attachments_module, "open", no_streams, raising=False)
    monkeypatch.setattr(attachments_module.os, "name", "nt")
    # Built once: python-docx stamps the save time into the zip, so two builds differ across a 2-second boundary.
    data = samples.docx()
    attachment = save(AttachmentStore(tmp_path), "letter.docx", data)
    assert Path(attachment.path).read_bytes() == data and attachment.text_path


# --- Workbook formulas -------------------------------------------------------------------------------------

SHEET_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>n</t></is></c><c r="B1" t="inlineStr"><is><t>double</t></is></c></row>
<row r="2"><c r="A2"><v>1</v></c><c r="B2"><f t="shared" ref="B2:B4" si="0">A2*2</f></c></row>
<row r="3"><c r="A3"><v>2</v></c><c r="B3"><f t="shared" si="0"/></c></row>
<row r="4"><c r="A4"><v>3</v></c><c r="B4"><f t="shared" si="0"/><v>6</v></c></row>
<row r="5"><c r="A5"><f t="array" ref="A5">SUM(A2:A4*2)</f></c><c r="B5"><f>SUM(B2:B4)</f><v>12</v></c>
<c r="C5"><f>B5-12</f><v>0</v></c></row>
</sheetData></worksheet>"""


def _workbook_with_sheet(sheet_xml: bytes) -> bytes:
    """A real workbook from openpyxl, with its sheet's XML replaced (openpyxl can't write cached values or shared
    formulas, which is exactly what Excel and other tools store)."""
    from openpyxl import Workbook

    buffer = io.BytesIO()
    Workbook().save(buffer)
    source = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as workbook:
        for item in source.infolist():
            data = sheet_xml if item.filename == "xl/worksheets/sheet1.xml" else source.read(item.filename)
            workbook.writestr(item.filename, data)
    return out.getvalue()


def test_formula_fallback_covers_shared_and_array_formulas_and_prefers_values(tmp_path: Path) -> None:
    attachment = save(AttachmentStore(tmp_path), "calc.xlsx", _workbook_with_sheet(SHEET_XML))
    lines = Path(attachment.text_path).read_text(encoding="utf-8").splitlines()
    rows = lines[lines.index("--- Sheet: Sheet ---") + 1:]
    assert rows[0] == "n\tdouble"
    assert rows[1] == "1\t=A2*2"  # a shared formula's first cell
    assert rows[2] == "2\t=A3*2"  # the shared formula, translated to its own row
    assert rows[3] == "3\t6"  # a stored value wins over the formula
    assert rows[4] == "=SUM(A2:A4*2)\t12\t0"  # array formula text; a stored 0 is a value, not "missing"


# --- The startup sweep -------------------------------------------------------------------------------------


def _age(attachment: Attachment, seconds: float) -> None:
    when = time.time() - seconds
    os.utime(Path(attachment.path).parent / META_FILE, (when, when))


def test_sweep_age_boundary(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    younger = save(store, "younger.txt", b"x")
    older = save(store, "older.txt", b"x")
    _age(younger, 3600 - 60)
    _age(older, 3600 + 60)
    assert store.sweep_unsent(max_age=3600) == 1
    assert Path(younger.path).exists() and not Path(older.path).exists()
    assert attachments_module.UNSENT_MAX_AGE == 24 * 3600


def test_sweep_survives_unreadable_metadata_and_stray_entries(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    folders = {}
    for name, meta in (("broken", "{not json"), ("listed", "[1, 2]"), ("number", "7"), ("null", "null")):
        attachment = save(store, f"{name}.txt", b"x")
        (Path(attachment.path).parent / META_FILE).write_text(meta, encoding="utf-8")
        _age(attachment, 3 * 24 * 3600)
        folders[name] = Path(attachment.path).parent
    (tmp_path / "stray-file.txt").write_text("not a session")
    (store.session_dir(SESSION) / "not-an-id").mkdir()
    (store.session_dir(SESSION) / "loose.txt").write_text("x")
    store.sweep_unsent()  # must not raise: it runs at startup, before the app serves anything
    for name, folder in folders.items():
        assert folder.exists(), name  # metadata it can't read is left alone, never guessed at
    assert (store.session_dir(SESSION) / "not-an-id").exists() and (tmp_path / "stray-file.txt").exists()


def test_sweep_leaves_an_upload_in_progress_alone(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    halfway = threading.Event()
    go_on = threading.Event()

    async def slow_chunks():
        yield b"first half "
        halfway.set()
        await asyncio.to_thread(go_on.wait, 10)
        yield b"second half"

    result: list[Attachment] = []
    upload_thread = threading.Thread(target=lambda: result.append(asyncio.run(store.save(SESSION, "live.txt",
                                                                                          slow_chunks()))))
    upload_thread.start()
    assert halfway.wait(10)
    # No metadata yet and a fresh folder: even a sweep with a short max age leaves it.
    assert store.sweep_unsent(max_age=60) == 0
    go_on.set()
    upload_thread.join(10)
    assert Path(result[0].path).read_bytes() == b"first half second half"
    assert store.get(SESSION, result[0].id) == result[0]


def test_sweep_at_startup_never_stops_the_app(engine, tmp_path: Path) -> None:
    from app.main import create_app
    from conftest import TEST_TOKEN, fake_adapter_factory, make_client

    store = AttachmentStore(tmp_path / "attachments")
    attachment = save(store, "odd.txt", b"x")
    (Path(attachment.path).parent / META_FILE).write_text("[]", encoding="utf-8")
    _age(attachment, 3 * 24 * 3600)
    app = create_app(engine, fake_adapter_factory, token=TEST_TOKEN, attachments_dir=tmp_path / "attachments",
                     chats_dir=tmp_path / "chats", skills_dir=tmp_path / "skills", codex_home=tmp_path / "codex",
                     claude_home=tmp_path / "claude", discover_native=False)
    with make_client(app) as client:
        assert client.get("/api/health").status_code == 200


# --- Inline fences -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("content, fence", [
    (b"no backticks at all\n", "```"),
    (b"inline `code` only\n", "```"),
    (b"seven ``````` in a row\n", "````````"),
    (b"ends with a fence\n````", "`````"),
])
def test_inline_fence_is_longer_than_any_run(tmp_path: Path, content: bytes, fence: str) -> None:
    note = stored(tmp_path, "notes.md", content)
    text = with_files("Look", [note], inline_text=True)
    body = text.split("### Contents of notes.md\n\n", 1)[1]
    assert body.startswith(fence + "\n") and not body.startswith(fence + "`")
    assert body.endswith("\n" + fence)
    assert content.decode().rstrip() in body


# --- At most two extractions at once -----------------------------------------------------------------------


@pytest.mark.parametrize("attempt", range(2))
def test_a_third_extraction_waits_for_a_slot(tmp_path: Path, monkeypatch, attempt: int) -> None:
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    script = tmp_path / "slow_worker.py"
    script.write_text(textwrap.dedent(f"""
        import json, os, sys, time
        request = json.loads(sys.stdin.buffer.read())
        name = os.path.basename(request["src"])
        start = time.time()
        time.sleep(1.5)
        open(os.path.join({str(log_dir)!r}, name), "w").write(json.dumps([start, time.time()]))
        open(request["dst"], "w").write("done")
        print(json.dumps({{"ok": True}}))
    """), encoding="utf-8")
    monkeypatch.setattr(extract, "worker_command", lambda: [sys.executable, "-I", str(script)])
    originals = []
    for index in range(3):
        original = tmp_path / f"f{index}.csv"
        original.write_text("a,b\n")
        originals.append(original)

    async def all_three():
        return await asyncio.gather(*(make_text_copy("csv", o) for o in originals))

    copies = asyncio.run(all_three())
    assert all(copy.path for copy in copies)
    spans = sorted(json.loads((log_dir / o.name).read_text()) for o in originals)
    # The third started only once one of the first two had finished.
    assert spans[2][0] >= min(spans[0][1], spans[1][1]) - 0.05, spans
    assert extract.CONCURRENT == 2


# --- Deleting a session while a file is held open ----------------------------------------------------------


@windows_only
def test_deleting_a_session_with_a_file_held_open(client: TestClient, tmp_path: Path, caplog) -> None:
    session_id = create(client, tmp_path, "ok")
    note = upload(client, session_id, "notes.txt", b"hello")
    other = upload(client, session_id, "other.txt", b"bye")
    caplog.set_level(logging.WARNING)
    with open(note["path"], "rb"):  # a viewer has it open
        response = client.delete(f"/api/sessions/{session_id}")
    # The session goes either way; the files that could go are gone, and the leftover is logged.
    assert response.status_code == 204
    assert client.get(f"/api/sessions/{session_id}").status_code == 404
    assert not Path(other["path"]).exists()
    assert Path(note["path"]).exists() and "Couldn't delete everything" in caplog.text
    assert client.app.state.orchestrator.attachments.delete_session(session_id) is True


@pytest.mark.parametrize("meta", ["[]", "null", '{"id": "x"}', "{broken"])
def test_metadata_that_isnt_the_hubs_is_an_unknown_attachment(client: TestClient, tmp_path: Path, meta: str) -> None:
    session_id = create(client, tmp_path, "ok")
    note = upload(client, session_id, "notes.txt", b"x")
    (Path(note["path"]).parent / META_FILE).write_text(meta, encoding="utf-8")
    base = f"/api/sessions/{session_id}/attachments/{note['id']}"
    assert client.get(base).status_code == 404
    assert client.get(f"{base}/file").status_code == 404
    assert client.delete(base).status_code == 404
    response = client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi", "attachments": [note["id"]]})
    assert response.status_code == 400
