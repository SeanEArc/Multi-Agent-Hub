from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.models import AgentKind, SessionEvent

from test_orchestrator import create


def add_event(engine: Engine, session_id: str, agent: AgentKind, type: str, payload: dict) -> None:
    with Session(engine) as db:
        db.add(SessionEvent(session_id=session_id, agent=agent, type=type, payload=payload))
        db.commit()


def test_usage_is_latest_report_per_agent_across_sessions(client: TestClient, engine: Engine, tmp_path: Path) -> None:
    assert client.get("/api/usage").json() == {}

    first = create(client, tmp_path, "ok")
    second = create(client, tmp_path, "ok", agent="codex")
    window = {"five_hour": {"utilization": 0.1}}
    add_event(engine, first, AgentKind.CLAUDE, "rate_limit", {"status": "allowed", "windows": window})
    add_event(engine, second, AgentKind.CODEX, "rate_limit",
              {"status": "allowed", "windows": {"five_hour": {"utilization": 0.2}}, "plan_type": "plus"})
    add_event(engine, second, AgentKind.CLAUDE, "limit_reached",
              {"status": "rejected", "windows": {"five_hour": {"utilization": 1.0}}})
    # A limit found only from error text has no windows, so the report before it still counts.
    add_event(engine, first, AgentKind.CODEX, "limit_reached", {"message": "You've hit your usage limit."})

    usage = client.get("/api/usage").json()
    assert set(usage) == {"claude", "codex"}
    assert (usage["claude"]["status"], usage["claude"]["windows"]["five_hour"]["utilization"]) == ("rejected", 1.0)
    assert (usage["codex"]["plan_type"], usage["codex"]["windows"]["five_hour"]["utilization"]) == ("plus", 0.2)
    assert usage["codex"]["reported_at"]
