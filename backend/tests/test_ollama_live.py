"""Runs against the real local Ollama server (free, but slow on a modest GPU). Opt in with HUB_LIVE_OLLAMA=1.

Uses HUB_OLLAMA_MODEL if set, otherwise the first installed model that supports tools.
"""

import asyncio
import os
from pathlib import Path

import pytest

from app.adapters.base import EventType
from app.adapters.ollama import OllamaAdapter, list_models_sync, pick_model
from app.config import get_settings

pytestmark = pytest.mark.skipif(os.environ.get("HUB_LIVE_OLLAMA") != "1", reason="set HUB_LIVE_OLLAMA=1")


def test_chat_then_reply(tmp_path: Path) -> None:
    settings = get_settings()
    model = pick_model(list_models_sync(settings.ollama_url), None, settings.ollama_model)
    adapter = OllamaAdapter(tmp_path, model, history_dir=tmp_path / "history", base_url=settings.ollama_url,
                            num_ctx=settings.ollama_num_ctx)

    async def run(events):
        return [event async for event in events]

    first = asyncio.run(run(adapter.start_task("My favorite color is teal. Reply with just: noted")))
    assert first[0].type is EventType.STARTED
    assert first[-1].type is EventType.COMPLETED, first[-1].data
    usage = next(e for e in first if e.type is EventType.USAGE).data["usage"]
    assert usage["input_tokens"] > 0 and usage["output_tokens"] > 0

    # A fresh adapter, as after a restart: the conversation comes back from the saved file.
    resumed = OllamaAdapter(tmp_path, model, adapter.native_session_id, history_dir=tmp_path / "history",
                            base_url=settings.ollama_url, num_ctx=settings.ollama_num_ctx)
    second = asyncio.run(run(resumed.send_reply("What is my favorite color? Answer in one word.")))
    assert second[-1].type is EventType.COMPLETED, second[-1].data
    assert "teal" in second[-1].data["text"].lower()
