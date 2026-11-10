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


def test_coding_task_with_tools(tmp_path: Path) -> None:
    """Phase 5b's acceptance task: create a file, run it, report the output."""
    settings = get_settings()
    model = pick_model(list_models_sync(settings.ollama_url), None, settings.ollama_model)
    project = tmp_path / "project"
    project.mkdir()
    adapter = OllamaAdapter(project, model, history_dir=tmp_path / "history", base_url=settings.ollama_url,
                            num_ctx=settings.ollama_num_ctx, command_timeout=120)

    async def run(events):
        return [event async for event in events]

    events = asyncio.run(run(adapter.start_task(
        "Create a Python file hello.py that prints the sum of 17 and 25. Run it with python and tell me what it printed."
    )))
    for e in events:
        if e.type in (EventType.TOOL_USE, EventType.TOOL_RESULT, EventType.MESSAGE, EventType.PLAN):
            print(e.type, {k: v for k, v in e.data.items() if k != "diffs"})
    assert events[-1].type is EventType.COMPLETED, events[-1].data
    assert (project / "hello.py").is_file()
    tools = [e.data["name"] for e in events if e.type is EventType.TOOL_USE]
    assert "Write" in tools and "PowerShell" in tools
    assert any(e.type is EventType.TOOL_RESULT and "42" in e.data["content"] for e in events)
    assert "42" in events[-1].data["text"]
