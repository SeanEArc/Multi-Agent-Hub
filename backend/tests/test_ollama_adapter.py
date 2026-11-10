import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.adapters.base import AgentEvent, EventType
from app.adapters.ollama import (
    OllamaAdapter,
    OllamaUnavailable,
    history_path,
    list_models_sync,
    models_from_tags,
    pick_model,
)
from app.main import create_app

from conftest import TEST_TOKEN, fake_adapter_factory, make_client
from fake_ollama import MODEL, TAGS, FakeOllama
from test_orchestrator import create, event_types, wait_until_idle


def collect(events: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    async def run() -> list[AgentEvent]:
        return [event async for event in events]

    return asyncio.run(run())


def adapter(tmp_path: Path, fake: FakeOllama, native_session_id: str | None = None, **kwargs) -> OllamaAdapter:
    return OllamaAdapter(tmp_path, MODEL, native_session_id, history_dir=tmp_path / "history",
                         transport=fake.transport, **kwargs)


def saved_messages(tmp_path: Path, conversation_id: str) -> list[dict]:
    return json.loads(history_path(tmp_path / "history", conversation_id).read_text(encoding="utf-8"))["messages"]


# --- Models ---------------------------------------------------------------------------------------


def test_models_from_tags() -> None:
    models = models_from_tags(TAGS)
    assert [m["name"] for m in models] == ["plain:1b", MODEL]
    assert models[1] == {"name": MODEL, "size": 6_550_825_550, "parameter_size": "9.0B", "quantization": "Q4_K_M",
                         "context_length": 262144, "tools": True, "thinking": True}
    assert models_from_tags({"models": [{"name": "old"}]})[0]["tools"] is None  # no capabilities reported


def test_pick_model() -> None:
    models = models_from_tags(TAGS)
    assert pick_model(models, None) == MODEL  # the first that supports tools
    assert pick_model(models, "plain:1b") == "plain:1b"
    assert pick_model(models, None, default="plain:1b") == "plain:1b"
    assert pick_model(models_from_tags({"models": [{"name": "llama:latest"}]}), "llama") == "llama:latest"
    with pytest.raises(OllamaUnavailable, match="ollama pull nope"):
        pick_model(models, "nope")
    with pytest.raises(OllamaUnavailable, match="No models"):
        pick_model([], None)


def test_list_models_sync() -> None:
    assert len(list_models_sync(transport=FakeOllama().sync_transport)) == 2
    with pytest.raises(OllamaUnavailable, match="isn't running"):
        list_models_sync(transport=FakeOllama("down").sync_transport)


def test_history_path_only_takes_uuids(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        history_path(tmp_path, "../../evil")


# --- Turns ----------------------------------------------------------------------------------------


def test_turn_streams_a_reply_and_saves_the_conversation(tmp_path: Path) -> None:
    fake = FakeOllama()
    local = adapter(tmp_path, fake)
    events = collect(local.start_task("hello"))

    assert [e.type for e in events] == [EventType.STARTED, EventType.MESSAGE, EventType.USAGE, EventType.COMPLETED]
    assert events[0].data == {"session_id": local.native_session_id, "model": MODEL, "cwd": str(tmp_path)}
    assert events[1].data["text"] == "Echo: hello (2 messages)"
    assert events[2].data == {"usage": {"input_tokens": 12, "output_tokens": 5}, "duration_ms": 1500}
    assert events[3].data["text"] == "Echo: hello (2 messages)"

    sent = fake.calls[0]
    assert sent["options"] == {"num_ctx": 32768}
    assert "think" not in sent
    assert sent["messages"][0]["role"] == "system" and MODEL in sent["messages"][0]["content"]
    assert saved_messages(tmp_path, local.native_session_id) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Echo: hello (2 messages)"},
    ]


def test_reply_continues_from_the_saved_conversation(tmp_path: Path) -> None:
    fake = FakeOllama()
    first = adapter(tmp_path, fake)
    collect(first.start_task("hello"))

    # A new adapter, as after a backend restart: the conversation comes from the file.
    resumed = adapter(tmp_path, fake, native_session_id=first.native_session_id, num_ctx=8192, think=False)
    events = collect(resumed.send_reply("and again"))
    assert events[-1].data["text"] == "Echo: and again (4 messages)"
    assert [m["content"] for m in fake.calls[1]["messages"][1:]] == ["hello", "Echo: hello (2 messages)", "and again"]
    assert fake.calls[1]["options"] == {"num_ctx": 8192}
    assert fake.calls[1]["think"] is False
    assert len(saved_messages(tmp_path, first.native_session_id)) == 4


def test_reply_without_history_fails_clearly(tmp_path: Path) -> None:
    local = adapter(tmp_path, FakeOllama(), native_session_id="00000000-0000-4000-8000-000000000000")
    events = collect(local.send_reply("hi"))
    assert [e.type for e in events] == [EventType.ERROR]
    assert "missing or unreadable" in events[0].data["message"]


def test_start_and_reply_order_is_enforced(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        adapter(tmp_path, FakeOllama()).send_reply("hi")
    with pytest.raises(RuntimeError):
        adapter(tmp_path, FakeOllama(), native_session_id="00000000-0000-4000-8000-000000000000").start_task("hi")


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("missing", f"The model {MODEL} isn't installed"),
        ("error", "HTTP 500: boom"),
        ("midstream", "Ollama: out of memory"),
        ("down", "Ollama isn't running"),
        ("empty", "empty answer"),
    ],
)
def test_failures_end_the_turn_with_an_error(tmp_path: Path, mode: str, message: str) -> None:
    local = adapter(tmp_path, FakeOllama(mode))
    events = collect(local.start_task("hello"))
    assert events[-1].type is EventType.ERROR
    assert message in events[-1].data["message"]
    if mode != "empty":
        # A failed message isn't kept, so sending it again doesn't put it in the conversation twice.
        assert not history_path(tmp_path / "history", local.native_session_id).exists()


def test_stop_cancels_a_hanging_request(tmp_path: Path) -> None:
    local = adapter(tmp_path, FakeOllama("hang"))

    async def run() -> list[AgentEvent]:
        seen = []
        async for event in local.start_task("hello"):
            seen.append(event)
            if event.type is EventType.STARTED:
                asyncio.get_running_loop().call_later(0.2, lambda: asyncio.ensure_future(local.stop()))
        return seen

    events = asyncio.run(asyncio.wait_for(run(), 5))
    assert events[-1].type is EventType.ERROR and events[-1].data["stopped"] is True
    # What the user saw is kept, so the next message follows on from it.
    assert saved_messages(tmp_path, local.native_session_id) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Thinking about it"},
    ]


def test_summarize_saves_nothing(tmp_path: Path) -> None:
    fake = FakeOllama()
    local = adapter(tmp_path, fake)
    events = collect(local.summarize("the session", "Write a summary."))
    assert [e.type for e in events] == [EventType.USAGE, EventType.COMPLETED]
    assert events[-1].data["text"] == "Echo: the session (2 messages)"
    assert fake.calls[0]["messages"][0] == {"role": "system", "content": "Write a summary."}
    assert local.native_session_id is None
    assert not (tmp_path / "history").exists()


# --- Through the API ------------------------------------------------------------------------------


def test_local_models_endpoint(engine: Engine) -> None:
    app = create_app(engine, fake_adapter_factory, token=TEST_TOKEN, ollama_url="http://127.0.0.1:11434",
                     ollama_transport=FakeOllama().transport)
    with make_client(app) as client:
        body = client.get("/api/local/models").json()
    assert body["available"] is True
    assert body["default"] == MODEL
    assert [m["name"] for m in body["models"]] == ["plain:1b", MODEL]
    assert body["models"][1]["tools"] is True


def test_local_models_endpoint_when_ollama_is_off(engine: Engine) -> None:
    app = create_app(engine, fake_adapter_factory, token=TEST_TOKEN, ollama_url="http://127.0.0.1:11434",
                     ollama_transport=FakeOllama("down").transport)
    with make_client(app) as client:
        body = client.get("/api/local/models").json()
    assert body == {"url": "http://127.0.0.1:11434", "available": False, "models": [], "default": None,
                    "error": "Ollama isn't running at http://127.0.0.1:11434. Start Ollama and try again."}


def test_local_session_chats_continues_and_is_deleted_with_its_history(engine: Engine, tmp_path: Path) -> None:
    app = create_app(engine, fake_adapter_factory, token=TEST_TOKEN, local_dir=tmp_path / "local-history")
    with make_client(app) as client:
        session_id = create(client, tmp_path, "ollama:ok", agent="ollama")
        client.post(f"/api/sessions/{session_id}/messages", json={"text": "hello"})
        session = wait_until_idle(client, session_id)
        assert session["status"] == "completed"
        assert session["reported_model"] == MODEL

        client.post(f"/api/sessions/{session_id}/messages", json={"text": "more"})
        assert wait_until_idle(client, session_id)["status"] == "completed"
        messages = [e["payload"]["text"] for e in client.get(f"/api/sessions/{session_id}/events").json()
                    if e["type"] == "message"]
        assert messages == ["Echo: hello (2 messages)", "Echo: more (4 messages)"]

        history = history_path(tmp_path / "local-history", session["native_session_id"])
        assert history.exists()
        assert client.delete(f"/api/sessions/{session_id}").status_code == 204
        assert not history.exists()


def test_local_model_takes_over_from_codex(client: TestClient, tmp_path: Path) -> None:
    # The default chain is Claude, Codex, local model, so a limit on Codex hands off to the local model.
    session_id = create(client, tmp_path, "codex:limit ollama:ok", agent="codex")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "build it"})
    session = wait_until_idle(client, session_id)
    assert (session["status"], session["agent"]) == ("completed", "ollama")
    assert "handoff" in event_types(client, session_id)
    last = client.get(f"/api/sessions/{session_id}/events").json()[-1]
    assert last["type"] == "completed" and "build it" in last["payload"]["text"]  # the brief carries the task
