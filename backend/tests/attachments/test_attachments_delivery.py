"""Attachments reaching each agent its own way, and the API around them (upload, show, open, send, handoff)."""

import base64
import json
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.base import EventType
from app.adapters.claude import ClaudeAdapter
from app.adapters.codex import CodexAdapter

from support import attachment_samples as samples
from support.attachments import codex_echo, collect, echoed, last_echo, stored, upload
from support.fake_ollama import FakeOllama
from support.hub import opened_files
from support.ollama import adapter as ollama_adapter, saved_messages
from support.paths import FAKE_CLAUDE, FAKE_CODEX
from support.sessions import create, wait_until_idle


# --- Claude: content blocks over stream-json, --add-dir ------------------------------------------------


def test_claude_gets_images_and_pdfs_as_blocks_and_the_rest_by_path(tmp_path: Path) -> None:
    files = tmp_path / "attachments"
    image = stored(files, "shot.png", samples.png())
    report = stored(files, "report.pdf", samples.pdf(), pages=1)
    sheet = stored(files, "data.xlsx", samples.xlsx(), text_path=str(files / "data.xlsx.txt"))
    work = tmp_path / "work"
    work.mkdir()
    claude = ClaudeAdapter(work, command=[sys.executable, str(FAKE_CLAUDE), "ok"], add_dirs=[files])

    sent = echoed(collect(claude.start_task("What do these show?", attachments=[image, report, sheet])))
    assert sent["blocks"] == [
        {"type": "text", "media_type": None, "bytes": None},
        {"type": "image", "media_type": "image/png", "bytes": image.size},
        {"type": "document", "media_type": "application/pdf", "bytes": report.size},
    ]
    assert sent["prompt"].startswith("What do these show?\n\n## Attached files")
    assert f"`{sheet.path}`; text copy: `{sheet.text_path}`" in sent["prompt"]
    assert f"`{image.path}` (also attached to this message)" in sent["prompt"]
    args = sent["args"]
    assert args[args.index("--input-format") + 1] == "stream-json"
    assert args[args.index("--add-dir") + 1] == str(files)
    assert sent["cwd"] == str(work)  # --add-dir doesn't move the working folder

    # A resumed turn takes blocks the same way.
    again = echoed(collect(claude.send_reply("And this one?", attachments=[image])))
    assert [b["type"] for b in again["blocks"]] == ["text", "image"]
    assert "--resume" in again["args"]


def test_claude_every_turn_is_stream_json(tmp_path: Path) -> None:
    claude = ClaudeAdapter(tmp_path, command=[sys.executable, str(FAKE_CLAUDE), "ok"],
                           add_dirs=[tmp_path / "not-made-yet"])
    for events in (claude.start_task("hi"), claude.send_reply("/context"), claude.run_command("/usage"),
                   claude.summarize("Summarize", "Be brief")):
        sent = echoed(collect(events))
        assert sent["blocks"] == [{"type": "text", "media_type": None, "bytes": None}]
        assert "--add-dir" not in sent["args"]  # the folder doesn't exist until something is attached
    assert sent["prompt"] == "Summarize"


def test_claude_skips_a_block_for_a_missing_file(tmp_path: Path) -> None:
    image = stored(tmp_path, "shot.png", samples.png())
    Path(image.path).unlink()
    claude = ClaudeAdapter(tmp_path, command=[sys.executable, str(FAKE_CLAUDE), "ok"])
    sent = echoed(collect(claude.start_task("hi", attachments=[image])))
    assert [b["type"] for b in sent["blocks"]] == ["text"]
    assert image.path in sent["prompt"]


# --- Codex: --image ------------------------------------------------------------------------------------


def test_codex_gets_images_with_image_flags_on_new_and_resumed_turns(tmp_path: Path) -> None:
    image = stored(tmp_path / "a", "shot.png", samples.png())
    doc = stored(tmp_path / "a", "letter.docx", samples.docx(), text_path="C:/x/letter.docx.txt")
    codex = CodexAdapter(tmp_path, command=[sys.executable, str(FAKE_CODEX), "ok"], codex_home=tmp_path / "home")

    second = stored(tmp_path / "b", "second.png", samples.png((0, 0, 255)))
    first = codex_echo(collect(codex.start_task("Look", attachments=[image, doc, second])))
    # The fake parses --image greedily, like clap: the prompt's "-" must not be taken as another image.
    assert first["images"] == [image.path, second.path] and first["image_errors"] == []
    assert first["args"].count("--image") == 2  # only images
    assert first["args"][-1] == "-"
    assert "text copy: `C:/x/letter.docx.txt`" in first["prompt"]

    reply = codex_echo(collect(codex.send_reply("Again", attachments=[image])))
    args = reply["args"]
    assert args[:2] == ["exec", "resume"] and reply["images"] == [image.path] and reply["image_errors"] == []
    assert args[-2] == codex.native_session_id  # the thread id still comes last


# --- Local model: images for a vision model, text copies inline, attachments as a read root -------------


def test_local_vision_model_gets_this_turns_images_only(tmp_path: Path) -> None:
    image = stored(tmp_path / "files", "shot.png", samples.png())
    fake = FakeOllama()
    local = ollama_adapter(tmp_path, fake, vision=True, attachments_dir=tmp_path / "files")
    collect(local.start_task("What color?", attachments=[image]))
    user = fake.calls[0]["messages"][-1]
    assert user["images"] == [base64.b64encode(samples.png()).decode()]
    assert "hub_images" not in user
    # The saved conversation keeps the path, not the image.
    saved = saved_messages(tmp_path, local.native_session_id)[0]
    assert saved["hub_images"] == [image.path] and "images" not in saved

    collect(local.send_reply("And now?"))
    assert all("images" not in m and "hub_images" not in m for m in fake.calls[1]["messages"])
    # The model may read the attachments folder, but not write there.
    assert local.tools.resolve(image.path) == Path(image.path).resolve()


def test_local_model_without_vision_gets_a_notice_and_text_inline(tmp_path: Path) -> None:
    image = stored(tmp_path / "files", "shot.png", samples.png())
    copy = tmp_path / "fruit.txt"
    copy.write_text("apple\t3\n", encoding="utf-8")
    sheet = stored(tmp_path / "files", "fruit.xlsx", samples.xlsx(), text_path=str(copy))
    fake = FakeOllama()
    events = collect(ollama_adapter(tmp_path, fake).start_task("Count the fruit", attachments=[image, sheet]))
    notice = next(e for e in events if e.type is EventType.NOTICE)
    assert notice.data["reason"] == "no_vision" and "shot.png" in notice.data["message"]
    user = fake.calls[0]["messages"][-1]
    assert "images" not in user
    assert "### Text of fruit.xlsx" in user["content"] and "apple\t3" in user["content"]


# --- API ---------------------------------------------------------------------------------------------


def test_upload_send_show_and_open(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    image = upload(client, session_id, "shot.png", samples.png())
    report = upload(client, session_id, "report.pdf", samples.pdf())
    script = upload(client, session_id, "fix.ps1", b"Write-Output 'hi'\n")
    assert report["pages"] == 1 and report["text_path"].endswith("report.pdf.txt")

    response = client.post(f"/api/sessions/{session_id}/messages",
                           json={"text": "Check these", "attachments": [image["id"], report["id"], script["id"]]})
    assert response.status_code == 202, response.text
    wait_until_idle(client, session_id)
    events = client.get(f"/api/sessions/{session_id}/events").json()
    assert events[0]["payload"]["attachments"] == [image, report, script]
    sent = json.loads(events[-1]["payload"]["text"])
    assert [b["type"] for b in sent["blocks"]] == ["text", "image", "document"]
    assert script["path"] in sent["prompt"]

    shown = client.get(f"/api/sessions/{session_id}/attachments/{image['id']}/file")
    assert shown.status_code == 200 and shown.content == samples.png()
    assert shown.headers["content-type"] == "image/png" and shown.headers["x-content-type-options"] == "nosniff"
    as_text = client.get(f"/api/sessions/{session_id}/attachments/{script['id']}/file")
    assert as_text.headers["content-type"].startswith("text/plain")  # never served as anything that runs
    copy = client.get(f"/api/sessions/{session_id}/attachments/{report['id']}/file", params={"text": "true"})
    assert samples.PDF_WORD in copy.text
    assert client.get(f"/api/sessions/{session_id}/attachments/../file").status_code == 404

    opened_files.clear()
    assert client.post(f"/api/sessions/{session_id}/attachments/{report['id']}/open").json() == {"action": "opened"}
    assert client.post(f"/api/sessions/{session_id}/attachments/{script['id']}/open").json() == {"action": "revealed"}
    assert opened_files == [(Path(report["path"]), False), (Path(script["path"]), True)]

    # A sent file stays; an unsent one can be discarded.
    assert client.delete(f"/api/sessions/{session_id}/attachments/{image['id']}").status_code == 409
    extra = upload(client, session_id, "notes.txt", b"draft")
    assert client.delete(f"/api/sessions/{session_id}/attachments/{extra['id']}").status_code == 204
    assert not Path(extra["path"]).exists()

    folder = Path(image["path"]).parent.parent
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert not folder.exists()


def test_upload_refusals(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    url = f"/api/sessions/{session_id}/attachments"
    assert client.post(url, params={"name": "virus.exe"}, content=b"MZ").status_code == 415
    assert client.post(url, params={"name": "fake.png"}, content=b"not a png").status_code == 415
    assert client.post(url, params={"name": "empty.txt"}, content=b"").status_code == 400
    big = client.post(url, params={"name": "big.txt"}, content=b"x", headers={"Content-Length": str(50 * 1024 ** 2)})
    assert big.status_code in (400, 413)
    assert client.post(f"/api/sessions/{uuid.uuid4()}/attachments", params={"name": "a.txt"},
                       content=b"x").status_code == 404


def test_messages_with_attachments_only_unknown_ids_and_commands(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    messages = f"/api/sessions/{session_id}/messages"
    assert client.post(messages, json={"text": "  "}).status_code == 422
    assert client.post(messages, json={"text": "hi", "attachments": ["0123456789ab"]}).status_code == 400
    note = upload(client, session_id, "notes.txt", b"remember the milk")
    # Claude's /model is answered by the hub with a note: files have nowhere to go.
    refused = client.post(messages, json={"text": "/model", "attachments": [note["id"]]})
    assert refused.status_code == 400 and "/model" in refused.json()["detail"]
    assert client.post(messages, json={"text": "", "attachments": [note["id"]]}).status_code == 202
    wait_until_idle(client, session_id)
    sent = json.loads(client.get(f"/api/sessions/{session_id}/events").json()[-1]["payload"]["text"])
    assert sent["prompt"].startswith("## Attached files") and note["path"] in sent["prompt"]


def test_images_go_again_with_the_next_agents_first_message(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "claude:limit codex:ok")
    image = upload(client, session_id, "shot.png", samples.png())
    sheet = upload(client, session_id, "fruit.xlsx", samples.xlsx())
    client.post(f"/api/sessions/{session_id}/messages",
                json={"text": "What is in the picture?", "attachments": [image["id"], sheet["id"]]})
    body = wait_until_idle(client, session_id)
    assert body["agent"] == "codex" and body["status"] == "completed"
    sent = last_echo(client, session_id)
    assert sent["args"][sent["args"].index("--image") + 1] == image["path"]
    assert "## Files the user attached" in sent["prompt"] and sheet["text_path"] in sent["prompt"]


def test_a_waiting_handoff_sends_the_images_with_the_next_message(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    image = upload(client, session_id, "shot.png", samples.png())
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "Look", "attachments": [image["id"]]})
    wait_until_idle(client, session_id)
    response = client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "codex", "wait": True})
    assert response.status_code == 202, response.text

    client.post(f"/api/sessions/{session_id}/messages", json={"text": "Your turn"})
    wait_until_idle(client, session_id)
    sent = last_echo(client, session_id)
    assert sent["args"][sent["args"].index("--image") + 1] == image["path"]
    # The message after that is an ordinary reply, without the image again.
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "Thanks"})
    wait_until_idle(client, session_id)
    sent = last_echo(client, session_id)
    assert "--image" not in sent["args"]


def test_old_unsent_uploads_are_swept_at_startup(engine, tmp_path: Path) -> None:
    import os
    import time

    from app.attachments import META_FILE, AttachmentStore
    from app.main import create_app
    from support.hub import TEST_TOKEN, fake_adapter_factory, make_client

    store = AttachmentStore(tmp_path / "attachments")
    folder = store.session_dir(str(uuid.uuid4())) / "0123456789ab"
    folder.mkdir(parents=True)
    (folder / META_FILE).write_text(json.dumps({"sent": False}))
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(folder / META_FILE, (long_ago, long_ago))
    app = create_app(engine, fake_adapter_factory, token=TEST_TOKEN, attachments_dir=tmp_path / "attachments",
                     chats_dir=tmp_path / "chats", skills_dir=tmp_path / "skills", codex_home=tmp_path / "codex",
                     claude_home=tmp_path / "claude", discover_native=False)
    with make_client(app):
        assert not folder.exists()


def test_a_failing_cleanup_never_stops_startup(engine, tmp_path: Path, monkeypatch) -> None:
    from app.attachments import AttachmentStore
    from app.main import create_app
    from support.hub import TEST_TOKEN, fake_adapter_factory, make_client

    def broken(self, *args, **kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(AttachmentStore, "sweep_unsent", broken)
    app = create_app(engine, fake_adapter_factory, token=TEST_TOKEN, attachments_dir=tmp_path / "attachments",
                     chats_dir=tmp_path / "chats", skills_dir=tmp_path / "skills", codex_home=tmp_path / "codex",
                     claude_home=tmp_path / "claude", discover_native=False)
    with make_client(app) as client:
        assert client.get("/api/health").status_code == 200

