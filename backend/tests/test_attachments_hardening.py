"""Attachments, the risky edges: hostile names and ids, auth on every route, streaming caps, broken documents and a
misbehaving text-copy worker, the per-agent delivery rules, and handoffs that carry images."""

import asyncio
import base64
import io
import json
import os
import sys
import textwrap
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import attachments as attachments_module
from app import extract
from app.adapters.base import EventType
from app.adapters.claude import user_line
from app.attachments import META_FILE, PDF_BLOCK_BYTES, Attachment, AttachmentStore, classify
from app.extract import make_text_copy

import attachment_samples as samples
from conftest import BASE_URL, opened_files


def delete_stored(path: Path) -> None:
    """Stored originals are read-only (so agents can't change them); a test that deletes one clears that first."""
    path.chmod(0o666)
    path.unlink()
from fake_ollama import FakeOllama
from test_attachments_delivery import collect, last_echo, stored, upload
from test_ollama_adapter import adapter as ollama_adapter, saved_messages
from test_orchestrator import create, wait_until_idle

SESSION = str(uuid.uuid4())


def save(store: AttachmentStore, name: str, data: bytes) -> Attachment:
    async def chunks():
        yield data

    return asyncio.run(store.save(SESSION, name, chunks()))


def folder_names(attachment: Attachment) -> list[str]:
    return sorted(p.name for p in Path(attachment.path).parent.iterdir())


def events_of(client: TestClient, session_id: str) -> list[dict]:
    return client.get(f"/api/sessions/{session_id}/events").json()


def claude_echo(client: TestClient, session_id: str) -> dict:
    """What fake Claude received on the session's last turn (its result is the turn-end event)."""
    return json.loads(events_of(client, session_id)[-1]["payload"]["text"])


# --- Auth: every new route needs the token ----------------------------------------------------------------


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


# --- Names, ids and sessions ------------------------------------------------------------------------------


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
    assert path.parent.parent.name == str(uuid.UUID(session_id)) and path.parent.name == body["id"]
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


# --- Serving and opening ----------------------------------------------------------------------------------


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


# --- Text copies: broken documents and a misbehaving worker -----------------------------------------------


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
    assert attachment.text_path is None and attachment.note, attachment
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


# --- Claude: the content-block rule -----------------------------------------------------------------------


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
    from conftest import FAKE_CLAUDE
    from test_attachments_delivery import echoed

    files = tmp_path / "attachments" / SESSION
    claude = ClaudeAdapter(tmp_path, command=[sys.executable, str(FAKE_CLAUDE), "ok"], add_dirs=[files])
    first = echoed(collect(claude.start_task("hi")))
    assert "--add-dir" not in first["args"]
    image = stored(files, "shot.png", samples.png())  # the folder now exists
    second = echoed(collect(claude.send_reply("look", attachments=[image])))
    assert second["args"][second["args"].index("--add-dir") + 1] == str(files)
    assert "--resume" in second["args"]


# --- Local model: vision, budgets and fit() ---------------------------------------------------------------


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


# --- Commands, handoffs and lifecycle through the API -----------------------------------------------------


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


# --- Regression: messages without files go out exactly as before -------------------------------------------


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

    source = (Path(__file__).parents[2] / "frontend" / "src" / "attachments.ts").read_text(encoding="utf-8")
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
