"""The local model's adapter against FakeOllama, and the conversation history it saves."""

import json
from pathlib import Path

from app.adapters.ollama import OllamaAdapter, history_path

from support.fake_ollama import MODEL, FakeOllama


def adapter(tmp_path: Path, fake: FakeOllama, native_session_id: str | None = None, **kwargs) -> OllamaAdapter:
    return OllamaAdapter(tmp_path, MODEL, native_session_id, history_dir=tmp_path / "history",
                         transport=fake.transport, **kwargs)


def saved_messages(tmp_path: Path, conversation_id: str) -> list[dict]:
    return json.loads(history_path(tmp_path / "history", conversation_id).read_text(encoding="utf-8"))["messages"]
