"""Runs the real `claude` CLI (uses a little subscription usage). Opt in with HUB_LIVE_CLAUDE=1."""

import asyncio
import os
from pathlib import Path

import pytest

from app.adapters.base import EventType
from app.adapters.claude import ClaudeAdapter

pytestmark = pytest.mark.skipif(os.environ.get("HUB_LIVE_CLAUDE") != "1", reason="set HUB_LIVE_CLAUDE=1")


def test_question_then_reply(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(tmp_path, model="haiku")

    async def run(events):
        return [event async for event in events]

    first = asyncio.run(run(adapter.start_task(
        "Create a file named color.txt containing my favorite color. You don't know it yet."
    )))
    assert first[0].type is EventType.STARTED
    assert first[-1].type is EventType.QUESTION, first[-1]

    second = asyncio.run(run(adapter.send_reply("blue")))
    assert second[-1].type is EventType.COMPLETED, second[-1]
    assert "blue" in (tmp_path / "color.txt").read_text().lower()


def test_summarize_is_one_off_and_writes_nothing(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(tmp_path, model="haiku")

    async def run(events):
        return [event async for event in events]

    events = asyncio.run(run(adapter.summarize(
        "Summarize this session.\n\n## The task\n\nAdd a README.md describing the project.",
        "You write one-sentence summaries. Don't call tools.",
    )))
    assert events[-1].type is EventType.COMPLETED, events[-1]
    assert events[-1].data["text"].strip()
    assert any(e.type is EventType.USAGE and e.data.get("usage") for e in events)
    assert not any(e.type is EventType.TOOL_USE for e in events)
    assert list(tmp_path.iterdir()) == []


def test_compact_and_context_commands(tmp_path: Path) -> None:
    """Revision 4: commands sent as the prompt. `/compact` compacts the saved conversation, which still resumes."""
    adapter = ClaudeAdapter(tmp_path, model="haiku")

    async def run(events):
        return [event async for event in events]

    asyncio.run(run(adapter.start_task("Remember the code word PELICAN. Reply with just OK.")))
    compacted = asyncio.run(run(adapter.send_reply("/compact")))
    assert compacted[-1].type is EventType.COMPLETED, compacted[-1]
    [notice] = [e for e in compacted if e.type is EventType.NOTICE]
    assert notice.data["reason"] == "compacted" and notice.data["pre_tokens"] > notice.data["post_tokens"]

    after = asyncio.run(run(adapter.send_reply("What was the code word? One word.")))
    assert "pelican" in after[-1].data["text"].lower()

    fresh = ClaudeAdapter(tmp_path, model="haiku")
    report = asyncio.run(run(fresh.run_command("/context")))
    assert report[-1].type is EventType.COMPLETED and "context" in report[-1].data["text"].lower()
    assert fresh.native_session_id is None
    assert not any(e.type is EventType.STARTED and e.data.get("session_id") for e in report)


def test_attachments_as_content_blocks_on_new_and_resumed_turns(tmp_path: Path) -> None:
    """Revision 4 attachments: images and PDFs go as stream-json content blocks, also on a resumed turn, and
    --add-dir gives Claude the attachments folder without moving its working folder."""
    import attachment_samples as samples
    from app.attachments import AttachmentStore

    store = AttachmentStore(tmp_path / "attachments")
    session = "00000000-0000-4000-8000-00000000a77a"

    async def run(events):
        return [event async for event in events]

    async def chunks(data: bytes):
        yield data

    async def save(name: str, data: bytes):
        return await store.save(session, name, chunks(data))

    red = asyncio.run(save("red.png", samples.png((255, 0, 0))))
    report = asyncio.run(save("report.pdf", samples.pdf()))
    blue = asyncio.run(save("blue.png", samples.png((0, 0, 255))))
    work = tmp_path / "work"
    work.mkdir()
    adapter = ClaudeAdapter(work, model="haiku", add_dirs=[store.session_dir(session)])

    first = asyncio.run(run(adapter.start_task(
        "What color is the image, and what is the secret word in the PDF? Under 12 words, no tools.",
        attachments=[red, report])))
    assert first[0].data["cwd"] == str(work)
    assert first[-1].type is EventType.COMPLETED, first[-1]
    assert "red" in first[-1].data["text"].lower() and "pelican" in first[-1].data["text"].lower()

    second = asyncio.run(run(adapter.send_reply("What color is this new image? One word, no tools.",
                                                attachments=[blue])))
    assert second[-1].type is EventType.COMPLETED, second[-1]
    assert "blue" in second[-1].data["text"].lower()
