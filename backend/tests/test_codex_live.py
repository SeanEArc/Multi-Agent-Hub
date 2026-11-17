"""Runs the real `codex` CLI (uses a little ChatGPT Codex allowance). Opt in with HUB_LIVE_CODEX=1."""

import asyncio
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.adapters.base import EventType
from app.adapters.codex import CodexAdapter

pytestmark = pytest.mark.skipif(os.environ.get("HUB_LIVE_CODEX") != "1", reason="set HUB_LIVE_CODEX=1")


@pytest.fixture
def project() -> Iterator[Path]:
    # Not pytest's tmp_path: on Windows it is created with an owner-only ACL, and files the Codex
    # sandbox user creates there are then unreadable by us. A plain folder inherits normal access.
    path = Path(tempfile.gettempdir()) / f"hub-codex-live-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_question_then_reply(project: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    adapter = CodexAdapter(project)

    async def run(events):
        return [event async for event in events]

    first = asyncio.run(run(adapter.start_task(
        "Create a file named color.txt containing my favorite color. You don't know it yet."
    )))
    assert first[0].type is EventType.STARTED
    assert first[-1].type is EventType.QUESTION, first[-1]

    second = asyncio.run(run(adapter.send_reply("blue")))
    assert second[-1].type is EventType.COMPLETED, second[-1]
    assert "blue" in (project / "color.txt").read_text().lower()


def test_summarize_is_one_off_and_read_only(project: Path) -> None:
    adapter = CodexAdapter(project)

    async def run(events):
        return [event async for event in events]

    events = asyncio.run(run(adapter.summarize(
        "Summarize this session.\n\n## The task\n\nAdd a README.md describing the project.",
        "You write one-sentence summaries. Don't run commands or change files.",
    )))
    assert events[-1].type is EventType.COMPLETED, events[-1]
    assert events[-1].data["text"].strip()
    assert any(e.type is EventType.USAGE and e.data.get("usage") for e in events)
    assert list(project.iterdir()) == []


def test_commands_through_the_app_server(project: Path) -> None:
    """Revision 4: /context, /usage, /compact and /code-review through `codex app-server` on an exec thread."""
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=project, check=True)
    (project / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
    adapter = CodexAdapter(project, effort="low")

    async def run(events):
        return [event async for event in events]

    asyncio.run(run(adapter.start_task("Remember the code word HERON. Reply with just OK.")))
    context = asyncio.run(run(adapter.run_command("/context")))
    assert "tokens used" in context[-1].data["text"], context[-1]
    usage = asyncio.run(run(adapter.run_command("/usage")))
    assert any(e.type is EventType.RATE_LIMIT and e.data["windows"] for e in usage)
    compacted = asyncio.run(run(adapter.run_command("/compact")))
    assert [e.type for e in compacted] == [EventType.NOTICE, EventType.COMPLETED], compacted
    after = asyncio.run(run(adapter.send_reply("What was the code word? One word.")))
    [done] = [e for e in after if e.type is EventType.COMPLETED]
    assert "heron" in done.data["text"].lower()

    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    review = asyncio.run(run(adapter.run_command("/code-review")))
    assert review[-1].type is EventType.COMPLETED and review[-1].data["text"].strip(), review[-1]
