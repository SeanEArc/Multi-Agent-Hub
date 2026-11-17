"""Agent commands (revision 4): routing `/name` messages to each agent's own commands."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.base import EventType
from app.adapters.claude import TurnParser
from app.commands import route
from app.models import AgentKind

from test_handoff import events_of, prompts_sent
from test_orchestrator import create, wait_until_idle

CAPABILITIES = {
    "skills": ["fake-skill"],
    "slash_commands": ["fake-skill", "compact", "context", "usage", "code-review", "clear", "model", "doctor",
                       "__internal", "hub:greet"],
    "terminal_slash_commands": ["doctor"],
}


def claude_args(client: TestClient, session_id: str) -> list[list[str]]:
    """The arguments of each fake Claude run that echoed them (not compactions, which reply with no text)."""
    return [json.loads(e["payload"]["text"])["args"] for e in events_of(client, session_id)
            if e["type"] == "completed" and e["agent"] == "claude" and e["payload"]["text"]]


def notices(client: TestClient, session_id: str) -> list[dict]:
    return [e["payload"] for e in events_of(client, session_id) if e["type"] == "notice"]


def test_route_follows_claudes_own_list() -> None:
    def action(text: str, caps: dict | None = CAPABILITIES) -> tuple[str, bool] | None:
        r = route(AgentKind.CLAUDE, text, caps)
        return (r.action, r.local) if r else None

    assert action("/compact keep the tests") == ("agent", False)
    assert action("/context") == ("agent", True) and action("/usage") == ("agent", True)
    assert action("/code-review") == ("agent", False) and action("/fake-skill x") == ("agent", False)
    for hidden in ("/clear", "/model opus", "/doctor"):
        assert action(hidden) == ("notice", False)
    assert action("/__internal") is None and action("/hub:greet") is None  # internals and the hub's own plugin
    assert action("/greet") is None and action("fix /compact") is None  # a library skill, plain text
    assert action("/code-review", None) == ("agent", False)  # nothing recorded yet: the commands every agent has
    assert route(AgentKind.CODEX, "/compact", CAPABILITIES) is None  # the hub's /compact for now


def test_claude_compacts_its_own_conversation(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "build the thing"})
    wait_until_idle(client, session_id)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/compact keep the tests"})
    session = wait_until_idle(client, session_id)

    assert session["status"] == "completed" and len(session["stints"]) == 1  # no handoff: Claude's own
    [notice] = notices(client, session_id)
    assert notice["reason"] == "compacted" and notice["trigger"] == "manual"
    assert notice["message"] == "Conversation compacted · 28.8k → 7.1k tokens"
    typed = [e["payload"]["text"] for e in events_of(client, session_id) if e["type"] == "user_message"]
    assert typed == ["build the thing", "/compact keep the tests"]

    client.post(f"/api/sessions/{session_id}/messages", json={"text": "next step"})
    wait_until_idle(client, session_id)
    first, last = claude_args(client, session_id)
    assert "--resume" in last and last[last.index("--resume") + 1] == first[first.index("--session-id") + 1]


def test_compact_before_the_conversation_starts_sends_nothing(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/compact"})
    assert "Nothing to compact" in notices(client, session_id)[0]["message"]
    assert not any(e["type"] in ("user_message", "started") for e in events_of(client, session_id))


def test_local_command_before_the_conversation_is_a_one_off(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/context"})
    session = wait_until_idle(client, session_id)
    assert session["native_session_id"] is None  # nothing saved to resume
    [args] = claude_args(client, session_id)
    assert "--no-session-persistence" in args and "--session-id" not in args
    assert prompts_sent(client, session_id, "claude") == ["/context"]

    client.post(f"/api/sessions/{session_id}/messages", json={"text": "hello"})
    session = wait_until_idle(client, session_id)
    assert "--session-id" in claude_args(client, session_id)[-1] and session["native_session_id"]


def test_hub_controlled_commands_answer_without_sending(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi"})
    wait_until_idle(client, session_id)  # Claude's list is recorded from this turn
    for text in ("/clear", "/model opus", "/doctor"):
        client.post(f"/api/sessions/{session_id}/messages", json={"text": text})
    messages = [n["message"] for n in notices(client, session_id)]
    assert "new chat" in messages[0] and "model picker" in messages[1] and "terminal" in messages[2]
    assert prompts_sent(client, session_id, "claude") == ["hi"]

    body = client.get("/api/skills/commands", params={"agent": "claude", "session_id": session_id}).json()
    names = [c["name"] for c in body["commands"]]
    assert names[:4] == ["compact", "context", "usage", "code-review"]
    assert not {"clear", "model", "doctor", "__internal"} & set(names)


def test_command_after_a_waiting_handoff_keeps_the_brief_behind_it(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok", agent="codex")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "build the thing"})
    wait_until_idle(client, session_id)
    response = client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "claude", "wait": True})
    assert response.status_code == 202, response.text
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/code-review the parser"})
    wait_until_idle(client, session_id)
    [prompt] = prompts_sent(client, session_id, "claude")
    assert prompt.startswith("/code-review the parser\n\n") and "build the thing" in prompt


def test_parser_reports_claudes_own_compaction() -> None:
    parser = TurnParser()
    [event] = parser.parse({"type": "system", "subtype": "compact_boundary",
                            "compact_metadata": {"trigger": "auto", "pre_tokens": 190000, "post_tokens": 900}})
    assert event.type is EventType.NOTICE
    assert event.data["message"] == "Claude compacted the conversation on its own · 190.0k → 900 tokens"
    assert parser.parse({"type": "system", "subtype": "status", "status": "compacting"}) == []
