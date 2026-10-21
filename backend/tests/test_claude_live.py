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
