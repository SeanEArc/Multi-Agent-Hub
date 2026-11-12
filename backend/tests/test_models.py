import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.adapters.base import AgentAdapter
from app.adapters.claude import ClaudeAdapter
from app.main import create_app
from app.model_list import CLAUDE_VERSIONS, is_unavailable_model_error, read_codex_models
from app.models import AgentKind, AgentSession, Stint
from app.orchestrator import AgentUnavailable

from conftest import FAKE_CLAUDE, TEST_TOKEN, make_client
from test_orchestrator import create, wait_until_idle

# Fake Claude modes for the check, by model: refused by the plan, out of usage, or fine.
CHECK_MODES = {"claude-opus-4-6": "badmodel", "claude-haiku-4-5": "limit"}


def write_codex_cache(codex_home: Path) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "models_cache.json").write_text(json.dumps({
        "fetched_at": "2026-11-11T00:00:00Z",
        "identity": {"account": "not read"},
        "models": [
            {"slug": "gpt-b", "display_name": "GPT-B", "visibility": "list", "priority": 2},
            {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide", "priority": 1},
            {"slug": "gpt-a", "display_name": "GPT-A", "visibility": "list", "priority": 1},
        ],
    }), encoding="utf-8")


def test_catalog_lists_claude_versions_and_builtin_codex(client: TestClient) -> None:
    body = client.get("/api/models").json()
    assert [m["id"] for m in body["claude"]] == [m for m, _ in CLAUDE_VERSIONS]
    assert body["claude"][0] == {"id": "claude-fable-5-2", "label": "Fable 5.2", "group": "version",
                                 "available": None, "note": None, "checked_at": None,
                                 "efforts": ["low", "medium", "high", "xhigh", "max"], "default_effort": None}
    assert {m["group"] for m in body["claude"]} == {"version"}
    # The test app's Codex folder has no cache, so the built-in list is shown.
    assert body["codex_source"] == "builtin"
    assert body["codex"][0]["id"] == "gpt-6.1-sol"
    assert body["checked_at"] == {"claude": None, "codex": None}


def test_codex_models_come_from_the_account_cache(tmp_path: Path) -> None:
    write_codex_cache(tmp_path)
    assert read_codex_models(tmp_path) == [("gpt-a", "GPT-A"), ("gpt-b", "GPT-B")]
    assert read_codex_models(tmp_path / "missing") is None
    (tmp_path / "models_cache.json").write_text("{not json", encoding="utf-8")
    assert read_codex_models(tmp_path) is None


def test_catalog_uses_codex_account_list(engine: Engine, tmp_path: Path) -> None:
    write_codex_cache(tmp_path / "codex")
    app = create_app(engine, token=TEST_TOKEN, chats_dir=tmp_path / "chats", codex_home=tmp_path / "codex")
    with make_client(app) as client:
        body = client.get("/api/models").json()
    assert body["codex_source"] == "account"
    assert [(m["id"], m["group"]) for m in body["codex"]] == [("gpt-a", "account"), ("gpt-b", "account")]


def test_turns_teach_which_models_the_plan_can_use(client: TestClient, tmp_path: Path) -> None:
    refused = create(client, tmp_path, "badmodel", model="claude-opus-4-6")
    client.post(f"/api/sessions/{refused}/messages", json={"text": "hi"})
    assert wait_until_idle(client, refused)["status"] == "failed"

    works = create(client, tmp_path, "ok", model="claude-sonnet-5")
    client.post(f"/api/sessions/{works}/messages", json={"text": "hi"})
    wait_until_idle(client, works)

    models = {m["id"]: m for m in client.get("/api/models").json()["claude"]}
    assert models["claude-opus-4-6"]["available"] is False
    assert "selected model" in models["claude-opus-4-6"]["note"]
    assert models["claude-sonnet-5"]["available"] is True
    assert models["claude-opus-5-5"]["available"] is None  # never used


def test_other_failures_teach_nothing(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "crash", model="claude-opus-5")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi"})
    wait_until_idle(client, session_id)
    models = {m["id"]: m for m in client.get("/api/models").json()["claude"]}
    assert models["claude-opus-5"]["available"] is None


def check_factory(session: AgentSession, stint: Stint, cwd: Path) -> AgentAdapter:
    if stint.agent is not AgentKind.CLAUDE:
        raise AgentUnavailable("only Claude in this test")
    mode = CHECK_MODES.get(stint.model or "", "ok")
    return ClaudeAdapter(cwd, model=stint.model, command=[sys.executable, str(FAKE_CLAUDE), mode])


def test_check_tries_every_model_and_records_results(engine: Engine, tmp_path: Path) -> None:
    app = create_app(engine, check_factory, token=TEST_TOKEN, chats_dir=tmp_path / "chats",
                     codex_home=tmp_path / "codex")
    with make_client(app) as client:
        response = client.post("/api/models/check", json={"agent": "claude"})
        assert response.status_code == 200, response.text
        body = response.json()
        models = {m["id"]: m for m in body["claude"]}
        assert models["claude-opus-4-6"]["available"] is False
        assert models["claude-haiku-4-5"]["available"] is None  # out of usage: unknown, not refused
        assert "Out of usage" in models["claude-haiku-4-5"]["note"]
        assert all(m["available"] for k, m in models.items() if k not in CHECK_MODES)
        assert body["checked_at"]["claude"] is not None

        # Codex can't run here: the check refuses before trying any model.
        assert client.post("/api/models/check", json={"agent": "codex"}).status_code == 400
        assert client.post("/api/models/check", json={"agent": "ollama"}).status_code == 400


def test_unavailable_model_wording() -> None:
    assert is_unavailable_model_error(
        "There's an issue with the selected model (claude-x). It may not exist or you may not have access to it.")
    assert is_unavailable_model_error("The model `gpt-z` does not exist")
    assert is_unavailable_model_error("invalid model: foo")
    assert not is_unavailable_model_error("You've hit your limit")
    assert not is_unavailable_model_error("Agent run failed: boom")
    assert not is_unavailable_model_error(None)
