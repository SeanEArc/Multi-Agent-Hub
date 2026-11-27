"""Attachments, the risky edges: hostile names and ids, auth on every route, streaming caps, broken documents and a
misbehaving text-copy worker (and the worker capping its own memory), the per-agent delivery rules (Codex's
greedy `--image`, Claude's per-request block budget), read-only originals and the Mark of the Web, formula
fallbacks in workbooks, the startup sweep, inline fences, the extraction gate, and handoffs that carry images.
"""

import asyncio
import base64
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
from app.adapters.base import EventType
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
    classify,
    with_files,
)
from app.extract import make_text_copy

from support import attachment_samples as samples
from support.attachments import codex_echo, collect, last_echo, stored, upload
from support.fake_ollama import FakeOllama
from support.hub import BASE_URL, opened_files
from support.ollama import adapter as ollama_adapter, saved_messages
from support.paths import BACKEND, FAKE_CODEX, ROOT
from support.sessions import create, wait_until_idle


def delete_stored(path: Path) -> None:
    """Stored originals are read-only (so agents can't change them); a test that deletes one clears that first."""
    path.chmod(0o666)
    path.unlink()


SESSION = str(uuid.uuid4())


def save(store: AttachmentStore, name: str, data: bytes, session: str = SESSION) -> Attachment:
    async def chunks():
        yield data

    return asyncio.run(store.save(session, name, chunks()))


def folder_names(attachment: Attachment) -> list[str]:
    return sorted(p.name for p in Path(attachment.path).parent.iterdir())


def events_of(client: TestClient, session_id: str) -> list[dict]:
    return client.get(f"/api/sessions/{session_id}/events").json()


def claude_echo(client: TestClient, session_id: str) -> dict:
    """What fake Claude received on the session's last turn (its result is the turn-end event)."""
    return json.loads(events_of(client, session_id)[-1]["payload"]["text"])


def test_every_attachment_route_needs_the_token(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    note = upload(client, session_id, "notes.txt", b"hello")
    base = f"/api/sessions/{session_id}/attachments"
    routes = [
        ("POST", base, {"params": {"name": "a.txt"}, "content": b"x"}),
        ("GET", f"{base}/{note['id']}", {}),
        ("DELETE", f"{base}/{note['id']}", {}),
        ("GET", f"{base}/{note['id']}/file", {}),
        ("POST", f"{base}/{note['id']}/open", {}),
    ]
    for headers in ({}, {"Authorization": "Bearer wrong-token"}):
        anonymous = TestClient(client.app, base_url=BASE_URL, headers=headers)
        opened_files.clear()
        for method, url, extra in routes:
            response = anonymous.request(method, url, **extra)
            assert response.status_code in (401, 403), (method, url, response.status_code)
        assert opened_files == []
    # Nothing was uploaded, deleted or opened without the token.
    assert client.get(f"{base}/{note['id']}").status_code == 200
    assert [p.name for p in Path(note["path"]).parent.parent.iterdir()] == [note["id"]]


def test_bad_session_ids_and_attachment_ids_are_404(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    other_id = create(client, tmp_path, "ok")
    mine = upload(client, session_id, "notes.txt", b"mine")
    base = f"/api/sessions/{session_id}/attachments"
    # ("/attachments/.." itself is normalized by the client to the session's own URL, so it isn't tried here.)
    for bad in ("ABCDEF012345", "0123456789a", "0123456789abc", "..%5C..%5Cdata", "%2e%2e"):
        assert client.get(f"{base}/{bad}").status_code == 404, bad
        assert client.get(f"{base}/{bad}/file").status_code == 404, bad
    # An attachment id from another session isn't found there, and can't be sent from there.
    other = f"/api/sessions/{other_id}/attachments/{mine['id']}"
    assert client.get(other).status_code == 404
    assert client.get(f"{other}/file").status_code == 404
    assert client.post(f"{other}/open").status_code == 404
    assert client.delete(other).status_code == 404
    assert Path(mine["path"]).exists()
    sent = client.post(f"/api/sessions/{other_id}/messages", json={"text": "hi", "attachments": [mine["id"]]})
    assert sent.status_code == 400
    # A session id that isn't a UUID never names a folder (and isn't a server error).
    for route in ("", "/file"):
        assert client.get(f"/api/sessions/not-a-uuid/attachments/{mine['id']}{route}").status_code == 404
    assert client.delete(f"/api/sessions/not-a-uuid/attachments/{mine['id']}").status_code == 404
    assert client.post(f"/api/sessions/not-a-uuid/attachments/{mine['id']}/open").status_code == 404
    assert client.post("/api/sessions/not-a-uuid/attachments", params={"name": "a.txt"},
                       content=b"x").status_code == 404


@pytest.mark.parametrize("name, stored_as", [
    ("..\\..\\..\\evil.txt", "evil.txt"),
    ("../../etc/passwd.txt", "passwd.txt"),
    ("C:\\Windows\\win.ini.txt", "win.ini.txt"),
    ("nul.txt", "_nul.txt"),
    ("COM1.txt", "_COM1.txt"),
    ("lpt9.md", "_lpt9.md"),
    ("résumé 報告 ✓.txt", "résumé 報告 ✓.txt"),
    ("a,b.png", "a_b.png"),
    ("trailing dots....txt", "trailing dots....txt"),
])
def test_hostile_and_unusual_names_stay_inside_the_session_folder(client: TestClient, tmp_path: Path, name: str,
                                                                  stored_as: str) -> None:
    session_id = create(client, tmp_path, "ok")
    data = samples.png() if name.endswith(".png") else "contenu ✓\n".encode()
    body = upload(client, session_id, name, data)
    path = Path(body["path"])
    assert path.name == stored_as
    # The session's folder is named by its id as stored (revision 4 PR 2: `<attachments>/<session id>/outputs`).
    assert path.parent.parent.name == uuid.UUID(session_id).hex and path.parent.name == body["id"]
    assert path.read_bytes() == data
    shown = client.get(f"/api/sessions/{session_id}/attachments/{body['id']}/file")
    assert shown.status_code == 200 and shown.content == data


def test_names_that_cant_be_stored_are_refused(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    url = f"/api/sessions/{session_id}/attachments"
    assert client.post(url, content=b"x").status_code == 422  # no name at all
    assert client.post(url, params={"name": ""}, content=b"x").status_code == 400
    assert client.post(url, params={"name": "..\\"}, content=b"x").status_code in (400, 415)
    for refused in ("..", "run.exe", "lib.dll", "page.svg", "book.pptx", "x.zip", "noextension", ".env"):
        assert client.post(url, params={"name": refused}, content=b"x").status_code == 415, refused
    # Refused uploads leave no folders behind: only the one accepted file is there.
    accepted = upload(client, session_id, "ok.txt", b"k")
    assert [p.name for p in Path(accepted["path"]).parents[1].iterdir()] == [accepted["id"]]


def test_contents_must_match_the_type(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    url = f"/api/sessions/{session_id}/attachments"
    png = samples.png()
    for name, data in [("shot.png", samples.pdf()), ("shot.jpg", png), ("anim.gif", png), ("pic.webp", png),
                       ("report.pdf", png), ("letter.docx", samples.pdf()), ("sheet.xlsx", b"MZ\x90\x00program"),
                       ("old.xls", samples.xlsx()), ("main.py", b"MZ\x90\x00\x03\x00\x00\x00"),
                       ("data.csv", b"a,b\n\x00\x00")]:
        response = client.post(url, params={"name": name}, content=data)
        assert response.status_code == 415, (name, response.text)
    # Real headers of each image type, and UTF-16 text with its BOM, are fine.
    for name, data in [("a.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 20), ("a.gif", b"GIF89a" + b"\x00" * 20),
                       ("a.webp", b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8),
                       ("utf16.txt", "hello".encode("utf-16"))]:
        assert client.post(url, params={"name": name}, content=data).status_code == 201, name
    # A Word file renamed .xlsx passes the zip check but simply gets no text copy.
    renamed = upload(client, session_id, "really-a-letter.xlsx", samples.docx())
    assert renamed["text_path"] is None and renamed["note"]


def test_a_streamed_upload_stops_at_the_cap(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(attachments_module.SIZE_CAPS, "text", 10_000)
    session_id = create(client, tmp_path, "ok")
    sent = []

    def body():
        for _ in range(100):  # 100 KB, no Content-Length: chunked
            sent.append(1)
            yield b"y" * 1000

    response = client.post(f"/api/sessions/{session_id}/attachments", params={"name": "big.txt"}, content=body())
    assert response.status_code == 413 and "10000" not in response.text
    folder = Path(client.app.state.orchestrator.attachments.session_dir(session_id))
    assert not folder.exists() or list(folder.iterdir()) == []
    # Exactly at the cap is fine.
    assert upload(client, session_id, "edge.txt", b"z" * 10_000)["size"] == 10_000


def test_messages_take_at_most_twenty_files(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    ids = [upload(client, session_id, f"n{i}.txt", b"x")["id"] for i in range(21)]
    messages = f"/api/sessions/{session_id}/messages"
    assert client.post(messages, json={"text": "hi", "attachments": ids}).status_code == 422
    assert client.post(messages, json={"text": "hi", "attachments": ids[:20]}).status_code == 202
    wait_until_idle(client, session_id)
    assert len(events_of(client, session_id)[0]["payload"]["attachments"]) == 20


def test_delete_twice_and_after_sending(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    base = f"/api/sessions/{session_id}/attachments"
    draft = upload(client, session_id, "draft.txt", b"x")
    assert client.delete(f"{base}/{draft['id']}").status_code == 204
    assert client.delete(f"{base}/{draft['id']}").status_code == 404
    used = upload(client, session_id, "used.txt", b"x")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi", "attachments": [used["id"]]})
    wait_until_idle(client, session_id)
    assert client.delete(f"{base}/{used['id']}").status_code == 409
    assert Path(used["path"]).exists()


def test_files_are_served_so_nothing_runs_as_a_page(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    base = f"/api/sessions/{session_id}/attachments"

    def fetch(body: dict, **params):
        response = client.get(f"{base}/{body['id']}/file", params=params)
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        return response

    page = upload(client, session_id, "page.html", b"<script>alert(1)</script>")
    svgish = upload(client, session_id, "notes.xml", b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>")
    for body in (page, svgish):
        assert fetch(body).headers["content-type"] == "text/plain; charset=utf-8"
    report = fetch(upload(client, session_id, "report.pdf", samples.pdf()))
    assert report.headers["content-type"] == "application/pdf"
    assert report.headers["content-disposition"].startswith("inline")
    letter = fetch(upload(client, session_id, "letter.docx", samples.docx()))
    assert letter.headers["content-type"] == "application/octet-stream"
    assert letter.headers["content-disposition"].startswith("attachment")
    table = upload(client, session_id, "table.csv", b"a,b\n1,2\n")
    assert fetch(table).headers["content-type"].startswith("text/plain")
    assert fetch(table, text="true").text.strip() == "a\tb\n1\t2"
    sheet = upload(client, session_id, "fruit.xlsx", samples.xlsx())
    copy = fetch(sheet, text="true")
    assert copy.headers["content-type"].startswith("text/plain") and "pear\t7" in copy.text
    image = upload(client, session_id, "shot.png", samples.png())
    assert client.get(f"{base}/{image['id']}/file", params={"text": "true"}).status_code == 404  # no text copy
    unicode_name = fetch(upload(client, session_id, "報告.pdf", samples.pdf()))
    assert "filename*=utf-8''" in unicode_name.headers["content-disposition"]


def test_open_never_starts_code_and_needs_the_file(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    base = f"/api/sessions/{session_id}/attachments"
    expected = {"page.html": "revealed", "run.bat": "revealed", "tool.cmd": "revealed", "x.ps1": "revealed",
                "app.js": "revealed", "notes.md": "opened", "table.csv": "opened"}
    opened_files.clear()
    bodies = {}
    for name, action in expected.items():
        bodies[name] = upload(client, session_id, name, b"echo hi\n")
        assert client.post(f"{base}/{bodies[name]['id']}/open").json() == {"action": action}, name
    assert [reveal for _, reveal in opened_files] == [action == "revealed" for action in expected.values()]
    # A file deleted from disk: 404, and nothing is handed to Windows.
    delete_stored(Path(bodies["notes.md"]["path"]))  # stored files are read-only
    opened_files.clear()
    assert client.post(f"{base}/{bodies['notes.md']['id']}/open").status_code == 404
    assert opened_files == []


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _encrypted_pdf(user_password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(samples.pdf())))
    writer.encrypt(user_password=user_password, owner_password="owner", algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize("name, data", [
    ("truncated.pdf", samples.pdf()[:120]),
    ("garbage.docx", b"PK\x03\x04" + os.urandom(2000)),
    ("truncated.docx", samples.docx()[:400]),
    ("not-word.docx", _zip({"hello.txt": b"just a zip"})),
    ("garbage.xlsx", b"PK\x03\x04" + b"\xff" * 2000),
    ("truncated.xlsx", samples.xlsx()[:400]),
    ("garbage.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600),
])
def test_broken_documents_upload_without_a_text_copy(tmp_path: Path, name: str, data: bytes) -> None:
    attachment = save(AttachmentStore(tmp_path), name, data)
    assert attachment.text_path is None and attachment.pages is None and attachment.note, attachment
    assert folder_names(attachment) == sorted([META_FILE, name])  # no .txt or .part left
    assert Path(attachment.path).read_bytes() == data


def test_a_password_protected_pdf_says_so(tmp_path: Path) -> None:
    locked = save(AttachmentStore(tmp_path), "locked.pdf", _encrypted_pdf("secret"))
    assert locked.text_path is None and "password" in locked.note
    assert folder_names(locked) == sorted([META_FILE, "locked.pdf"])
    # Encrypted with only an owner password, it opens without one, so it gets a text copy.
    owner_only = save(AttachmentStore(tmp_path), "owner.pdf", _encrypted_pdf(""))
    assert samples.PDF_WORD in Path(owner_only.text_path).read_text(encoding="utf-8")
    assert owner_only.pages == 1


def test_csv_text_copy_handles_encodings_and_huge_cells(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path)
    latin = save(store, "latin.csv", "name;city\nJosé;Zürich\n".encode("cp1252"))
    assert "José\tZürich" in Path(latin.text_path).read_text(encoding="utf-8")
    wide = save(store, "wide.csv", b"a,b\n" + b"x" * 50_000 + b",tail\n")
    text = Path(wide.text_path).read_text(encoding="utf-8")
    assert "\ttail" in text and len(text) < 3000  # one cell is cut, the row goes on


def test_a_huge_csv_is_cut_at_the_character_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(extract, "MAX_TEXT_CHARS", 5_000)
    # make_text_copy's default was bound at import; the store passes none, so set the default too.
    monkeypatch.setattr(extract.make_text_copy, "__kwdefaults__",
                        {**extract.make_text_copy.__kwdefaults__, "max_chars": 5_000})
    rows = "".join(f"{i},value {i},{i * 3}\n" for i in range(200_000)).encode()  # about 4 MB
    attachment = save(AttachmentStore(tmp_path), "big.csv", rows)
    text = Path(attachment.text_path).read_text(encoding="utf-8")
    assert "cut at 5,000 characters" in text and len(text) < 5_200
    assert folder_names(attachment) == sorted([META_FILE, "big.csv", "big.csv.txt"])


def _fake_worker(tmp_path: Path, monkeypatch, body: str) -> None:
    """Replace the worker with a script; it gets the same request on stdin."""
    script = tmp_path / "worker_script.py"
    script.write_text("import json, sys, time\nrequest = json.loads(sys.stdin.buffer.read())\n"
                      + textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setattr(extract, "worker_command", lambda: [sys.executable, "-I", str(script)])


def _original(tmp_path: Path) -> Path:
    folder = tmp_path / "files"
    folder.mkdir()
    original = folder / "report.pdf"
    original.write_bytes(samples.pdf())
    return original


def test_a_worker_killed_at_the_timeout_leaves_no_partial_copy(tmp_path: Path, monkeypatch) -> None:
    # The worker has its output file open when it is killed.
    _fake_worker(tmp_path, monkeypatch, """
        out = open(request["dst"], "w")
        out.write("partial text")
        out.flush()
        print("started", file=sys.stderr, flush=True)
        time.sleep(60)
    """)
    original = _original(tmp_path)
    copy = asyncio.run(make_text_copy("pdf", original, timeout=3))
    assert copy.path is None and "longer than 3 seconds" in copy.error
    assert sorted(p.name for p in original.parent.iterdir()) == ["report.pdf"]


def test_a_worker_that_crashes_mid_write_leaves_no_partial_copy(tmp_path: Path, monkeypatch) -> None:
    _fake_worker(tmp_path, monkeypatch, """
        open(request["dst"], "w").write("half a document")
        sys.exit(3)
    """)
    original = _original(tmp_path)
    copy = asyncio.run(make_text_copy("pdf", original))
    assert copy.path is None and "exit code 3" in copy.error
    assert sorted(p.name for p in original.parent.iterdir()) == ["report.pdf"]


@pytest.mark.parametrize("body, error", [
    ('print(json.dumps({"ok": True, "pages": 2}))', "wrote nothing"),
    ('open(request["dst"], "w").write("x")\nprint(json.dumps({"ok": False, "error": "bad xref"}))', "bad xref"),
    ('print("not json at all")', "stopped unexpectedly"),
    ('raise MemoryError()', "more memory"),
])
def test_worker_failures_become_notes(tmp_path: Path, monkeypatch, body: str, error: str) -> None:
    _fake_worker(tmp_path, monkeypatch, body)
    original = _original(tmp_path)
    copy = asyncio.run(make_text_copy("pdf", original))
    assert copy.path is None and error in copy.error
    assert sorted(p.name for p in original.parent.iterdir()) == ["report.pdf"]


def test_a_worker_reporting_success_gets_its_copy_renamed(tmp_path: Path, monkeypatch) -> None:
    _fake_worker(tmp_path, monkeypatch, """
        open(request["dst"], "w", encoding="utf-8").write("the text")
        print(json.dumps({"ok": True, "pages": "seven"}))
    """)
    original = _original(tmp_path)
    copy = asyncio.run(make_text_copy("pdf", original))
    assert copy.path.read_text(encoding="utf-8") == "the text"
    assert copy.pages is None  # a page count that isn't a number is ignored
    assert sorted(p.name for p in original.parent.iterdir()) == ["report.pdf", "report.pdf.txt"]


@pytest.mark.skipif(os.name != "nt", reason="the job object is Windows-only")
def test_no_text_copy_when_the_memory_limit_cant_be_set(tmp_path: Path, monkeypatch) -> None:
    class BrokenJob:
        def __init__(self, memory_limit: int, **options) -> None:
            pass

        def assign(self, pid: int) -> None:
            raise OSError("access denied")

        def close(self) -> None:
            pass

    ran = tmp_path / "parsed"
    _fake_worker(tmp_path, monkeypatch, f"""
        open({str(ran)!r}, "w").write("parsed")
        open(request["dst"], "w").write("text")
        print(json.dumps({{"ok": True}}))
    """)
    monkeypatch.setattr(extract, "WindowsJob", BrokenJob)
    original = _original(tmp_path)
    copy = asyncio.run(make_text_copy("pdf", original))
    assert copy.path is None and "memory limit" in copy.error
    assert not ran.exists()  # the request was never sent, so nothing was parsed outside the cap
    assert sorted(p.name for p in original.parent.iterdir()) == ["report.pdf"]


def test_an_unexpected_error_never_fails_the_upload(tmp_path: Path, monkeypatch) -> None:
    async def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(extract, "_run_worker", explode)
    attachment = save(AttachmentStore(tmp_path), "report.pdf", samples.pdf())
    assert attachment.text_path is None and "RuntimeError" in attachment.note


def test_a_failed_text_copy_never_fails_the_message(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    broken = upload(client, session_id, "broken.pdf", b"%PDF-1.7\n" + b"\x00garbage" * 50)
    assert broken["text_path"] is None and broken["note"]
    response = client.post(f"/api/sessions/{session_id}/messages",
                           json={"text": "Read this", "attachments": [broken["id"]]})
    assert response.status_code == 202
    assert wait_until_idle(client, session_id)["status"] == "completed"
    sent = claude_echo(client, session_id)
    assert "(no text copy could be made" in sent["prompt"] and broken["path"] in sent["prompt"]
    assert [b["type"] for b in sent["blocks"]] == ["text"]  # page count unknown: no document block


def _line(prompt: str, attachments: list[Attachment]) -> dict:
    return json.loads(user_line(prompt, attachments))


def test_claude_pdf_blocks_only_within_the_api_limits(tmp_path: Path) -> None:
    pdf = samples.pdf()
    ok = stored(tmp_path, "short.pdf", pdf, pages=100)
    unknown = stored(tmp_path, "unknown.pdf", pdf, pages=None, text_path="C:/t/unknown.pdf.txt")
    long = stored(tmp_path, "long.pdf", pdf, pages=101, text_path="C:/t/long.pdf.txt")
    heavy = Attachment(**{**stored(tmp_path, "heavy.pdf", pdf, pages=3).to_json(), "size": PDF_BLOCK_BYTES + 1})
    sheet = stored(tmp_path, "data.xlsx", samples.xlsx(), text_path="C:/t/data.xlsx.txt")
    line = _line("Compare", [ok, unknown, long, heavy, sheet])
    assert line["type"] == "user" and line["message"]["role"] == "user"
    content = line["message"]["content"]
    assert [b["type"] for b in content] == ["text", "document"]
    assert base64.b64decode(content[1]["source"]["data"]) == pdf
    text = content[0]["text"]
    assert f"`{ok.path}` (also attached to this message)" in text
    for gone_by_path in (unknown, long, heavy):
        assert f"`{gone_by_path.path}`" in text
        assert f"`{gone_by_path.path}` (also attached" not in text
    assert "text copy: `C:/t/long.pdf.txt`" in text and "text copy: `C:/t/data.xlsx.txt`" in text


def test_claude_line_keeps_a_command_first_and_is_one_line(tmp_path: Path) -> None:
    image = stored(tmp_path, "shot.png", samples.png())
    raw = user_line("/review the login page\nplease", [image])
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1  # a newline in the prompt is escaped, not a new line
    content = json.loads(raw)["message"]["content"]
    assert content[0]["text"].startswith("/review the login page\nplease")
    assert content[1]["source"] == {"type": "base64", "media_type": "image/png",
                                    "data": base64.b64encode(samples.png()).decode()}
    # Unicode survives (the line is UTF-8 JSON).
    assert json.loads(user_line("café ✓"))["message"]["content"] == [{"type": "text", "text": "café ✓"}]


def test_claude_add_dir_once_the_session_has_files(client: TestClient, tmp_path: Path) -> None:
    from app.adapters.claude import ClaudeAdapter
    from support.paths import FAKE_CLAUDE
    from support.attachments import echoed

    files = tmp_path / "attachments" / SESSION
    claude = ClaudeAdapter(tmp_path, command=[sys.executable, str(FAKE_CLAUDE), "ok"], add_dirs=[files])
    first = echoed(collect(claude.start_task("hi")))
    assert "--add-dir" not in first["args"]
    image = stored(files, "shot.png", samples.png())  # the folder now exists
    second = echoed(collect(claude.send_reply("look", attachments=[image])))
    assert second["args"][second["args"].index("--add-dir") + 1] == str(files)
    assert "--resume" in second["args"]


def test_local_inline_text_has_a_total_budget(tmp_path: Path) -> None:
    files = tmp_path / "files"
    attachments = []
    for index in range(3):
        copy = tmp_path / f"copy{index}.txt"
        copy.write_text(f"FILE{index} " + "w" * 40_000, encoding="utf-8")
        attachments.append(stored(files, f"doc{index}.docx", samples.docx(), text_path=str(copy)))
    fake = FakeOllama()
    collect(ollama_adapter(tmp_path, fake, num_ctx=1_000_000).start_task("Summarize", attachments=attachments))
    content = fake.calls[0]["messages"][-1]["content"]
    assert "FILE0" in content and "FILE1" in content and "FILE2" not in content  # 30k + 30k, then none left
    assert content.count("more characters; read the file for the rest") == 2
    for attachment in attachments:  # every file is still listed by path
        assert attachment.path in content


def test_fit_shortens_inline_text_but_keeps_this_turns_images(tmp_path: Path) -> None:
    files = tmp_path / "files"
    image = stored(files, "shot.png", samples.png())
    copy = tmp_path / "copy.txt"
    copy.write_text("START " + "lorem ipsum " * 5_000 + " END", encoding="utf-8")
    doc = stored(files, "doc.docx", samples.docx(), text_path=str(copy))
    fake = FakeOllama()
    local = ollama_adapter(tmp_path, fake, vision=True, num_ctx=8192)
    events = collect(local.start_task("What is this?", attachments=[image, doc]))
    assert any(e.type is EventType.NOTICE and e.data.get("reason") == "context" for e in events)
    user = fake.calls[0]["messages"][-1]
    assert user["images"] == [base64.b64encode(samples.png()).decode()] and "hub_images" not in user
    saved = saved_messages(tmp_path, local.native_session_id)[0]
    assert len(user["content"]) < len(saved["content"])  # only what was sent was shortened
    assert saved["content"].startswith("What is this?") and "lorem ipsum " * 2000 in saved["content"]


def test_local_vision_sends_only_the_newest_images(tmp_path: Path) -> None:
    files = tmp_path / "files"
    red = stored(files, "red.png", samples.png((255, 0, 0)))
    blue = stored(files, "blue.png", samples.png((0, 0, 255)))
    fake = FakeOllama()
    local = ollama_adapter(tmp_path, fake, vision=True)
    collect(local.start_task("first", attachments=[red]))
    collect(local.send_reply("second", attachments=[blue]))
    messages = fake.calls[-1]["messages"]
    with_images = [m for m in messages if "images" in m]
    assert len(with_images) == 1 and with_images[0] is messages[-1]
    assert with_images[0]["images"] == [base64.b64encode(samples.png((0, 0, 255))).decode()]
    assert not any("hub_images" in m for m in messages)
    assert [m.get("hub_images") for m in saved_messages(tmp_path, local.native_session_id)
            if m["role"] == "user"] == [[red.path], [blue.path]]


def test_local_vision_with_a_deleted_image_still_runs(tmp_path: Path) -> None:
    image = stored(tmp_path / "files", "shot.png", samples.png())
    Path(image.path).unlink()
    fake = FakeOllama()
    events = collect(ollama_adapter(tmp_path, fake, vision=True).start_task("Look", attachments=[image]))
    assert events[-1].type is EventType.COMPLETED
    assert "images" not in fake.calls[0]["messages"][-1]
    assert image.path in fake.calls[0]["messages"][-1]["content"]


def test_local_no_vision_notice_only_when_there_are_images(tmp_path: Path) -> None:
    note = stored(tmp_path / "files", "notes.txt", b"plain text")
    events = collect(ollama_adapter(tmp_path, FakeOllama()).start_task("Read", attachments=[note]))
    assert not any(e.type is EventType.NOTICE for e in events)


def test_commands_that_have_nowhere_to_put_files_refuse_them(client: TestClient, tmp_path: Path) -> None:
    claude = create(client, tmp_path, "ok")
    note = upload(client, claude, "notes.txt", b"x")
    messages = f"/api/sessions/{claude}/messages"
    # Before a conversation /context runs as a one-off; with one it's still answered without the model.
    for command in ("/context", "/usage", "/compact", "/model", "/clear"):
        response = client.post(messages, json={"text": command, "attachments": [note["id"]]})
        assert response.status_code == 400, (command, response.text)
    assert events_of(client, claude) == []  # nothing was saved for a refused message
    # The file wasn't used, so it can still be discarded.
    assert client.delete(f"/api/sessions/{claude}/attachments/{note['id']}").status_code == 204

    codex = create(client, tmp_path, "ok", agent="codex")
    note = upload(client, codex, "notes.txt", b"x")
    for command in ("/usage", "/compact", "/diff", "/status", "/model"):
        response = client.post(f"/api/sessions/{codex}/messages", json={"text": command, "attachments": [note["id"]]})
        assert response.status_code == 400, (command, response.text)


def test_claude_pass_through_commands_take_files(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    image = upload(client, session_id, "shot.png", samples.png())
    response = client.post(f"/api/sessions/{session_id}/messages",
                           json={"text": "/code-review the UI", "attachments": [image["id"]]})
    assert response.status_code == 202, response.text
    wait_until_idle(client, session_id)
    sent = claude_echo(client, session_id)
    assert sent["prompt"].startswith("/code-review the UI")  # the command still starts the text
    assert [b["type"] for b in sent["blocks"]] == ["text", "image"]


def test_a_manual_handoff_sends_recent_images_with_the_brief(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    image = upload(client, session_id, "shot.png", samples.png())
    sheet = upload(client, session_id, "fruit.xlsx", samples.xlsx())
    client.post(f"/api/sessions/{session_id}/messages",
                json={"text": "Look", "attachments": [image["id"], sheet["id"]]})
    wait_until_idle(client, session_id)
    response = client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "codex", "note": "go on"})
    assert response.status_code == 202, response.text
    body = wait_until_idle(client, session_id)
    assert body["agent"] == "codex" and body["status"] == "completed"
    sent = last_echo(client, session_id)
    assert sent["args"][sent["args"].index("--image") + 1] == image["path"]
    assert "## Files the user attached" in sent["prompt"] and sheet["text_path"] in sent["prompt"]


def test_a_clean_handoff_sends_no_images(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    image = upload(client, session_id, "shot.png", samples.png())
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "Look", "attachments": [image["id"]]})
    wait_until_idle(client, session_id)
    response = client.post(f"/api/sessions/{session_id}/handoff",
                           json={"agent": "codex", "brief": False, "note": "start fresh"})
    assert response.status_code == 202, response.text
    wait_until_idle(client, session_id)
    sent = last_echo(client, session_id)
    assert "--image" not in sent["args"] and "Files the user attached" not in sent["prompt"]


def test_a_handoff_skips_images_deleted_from_disk(client: TestClient, tmp_path: Path) -> None:
    # Fake Codex, like the real one, fails a run whose --image can't be read.
    session_id = create(client, tmp_path, "claude:limit codex:ok")
    kept = upload(client, session_id, "kept.png", samples.png((0, 255, 0)))
    gone = upload(client, session_id, "gone.png", samples.png())
    delete_stored(Path(gone["path"]))
    client.post(f"/api/sessions/{session_id}/messages",
                json={"text": "Look", "attachments": [kept["id"], gone["id"]]})
    body = wait_until_idle(client, session_id)
    assert body["agent"] == "codex" and body["status"] == "completed", body
    args = last_echo(client, session_id)["args"]
    assert args.count("--image") == 1 and args[args.index("--image") + 1] == kept["path"]


def test_a_waiting_handoff_with_a_new_image_sends_both_once(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    old = upload(client, session_id, "old.png", samples.png())
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "Look", "attachments": [old["id"]]})
    wait_until_idle(client, session_id)
    client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "codex", "wait": True})
    new = upload(client, session_id, "new.png", samples.png((0, 0, 255)))
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "And this", "attachments": [new["id"]]})
    wait_until_idle(client, session_id)
    args = last_echo(client, session_id)["args"]
    images = [args[i + 1] for i, arg in enumerate(args) if arg == "--image"]
    assert images == [old["path"], new["path"]]


def test_deleting_a_session_removes_its_files_but_not_others(client: TestClient, tmp_path: Path) -> None:
    doomed = create(client, tmp_path, "ok")
    kept = create(client, tmp_path, "ok")
    unsent = upload(client, doomed, "draft.txt", b"x")
    report = upload(client, doomed, "report.pdf", samples.pdf())
    client.post(f"/api/sessions/{doomed}/messages", json={"text": "hi", "attachments": [report["id"]]})
    wait_until_idle(client, doomed)
    other = upload(client, kept, "mine.txt", b"y")
    assert client.delete(f"/api/sessions/{doomed}").status_code == 204
    assert not Path(report["path"]).parent.parent.exists() and not Path(unsent["path"]).exists()
    assert Path(other["path"]).exists()
    assert client.get(f"/api/sessions/{doomed}/attachments/{report['id']}").status_code == 404


def test_a_plain_message_has_no_attachment_section(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "plain hello"})
    wait_until_idle(client, session_id)
    events = events_of(client, session_id)
    assert events[0]["payload"] == {"text": "plain hello"}  # no empty attachments key
    sent = claude_echo(client, session_id)
    assert sent["prompt"] == "plain hello" and sent["blocks"] == [{"type": "text", "media_type": None,
                                                                   "bytes": None}]
    codex = create(client, tmp_path, "ok", agent="codex")
    client.post(f"/api/sessions/{codex}/messages", json={"text": "plain hello"})
    wait_until_idle(client, codex)
    sent = last_echo(client, codex)
    assert sent["prompt"] == "plain hello" and "--image" not in sent["args"]


def test_classify_is_case_insensitive_for_every_allowed_document_type() -> None:
    for suffix, (kind, mime) in attachments_module.DOCUMENT_TYPES.items():
        assert classify("FILE" + suffix.upper()) == (kind, mime)


def test_the_composer_allows_exactly_what_the_backend_allows() -> None:
    """frontend/src/attachments.ts mirrors the allowed types and caps (it checks files before uploading); the repo
    has no frontend test runner, so the two lists are compared here."""
    import re

    source = (ROOT / "frontend" / "src" / "attachments.ts").read_text(encoding="utf-8")
    caps_block = re.search(r"SIZE_CAPS[^{]*\{(.*?)\}", source, re.S).group(1)
    caps = {kind: int(mb) * attachments_module.MB for kind, mb in re.findall(r"(\w+): (\d+) \* MB", caps_block)}
    assert caps == attachments_module.SIZE_CAPS
    documents_block = re.search(r"const DOCUMENTS[^{]*\{(.*?)\}", source, re.S).group(1)
    documents = dict(re.findall(r'(\w+): "(\w+)"', documents_block))
    assert documents == {suffix[1:]: kind for suffix, (kind, _) in attachments_module.DOCUMENT_TYPES.items()}
    text_block = re.search(r"const TEXT = new Set\((.*?)\.split", source, re.S).group(1)
    text = set("".join(re.findall(r'"([^"]*)"', text_block)).split())
    assert text == {suffix[1:] for suffix in attachments_module.TEXT_EXTENSIONS}
    names = set(re.findall(r'"([^"]+)"', re.search(r"const TEXT_NAMES = new Set\(\[(.*?)\]", source).group(1)))
    assert names == attachments_module.TEXT_NAMES


windows_only = pytest.mark.skipif(os.name != "nt", reason="job objects and NTFS streams are Windows-only")


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
    from support.hub import TEST_TOKEN, fake_adapter_factory, make_client

    store = AttachmentStore(tmp_path / "attachments")
    attachment = save(store, "odd.txt", b"x")
    (Path(attachment.path).parent / META_FILE).write_text("[]", encoding="utf-8")
    _age(attachment, 3 * 24 * 3600)
    app = create_app(engine, fake_adapter_factory, token=TEST_TOKEN, attachments_dir=tmp_path / "attachments",
                     chats_dir=tmp_path / "chats", skills_dir=tmp_path / "skills", codex_home=tmp_path / "codex",
                     claude_home=tmp_path / "claude", discover_native=False)
    with make_client(app) as client:
        assert client.get("/api/health").status_code == 200


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
