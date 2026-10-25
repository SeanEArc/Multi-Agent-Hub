import asyncio
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.db import init_db
from app.main import create_app
from app.models import AgentKind, AgentSession, SessionStatus
from app.orchestrator import Orchestrator

from conftest import TEST_TOKEN, fake_adapter_factory, make_client


def create(client: TestClient, path: Path, mode: str, agent: str = "claude") -> str:
    response = client.post("/api/sessions", json={"title": mode, "project_path": str(path), "agent": agent})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def wait_until_idle(client: TestClient, session_id: str, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/sessions/{session_id}").json()
        if body["status"] != "working":
            return body
        time.sleep(0.05)
    raise AssertionError("session still working")


def event_types(client: TestClient, session_id: str) -> list[str]:
    return [e["type"] for e in client.get(f"/api/sessions/{session_id}/events").json()]


def test_turn_runs_and_completes(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")

    response = client.post(f"/api/sessions/{session_id}/messages", json={"text": "do the thing"})
    assert response.status_code == 202
    assert response.json()["status"] == "working"
    assert response.json()["native_session_id"]

    assert wait_until_idle(client, session_id)["status"] == "completed"
    assert event_types(client, session_id) == ["user_message", "started", "message", "usage", "completed"]


def test_question_then_reply_resumes_same_session(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "question")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    first = wait_until_idle(client, session_id)
    assert first["status"] == "waiting_for_input"

    client.post(f"/api/sessions/{session_id}/messages", json={"text": "blue"})
    wait_until_idle(client, session_id)

    final = client.get(f"/api/sessions/{session_id}/events").json()[-1]
    sent = json.loads(final["payload"]["text"])
    assert sent["prompt"] == "blue"
    assert sent["args"][sent["args"].index("--resume") + 1] == first["native_session_id"]


def test_codex_session_saves_reported_thread_and_resumes_it(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "question", agent="codex")
    response = client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    assert response.json()["native_session_id"] is None  # Codex picks the id once the turn starts
    first = wait_until_idle(client, session_id)
    assert first["status"] == "waiting_for_input"
    assert first["native_session_id"]

    client.post(f"/api/sessions/{session_id}/messages", json={"text": "blue"})
    wait_until_idle(client, session_id)
    sent = json.loads(client.get(f"/api/sessions/{session_id}/events").json()[-1]["payload"]["text"])
    assert sent["prompt"] == "blue"
    assert sent["args"][-2] == first["native_session_id"]


def test_codex_limit_fails_session(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "limit", agent="codex")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    assert wait_until_idle(client, session_id)["status"] == "failed"  # phase 3 hands off instead
    assert event_types(client, session_id)[-1] == "limit_reached"


def test_busy_session_rejects_second_message_and_can_stop(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "hang")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})

    assert client.post(f"/api/sessions/{session_id}/messages", json={"text": "again"}).status_code == 409

    stopped = client.post(f"/api/sessions/{session_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "ready"
    assert event_types(client, session_id)[-1] == "error"
    assert client.post(f"/api/sessions/{session_id}/stop").status_code == 409


def test_failed_turn_marks_session_failed(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "crash")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    assert wait_until_idle(client, session_id)["status"] == "failed"


def test_delete_stops_running_turn(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "hang")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_unsupported_agent(engine: Engine, tmp_path: Path) -> None:
    with make_client(create_app(engine, token=TEST_TOKEN)) as client:  # real factory
        session_id = create(client, tmp_path, "ok", agent="ollama")
        response = client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi"})
        assert response.status_code == 400
        assert "not supported" in response.json()["detail"]


def test_interrupted_turns_fail_on_startup(engine: Engine, tmp_path: Path) -> None:
    init_db(engine)
    with Session(engine) as db:
        db.add(AgentSession(id="s1", title="x", project_path=str(tmp_path), status=SessionStatus.WORKING))
        db.commit()

    with make_client(create_app(engine, fake_adapter_factory, token=TEST_TOKEN)) as client:
        assert client.get("/api/sessions/s1").json()["status"] == "failed"
        assert event_types(client, "s1") == ["error"]


def test_follow_sends_backlog_then_live_events(engine: Engine, tmp_path: Path) -> None:
    init_db(engine)
    with Session(engine) as db:
        db.add(AgentSession(id="s1", title="ok", project_path=str(tmp_path), agent=AgentKind.CLAUDE))
        db.commit()

    async def run() -> list[tuple[str, object]]:
        orchestrator = Orchestrator(engine, fake_adapter_factory)
        seen = []
        async for message in orchestrator.follow("s1"):
            seen.append((message.kind, message.data.get("status") or message.data.get("type")))
            if message.kind == "status" and message.data["status"] == SessionStatus.READY:
                await orchestrator.send("s1", "hello")  # start the turn once subscribed
            if message.kind == "status" and message.data["status"] == SessionStatus.COMPLETED:
                break
        return seen

    seen = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert seen == [
        ("status", "ready"),
        ("event", "user_message"),
        ("status", "working"),
        ("event", "started"),
        ("event", "message"),
        ("event", "usage"),
        ("event", "completed"),
        ("status", "completed"),
    ]


def test_stream_endpoint_replays_saved_events(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi"})
    wait_until_idle(client, session_id)
    last_id = client.get(f"/api/sessions/{session_id}/events").json()[1]["id"]

    # The stream never ends on its own, so read just the first few SSE frames.
    with client.stream("GET", f"/api/sessions/{session_id}/stream", headers={"Last-Event-ID": str(last_id)}) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        frames = []
        for line in response.iter_lines():
            if line.startswith("event:"):
                frames.append(line.removeprefix("event: "))
            if len(frames) == 4:
                break
    assert frames == ["status", "event", "event", "event"]  # status, then events after last_id
