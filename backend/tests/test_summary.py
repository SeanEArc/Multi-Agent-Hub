import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.base import EventType
from app.handoff import GitState
from app.models import AgentKind, PlanItemRead, SessionEvent, Stint, StintReason, summary_headline
from app.summary import build_summary_prompt, total_tokens

from test_orchestrator import create, wait_until_idle


def test_current_agent_writes_summary_outside_the_session(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "add a login page"})
    native_id = wait_until_idle(client, session_id)["native_session_id"]

    response = client.post(f"/api/sessions/{session_id}/summary")
    assert response.status_code == 201, response.text
    summary = response.json()
    assert summary["agent"] == "claude"
    assert summary["fallback_from"] is None
    assert not summary["outdated"]
    sent = json.loads(summary["text"])
    # A one-off call: not saved, no tools, its own system prompt, and not the session's conversation.
    assert "--no-session-persistence" in sent["args"]
    assert sent["args"][sent["args"].index("--tools") + 1] == ""
    assert "--system-prompt" in sent["args"]
    assert "--resume" not in sent["args"] and native_id not in sent["args"]
    assert "add a login page" in sent["prompt"]
    # No new events: the summary stays out of the transcript.
    assert [e["type"] for e in client.get(f"/api/sessions/{session_id}/events").json()][-1] == "completed"

    body = client.get(f"/api/sessions/{session_id}").json()
    assert body["summary"]["id"] == summary["id"]
    assert body["task"] == "add a login page"
    info = client.get(f"/api/sessions/{session_id}/summary").json()
    assert info == {"git": None, "summary": body["summary"], "summarizing": False}

    # The next turn makes it outdated.
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "and a logout button"})
    wait_until_idle(client, session_id)
    assert client.get(f"/api/sessions/{session_id}").json()["summary"]["outdated"]


def test_next_agent_in_chain_writes_summary_when_current_is_out_of_usage(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "claude:limit codex:ok")

    response = client.post(f"/api/sessions/{session_id}/summary")
    assert response.status_code == 201, response.text
    summary = response.json()
    assert summary["agent"] == "codex"
    assert summary["fallback_from"] == "claude"
    assert summary["tokens"] == 15
    args = json.loads(summary["text"])["args"]
    assert "--ephemeral" in args
    assert 'sandbox_mode="read-only"' in args
    assert args[:2] == ["exec", "--json"]


def test_summary_fails_when_no_agent_can_write_it(client: TestClient, tmp_path: Path) -> None:
    # Codex's only successor in the default chain is the local model, which isn't available yet.
    session_id = create(client, tmp_path, "limit", agent="codex")
    response = client.post(f"/api/sessions/{session_id}/summary")
    assert response.status_code == 400
    assert client.get(f"/api/sessions/{session_id}").json()["summary"] is None


def test_summary_error_is_reported(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "crash")
    response = client.post(f"/api/sessions/{session_id}/summary")
    assert response.status_code == 502
    assert "exited with code 2" in response.json()["detail"]


def test_summary_prompt_holds_task_plan_last_turn_and_git() -> None:
    stints = [
        Stint(id=1, session_id="s", seq=1, agent=AgentKind.CLAUDE, reason=StintReason.NEW),
        Stint(id=2, session_id="s", seq=2, agent=AgentKind.CODEX, reason=StintReason.LIMIT),
    ]

    def event(type_: EventType, agent: AgentKind, **payload) -> SessionEvent:
        return SessionEvent(session_id="s", stint_id=1 if agent is AgentKind.CLAUDE else 2, agent=agent,
                            type=type_, payload=payload)

    events = [
        event(EventType.USER_MESSAGE, AgentKind.CLAUDE, text="build the parser"),
        event(EventType.TOOL_USE, AgentKind.CLAUDE, name="Edit", input={"file_path": "parser.py"}),
        event(EventType.LIMIT_REACHED, AgentKind.CLAUDE),
        event(EventType.HANDOFF, AgentKind.CODEX),
        event(EventType.MESSAGE, AgentKind.CODEX, text="Tests pass now."),
        event(EventType.QUESTION, AgentKind.CODEX, text="Which format?"),
    ]
    plan = [PlanItemRead(text="Write parser", status="completed"), PlanItemRead(text="Add tests", status="in_progress")]
    git = GitState(status=" M parser.py\n", diff_stat=" parser.py | 3 +++\n", diff="+x\n")

    prompt = build_summary_prompt("Parser", events, stints, plan, git)
    assert "build the parser" in prompt
    assert "- Codex (took over after a usage limit)" in prompt
    assert "- [x] Write parser" in prompt and "- [ ] (in progress) Add tests" in prompt
    assert "Codex:\n\nTests pass now." in prompt
    assert "Codex asked the user a question" in prompt
    assert "- parser.py" in prompt
    assert " M parser.py" in prompt and "```diff\n+x\n```" in prompt


def test_headline_and_tokens() -> None:
    assert summary_headline("\n## **Adding** tests, step 2 of 3\n\n**Goal**") == "Adding tests, step 2 of 3"
    assert summary_headline("") == ""
    assert total_tokens({"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 2}) == 17
    assert total_tokens(None) is None
