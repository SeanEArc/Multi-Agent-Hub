"""Attachments: allowed types, storage, text copies (separate capped process), prompt helpers and the handoff brief."""

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app import attachments as attachments_module
from app.adapters.base import EventType
from app.attachments import (
    META_FILE,
    Attachment,
    AttachmentError,
    AttachmentStore,
    check_content,
    claude_block_ok,
    claude_blocks,
    classify,
    openable,
    safe_name,
    with_files,
)
from app.extract import make_text_copy, text_copy_path
from app.handoff import build_brief, messages_brief, recent_images, session_attachments
from app.models import AgentKind, SessionEvent, StintReason

import attachment_samples as samples

SESSION = str(uuid.uuid4())


async def _chunks(data: bytes, size: int = 1000) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start:start + size]


def save(store: AttachmentStore, name: str, data: bytes, **kwargs) -> Attachment:
    return asyncio.run(store.save(SESSION, name, _chunks(data), **kwargs))


# --- Types and names -------------------------------------------------------------------------------


def test_classify_allowed_and_refused_types() -> None:
    assert classify("Shot.PNG") == ("image", "image/png")
    assert classify("a.jpeg")[0] == "image"
    assert classify("report.pdf") == ("pdf", "application/pdf")
    assert classify("letter.docx")[0] == "word"
    assert classify("old.xls") == ("excel", "application/vnd.ms-excel")
    assert classify("data.csv") == ("csv", "text/csv")
    assert classify("main.py") == ("text", "text/plain")
    assert classify("Dockerfile") == ("text", "text/plain")
    for refused in ("setup.exe", "movie.mp4", "deck.pptx", "logo.svg", "archive.zip", "noext"):
        with pytest.raises(AttachmentError) as caught:
            classify(refused)
        assert caught.value.status == 415


def test_safe_name_strips_folders_commas_and_reserved_names() -> None:
    assert safe_name("..\\..\\evil.png") == "evil.png"
    assert safe_name("a,b,c.png") == "a_b_c.png"  # Codex's --image splits on commas
    assert safe_name('bad<>:"|?*.txt') == "bad_______.txt"
    assert safe_name("CON.txt") == "_CON.txt"
    assert len(safe_name("x" * 300 + ".pdf")) <= attachments_module.MAX_NAME


def test_check_content_refuses_a_renamed_file() -> None:
    check_content("image", "image/png", samples.png())
    check_content("pdf", "application/pdf", samples.pdf())
    check_content("text", "text/plain", b"print('hi')\n")
    with pytest.raises(AttachmentError, match="don't match"):
        check_content("image", "image/png", b"MZ\x90\x00 a program")
    with pytest.raises(AttachmentError, match="don't match"):
        check_content("text", "text/plain", b"\x00\x01\x02binary")
    check_content("image", "image/webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ")


def test_openable_never_starts_code() -> None:
    def attachment(name: str) -> Attachment:
        kind, mime = classify(name)
        return Attachment(id="a" * 12, name=name, mime=mime, size=1, path=f"C:/x/{name}", text_path=None, kind=kind)

    assert openable(attachment("shot.png")) and openable(attachment("report.pdf")) and openable(attachment("a.xlsx"))
    assert openable(attachment("notes.txt"))
    for script in ("run.ps1", "build.bat", "tool.cmd", "app.py", "page.html"):
        assert not openable(attachment(script))


# --- Storage ---------------------------------------------------------------------------------------


def test_save_stores_the_original_untouched_with_metadata(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    data = samples.png()
    attachment = save(store, "screen shot.png", data)
    path = Path(attachment.path)
    assert path.read_bytes() == data
    assert path.parent.parent == store.session_dir(SESSION)
    assert attachment.size == len(data) and attachment.kind == "image" and attachment.text_path is None
    assert store.get(SESSION, attachment.id) == attachment
    assert json.loads((path.parent / META_FILE).read_text())["sent"] is False


def test_save_refuses_over_the_cap_bad_content_and_empty_files(tmp_path: Path, monkeypatch) -> None:
    store = AttachmentStore(tmp_path)
    monkeypatch.setitem(attachments_module.SIZE_CAPS, "image", 50)
    with pytest.raises(AttachmentError) as caught:
        save(store, "big.png", samples.png() + b"\x00" * 100)
    assert caught.value.status == 413
    with pytest.raises(AttachmentError) as caught:
        save(store, "big.png", b"", declared_size=51)  # refused before reading
    assert caught.value.status == 413
    with pytest.raises(AttachmentError) as caught:
        save(store, "fake.pdf", b"not a pdf at all")
    assert caught.value.status == 415
    with pytest.raises(AttachmentError, match="empty"):
        save(store, "empty.txt", b"")
    # Nothing is left behind by a refused upload.
    assert not store.session_dir(SESSION).exists() or not any(store.session_dir(SESSION).iterdir())


def test_load_mark_sent_discard_and_delete(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    first = save(store, "a.txt", b"one")
    second = save(store, "b.txt", b"two")
    assert store.load(SESSION, [second.id, first.id, second.id]) == [second, first]
    with pytest.raises(AttachmentError):
        store.load(SESSION, ["000000000000"])
    with pytest.raises(AttachmentError):
        store.get(SESSION, "../../etc")  # not an id
    store.mark_sent(SESSION, [first])
    with pytest.raises(AttachmentError) as caught:
        store.discard(SESSION, first.id)
    assert caught.value.status == 409
    store.discard(SESSION, second.id)
    assert not Path(second.path).exists()
    store.delete_session(SESSION)
    assert not store.session_dir(SESSION).exists()


def test_file_refuses_paths_outside_the_session(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path / "attachments")
    attachment = save(store, "a.txt", b"hello")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    meta = Path(attachment.path).parent / META_FILE
    meta.write_text(json.dumps({**attachment.to_json(), "path": str(outside), "sent": True}))
    with pytest.raises(AttachmentError) as caught:
        store.file(SESSION, attachment.id)
    assert caught.value.status == 404


# --- Text copies -----------------------------------------------------------------------------------


@pytest.mark.parametrize("name, data, expected", [
    ("report.pdf", samples.pdf(), samples.PDF_WORD),
    ("letter.docx", samples.docx(), "# Quarterly report"),
    ("fruit.xlsx", samples.xlsx(), "pear\t7"),
    ("table.csv", b"name;qty\napple;3\n", "apple\t3"),
])
def test_text_copies_are_written_beside_the_original(tmp_path: Path, name: str, data: bytes, expected: str) -> None:
    attachment = save(AttachmentStore(tmp_path), name, data)
    assert attachment.text_path == str(text_copy_path(Path(attachment.path)))
    assert expected in Path(attachment.text_path).read_text(encoding="utf-8")
    assert Path(attachment.path).read_bytes() == data  # the original is never changed
    if name.endswith(".pdf"):
        assert attachment.pages == 1


def test_word_tables_keep_their_cells(tmp_path: Path) -> None:
    attachment = save(AttachmentStore(tmp_path), "letter.docx", samples.docx())
    text = Path(attachment.text_path).read_text(encoding="utf-8")
    assert "region\tsales" in text and "north\t42" in text


def test_text_copy_timeout_leaves_nothing(tmp_path: Path) -> None:
    original = tmp_path / "report.pdf"
    original.write_bytes(samples.pdf())
    copy = asyncio.run(make_text_copy("pdf", original, timeout=0.001))
    assert copy.path is None and "longer than" in copy.error
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.pdf"]


@pytest.mark.skipif(os.name != "nt", reason="the job object is Windows-only")
def test_text_copy_memory_cap_is_enforced(tmp_path: Path) -> None:
    original = tmp_path / "report.pdf"
    original.write_bytes(samples.pdf())
    # 20 MB is too little for Python to even import the PDF reader inside the job.
    copy = asyncio.run(make_text_copy("pdf", original, memory_limit=20 * 1024 * 1024))
    assert copy.path is None and "memory" in copy.error
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.pdf"]


def test_text_copy_is_capped(tmp_path: Path) -> None:
    original = tmp_path / "big.csv"
    original.write_text("".join(f"row{i},{i}\n" for i in range(5000)))
    copy = asyncio.run(make_text_copy("csv", original, max_chars=1000))
    text = copy.path.read_text(encoding="utf-8")
    assert "cut at 1,000 characters" in text and len(text) < 1200


# --- Prompt helpers --------------------------------------------------------------------------------


def _attachment(tmp_path: Path, name: str, data: bytes | None = None, **extra) -> Attachment:
    kind, mime = classify(name)
    folder = tmp_path / uuid.uuid4().hex[:12]
    folder.mkdir()
    path = folder / name
    path.write_bytes(data if data is not None else b"x")
    return Attachment(id=folder.name, name=name, mime=mime, size=path.stat().st_size, path=str(path),
                      text_path=extra.pop("text_path", None), kind=kind, **extra)


def test_with_files_lists_paths_text_copies_and_what_came_attached(tmp_path: Path) -> None:
    image = _attachment(tmp_path, "shot.png", samples.png())
    sheet = _attachment(tmp_path, "data.xlsx", text_path="C:/x/data.xlsx.txt")
    broken = _attachment(tmp_path, "bad.pdf", note="the PDF is password-protected")
    text = with_files("What's wrong?", [image, sheet, broken], attached={image.id})
    assert text.startswith("What's wrong?\n\n## Attached files")
    assert f"`{image.path}` (also attached to this message)" in text
    assert "text copy: `C:/x/data.xlsx.txt`" in text
    assert "no text copy could be made: the PDF is password-protected" in text
    assert with_files("plain", []) == "plain"
    assert with_files("", [sheet]).startswith("## Attached files")


def test_with_files_inlines_text_copies_within_a_budget(tmp_path: Path, monkeypatch) -> None:
    copy = tmp_path / "data.xlsx.txt"
    copy.write_text("apple\t3\n" * 10, encoding="utf-8")
    sheet = _attachment(tmp_path, "data.xlsx", text_path=str(copy))
    code = _attachment(tmp_path, "main.py", b"print('hi')\n")
    text = with_files("Look", [sheet, code], inline_text=True)
    assert "### Text of data.xlsx" in text and "apple\t3" in text
    assert "### Contents of main.py" in text and "print('hi')" in text
    monkeypatch.setattr(attachments_module, "INLINE_CHARS", 20)
    assert "more characters; read the file for the rest" in with_files("Look", [sheet], inline_text=True)


def test_claude_takes_images_and_short_pdfs_as_blocks(tmp_path: Path) -> None:
    assert claude_block_ok(_attachment(tmp_path, "shot.png"))
    assert claude_block_ok(_attachment(tmp_path, "short.pdf", pages=3))
    assert not claude_block_ok(_attachment(tmp_path, "long.pdf", pages=101))
    assert not claude_block_ok(_attachment(tmp_path, "unknown.pdf"))  # page count unknown
    assert not claude_block_ok(_attachment(tmp_path, "data.xlsx"))


def test_claude_block_budget_is_per_request(tmp_path: Path) -> None:
    first = _attachment(tmp_path, "a.pdf", pages=60)
    second = _attachment(tmp_path, "b.pdf", pages=60)
    small = _attachment(tmp_path, "c.pdf", pages=30)
    images = [_attachment(tmp_path, f"shot{i}.png") for i in range(3)]
    # 60 + 60 pages is over 100: the second goes by path, a later 30-page one still fits.
    assert claude_blocks([first, second, small, *images]) == [first, small, *images]
    # Sizes add up too (base64); images count at most IMAGE_SENT_BYTES, since Claude Code downsizes them.
    big = [Attachment(id=f"{i:012x}", name=f"big{i}.png", mime="image/png", size=20 * attachments_module.MB,
                      path=f"C:/x/big{i}.png", text_path=None, kind="image") for i in range(8)]
    assert len(claude_blocks(big)) == attachments_module.PDF_BLOCK_BYTES // attachments_module.IMAGE_SENT_BYTES
    heavy = Attachment(id="f" * 12, name="heavy.pdf", mime="application/pdf", size=20 * attachments_module.MB,
                       path="C:/x/heavy.pdf", text_path=None, kind="pdf", pages=5)
    assert claude_blocks([heavy, heavy.__class__(**{**heavy.to_json(), "id": "e" * 12})]) == [heavy]


# --- Handoff ---------------------------------------------------------------------------------------


def _event(event_id: int, event_type: EventType, payload: dict, stint_id: int = 1) -> SessionEvent:
    return SessionEvent(id=event_id, session_id=SESSION, stint_id=stint_id, agent=AgentKind.CLAUDE, type=event_type,
                        payload=payload)


def test_brief_lists_attachments_and_recent_images_go_again(tmp_path: Path) -> None:
    images = [_attachment(tmp_path, f"shot{i}.png", samples.png()) for i in range(7)]
    sheet = _attachment(tmp_path, "data.xlsx", text_path="C:/x/data.xlsx.txt")
    gone = _attachment(tmp_path, "gone.png", samples.png())
    Path(gone.path).unlink()
    events = [
        _event(1, EventType.USER_MESSAGE, {"text": "first", "attachments": [images[0].to_json(), sheet.to_json()]}),
        _event(2, EventType.USER_MESSAGE, {"text": "no files"}),
        _event(3, EventType.USER_MESSAGE, {"text": "more", "attachments": [i.to_json() for i in images[1:5]]}),
        _event(4, EventType.USER_MESSAGE, {"text": "last", "attachments": [*(i.to_json() for i in images[5:]),
                                                                           gone.to_json()]}),
    ]
    assert [a.id for a in session_attachments(events)] == [a.id for a in [images[0], sheet, *images[1:], gone]]
    # The newest five images that are still on disk, oldest first.
    assert [a.name for a in recent_images(events)] == ["shot2.png", "shot3.png", "shot4.png", "shot5.png",
                                                       "shot6.png"]
    assert images[6].id not in [a.id for a in recent_images(events, exclude=[images[6].id])]

    brief = build_brief(events, 1, AgentKind.CLAUDE, AgentKind.CODEX, StintReason.LIMIT, None)
    assert "## Files the user attached" in brief
    assert f"data.xlsx (Excel workbook): `{sheet.path}`; text copy: `C:/x/data.xlsx.txt`" in brief
    assert "Images from the user's last messages come with this message again" in brief
    assert "## Files the user attached" in messages_brief(events, AgentKind.CODEX)
    assert "Files the user attached" not in build_brief(events[1:2], 1, AgentKind.CLAUDE, AgentKind.CODEX,
                                                        StintReason.LIMIT, None)


# --- The worker's own memory cap, unmocked -------------------------------------------------------------

BACKEND = Path(__file__).resolve().parent.parent
ALLOCATE = """
import sys
sys.path.insert(0, sys.argv[1])
from app.extract_worker import cap_memory
if sys.argv[2] == "cap":
    cap_memory(256 * 1024 * 1024)
try:
    block = bytearray(600 * 1024 * 1024)
    print("ALLOCATED")
except MemoryError:
    print("MEMORY_ERROR")
"""


@pytest.mark.skipif(os.name != "nt", reason="the job object is Windows-only")
@pytest.mark.parametrize("attempt", range(5))
def test_worker_cap_holds_in_the_process_that_parses(attempt: int) -> None:
    """The cap is set from inside the interpreter, so it holds even when sys.executable is a venv launcher whose
    child could start outside a job assigned from the hub (QA reproduced that with the old hub-only job)."""
    import subprocess

    result = subprocess.run([sys.executable, "-I", "-c", ALLOCATE, str(BACKEND), "cap"], capture_output=True,
                            text=True, timeout=60)
    assert result.stdout.strip() == "MEMORY_ERROR", result.stderr
    if attempt == 0:  # the same allocation without the cap succeeds, so the check means something
        control = subprocess.run([sys.executable, "-I", "-c", ALLOCATE, str(BACKEND), "nocap"],
                                 capture_output=True, text=True, timeout=60)
        assert control.stdout.strip() == "ALLOCATED", control.stderr


def _docx_bomb(path: Path, megabytes: int = 700) -> None:
    """A Word file under 1 MB whose document.xml unpacks to `megabytes` of text."""
    import io
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(samples.docx()))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bomb:
        for item in source.infolist():
            if item.filename != "word/document.xml":
                bomb.writestr(item, source.read(item.filename))
                continue
            with bomb.open("word/document.xml", "w", force_zip64=True) as xml:
                xml.write(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w='
                          b'"http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>')
                chunk = b"a" * (1 << 20)
                for _ in range(megabytes):
                    xml.write(chunk)
                xml.write(b"</w:t></w:r></w:p></w:body></w:document>")


@pytest.mark.skipif(os.name != "nt", reason="the job object is Windows-only")
@pytest.mark.parametrize("attempt", range(2))
def test_a_docx_bomb_is_stopped_by_the_cap(tmp_path: Path, attempt: int) -> None:
    bomb = tmp_path / "bomb.docx"
    _docx_bomb(bomb)
    assert bomb.stat().st_size < 2 * 1024 * 1024
    copy = asyncio.run(make_text_copy("word", bomb))
    assert copy.path is None and copy.error, copy
    assert not text_copy_path(bomb).exists()


# --- Protecting the originals, and cleaning up ------------------------------------------------------------


def test_originals_and_text_copies_are_read_only_and_marked_as_downloaded(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    report = save(store, "report.pdf", samples.pdf())
    for stored in (Path(report.path), Path(report.text_path)):
        with pytest.raises(PermissionError):
            stored.write_bytes(b"changed")  # what an agent's edit would do
    assert Path(report.path).read_bytes() == samples.pdf()
    meta = Path(report.path).parent / META_FILE
    store.mark_sent(SESSION, [report])  # the metadata stays writable
    assert json.loads(meta.read_text())["sent"] is True
    if os.name == "nt":
        zone = Path(f"{report.path}:Zone.Identifier").read_text(encoding="ascii")
        assert "[ZoneTransfer]" in zone and "ZoneId=3" in zone


def test_discard_and_delete_remove_read_only_files(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    unsent = save(store, "fruit.xlsx", samples.xlsx())
    store.discard(SESSION, unsent.id)
    assert not Path(unsent.path).parent.exists()
    sent = save(store, "report.pdf", samples.pdf())
    store.mark_sent(SESSION, [sent])
    assert store.delete_session(SESSION) is True
    assert not store.session_dir(SESSION).exists()


@pytest.mark.skipif(os.name != "nt", reason="an open file blocks deletion only on Windows")
def test_delete_reports_files_it_couldnt_remove(tmp_path: Path, caplog) -> None:
    store = AttachmentStore(tmp_path)
    note = save(store, "notes.txt", b"hello")
    with open(note.path, "rb"):  # a viewer still has it open
        assert store.delete_session(SESSION) is False
    assert "Couldn't delete everything" in caplog.text
    assert store.delete_session(SESSION) is True


def test_startup_sweep_removes_only_old_unsent_uploads(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    old_unsent = save(store, "old.txt", b"old")
    new_unsent = save(store, "new.txt", b"new")
    old_sent = save(store, "kept.txt", b"kept")
    store.mark_sent(SESSION, [old_sent])
    crashed = store.session_dir(SESSION) / "abcdefabcdef"  # a folder a crash left without metadata
    crashed.mkdir()
    long_ago = time.time() - 2 * 24 * 3600
    for folder in (Path(old_unsent.path).parent, Path(old_sent.path).parent):
        os.utime(folder / META_FILE, (long_ago, long_ago))
    os.utime(crashed, (long_ago, long_ago))
    assert store.sweep_unsent() == 2
    assert not Path(old_unsent.path).exists() and not crashed.exists()
    assert Path(new_unsent.path).exists() and Path(old_sent.path).exists()
    assert AttachmentStore(tmp_path / "missing").sweep_unsent() == 0


def test_excel_formulas_without_a_stored_value_show_the_formula(tmp_path: Path) -> None:
    """A workbook saved by a tool other than Excel stores formulas without results: the text copy shows them."""
    import io

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["item", "qty"])
    sheet.append(["apple", 3])
    sheet.append(["pear", 7])
    sheet["B4"] = "=SUM(B2:B3)"
    buffer = io.BytesIO()
    workbook.save(buffer)
    attachment = save(AttachmentStore(tmp_path), "totals.xlsx", buffer.getvalue())
    text = Path(attachment.text_path).read_text(encoding="utf-8")
    assert "apple\t3" in text and "\t=SUM(B2:B3)" in text


def test_sweep_skips_a_folder_it_cant_list(tmp_path: Path, monkeypatch, caplog) -> None:
    store = AttachmentStore(tmp_path)
    old = save(store, "old.txt", b"old")
    long_ago = time.time() - 2 * 24 * 3600
    os.utime(Path(old.path).parent / META_FILE, (long_ago, long_ago))
    locked = tmp_path / str(uuid.uuid4())
    locked.mkdir()
    real_iterdir = Path.iterdir

    def iterdir(self: Path):
        if self == locked:
            raise PermissionError(13, "Access is denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    assert store.sweep_unsent() == 1  # the readable session is still cleaned
    assert "Skipped" in caplog.text
    monkeypatch.setattr(Path, "iterdir", lambda self: (_ for _ in ()).throw(PermissionError(13, "denied")))
    assert store.sweep_unsent() == 0  # even the root being unlistable doesn't raise


def test_sweep_removes_only_empty_folders_of_deleted_sessions(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    kept = save(store, "kept.txt", b"kept")
    store.mark_sent(SESSION, [kept])
    empty_gone = tmp_path / str(uuid.uuid4())
    empty_known = tmp_path / str(uuid.uuid4())
    empty_gone.mkdir()
    empty_known.mkdir()
    store.sweep_unsent(known_sessions={empty_known.name})
    assert not empty_gone.exists() and empty_known.exists()
    assert Path(kept.path).exists()  # a folder with files is never removed this way, known or not

