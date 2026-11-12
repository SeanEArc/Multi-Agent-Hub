"""Reasoning effort: passed to each CLI per turn, changed between turns, carried by a handoff, listed per model."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.model_list import read_codex_efforts

from test_orchestrator import create, wait_until_idle


def last_args(client: TestClient, session_id: str) -> list[str]:
    """The arguments the fake CLI echoed in its last turn."""
    events = client.get(f"/api/sessions/{session_id}/events").json()
    done = [e for e in events if e["type"] == "completed"]
    return json.loads(done[-1]["payload"]["text"])["args"]


def test_claude_gets_effort_and_it_changes_between_turns(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok", effort="high")
    assert client.get(f"/api/sessions/{session_id}").json()["effort"] == "high"
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    wait_until_idle(client, session_id)
    args = last_args(client, session_id)
    assert args[args.index("--effort") + 1] == "high"

    # Changed between turns on the same stint: the next turn (a resume) uses it.
    session = client.patch(f"/api/sessions/{session_id}", json={"effort": "low"}).json()
    assert (session["effort"], len(session["stints"])) == ("low", 1)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "more"})
    wait_until_idle(client, session_id)
    args = last_args(client, session_id)
    assert "--resume" in args and args[args.index("--effort") + 1] == "low"

    # Back to the CLI's default: no flag.
    client.patch(f"/api/sessions/{session_id}", json={"effort": None})
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "again"})
    wait_until_idle(client, session_id)
    assert "--effort" not in last_args(client, session_id)


def test_unknown_effort_is_rejected(client: TestClient, tmp_path: Path) -> None:
    body = {"title": "ok", "project_path": str(tmp_path), "agent": "claude", "effort": "ultra"}
    assert client.post("/api/sessions", json=body).status_code == 422


def test_handoff_carries_effort_to_codex(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok", effort="high")
    client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "codex", "effort": "xhigh", "wait": True})
    session = client.get(f"/api/sessions/{session_id}").json()
    assert (session["agent"], session["effort"]) == ("codex", "xhigh")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "go"})
    wait_until_idle(client, session_id)
    assert 'model_reasoning_effort="xhigh"' in last_args(client, session_id)


def test_codex_efforts_come_from_the_cache(tmp_path: Path) -> None:
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-a", "visibility": "list", "default_reasoning_level": "medium", "supported_reasoning_levels": [
            {"effort": "ultra"}, {"effort": "high"}, {"effort": "low"}, {"effort": "medium"}]},
        {"slug": "gpt-b", "visibility": "list"},
    ]}), encoding="utf-8")
    # Lowest first, "ultra" left out; a model without levels isn't listed (the catalog keeps the full set).
    assert read_codex_efforts(tmp_path) == {"gpt-a": (["low", "medium", "high"], "medium")}
    assert read_codex_efforts(tmp_path / "missing") == {}


def test_new_stints_start_with_the_default_effort(client: TestClient, tmp_path: Path) -> None:
    # Settings' default (medium) when the session names none; the local model gets none.
    assert client.get(f"/api/sessions/{create(client, tmp_path, 'ok')}").json()["effort"] == "medium"
    assert client.get(f"/api/sessions/{create(client, tmp_path, 'ollama:ok', agent='ollama')}").json()["effort"] is None
    assert client.put("/api/settings", json={"default_effort": "max"}).status_code == 200
    assert client.put("/api/settings", json={"default_effort": None}).status_code == 422

    # An automatic handoff names no effort, so the new agent starts with the default too.
    session_id = create(client, tmp_path, "claude:limit codex:ok", effort="low")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    session = wait_until_idle(client, session_id)
    assert (session["agent"], session["effort"]) == ("codex", "max")
    assert 'model_reasoning_effort="max"' in last_args(client, session_id)


def test_local_model_ignores_an_effort_change(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ollama:ok", agent="ollama")
    response = client.patch(f"/api/sessions/{session_id}", json={"effort": "high"})
    assert response.status_code == 200
    assert response.json()["effort"] is None
