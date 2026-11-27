"""Events say which output files an agent created or edited, so the transcript can show them where it happened."""

import time
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.adapters.base import AgentAdapter, AgentEvent, EventType
from app.main import create_app
from app.models import AgentKind, AgentSession, Stint

from support.hub import TEST_TOKEN, make_client, pr2_seams


class Writer(AgentAdapter):
    """First turn: creates a file in a tool call, then one that no result reports. Later turns: edits the first."""

    kind = AgentKind.CLAUDE

    def __init__(self) -> None:
        self._id: str | None = None

    @property
    def native_session_id(self) -> str | None:
        return self._id

    async def stop(self) -> None:
        pass

    async def start_task(self, prompt, attachments=()) -> AsyncGenerator[AgentEvent]:
        self._id = "11111111-1111-4111-8111-111111111111"
        async for event in self._turn(["report.csv"], "late.txt"):
            yield event

    async def send_reply(self, reply, attachments=()) -> AsyncGenerator[AgentEvent]:
        async for event in self._turn(["report.csv"], None):
            yield event

    async def _turn(self, names: list[str], late: str | None) -> AsyncGenerator[AgentEvent]:
        outputs = self.documents.outputs
        yield AgentEvent(EventType.TOOL_USE, {"tool_use_id": "a", "name": "Read", "input": {}})
        yield AgentEvent(EventType.TOOL_RESULT, {"tool_use_id": "a", "content": ""})
        yield AgentEvent(EventType.TOOL_USE, {"tool_use_id": "b", "name": "PowerShell", "input": {"command": "x"}})
        for name in names:
            with (outputs / name).open("a") as file:
                file.write("a,b\n")
        yield AgentEvent(EventType.TOOL_RESULT, {"tool_use_id": "b", "content": ""})
        if late:
            (outputs / late).write_text("x")
        yield AgentEvent(EventType.COMPLETED, {"text": "done"})


def run_turn(client: TestClient, session_id: str, text: str) -> list[dict]:
    """Sends the message and returns the turn's events once it ended."""
    before = client.get(f"/api/sessions/{session_id}/events").json()
    last = before[-1]["id"] if before else 0
    assert client.post(f"/api/sessions/{session_id}/messages", json={"text": text}).is_success
    deadline = time.time() + 15
    while client.get(f"/api/sessions/{session_id}").json()["status"] != "completed":
        assert time.time() < deadline
        time.sleep(0.05)
    return client.get(f"/api/sessions/{session_id}/events", params={"after_id": last}).json()


def changed(events: list[dict]) -> list[tuple[str, str, str]]:
    return [(e["type"], c["name"], c["change"]) for e in events for c in e["payload"].get("output_files", [])]


def test_events_name_the_output_files_written(engine: Engine, tmp_path_factory) -> None:
    def factory(session: AgentSession, stint: Stint, cwd: Path) -> AgentAdapter:
        return Writer()

    app = create_app(engine, factory, token=TEST_TOKEN, chats_dir=tmp_path_factory.mktemp("chats"),
                     worktrees_dir=tmp_path_factory.mktemp("worktrees"),
                     codex_home=tmp_path_factory.mktemp("codex"), skills_dir=tmp_path_factory.mktemp("skills"),
                     claude_home=tmp_path_factory.mktemp("claude"), discover_native=False,
                     attachments_dir=tmp_path_factory.mktemp("attachments"), **pr2_seams(tmp_path_factory))
    with make_client(app) as client:
        session_id = client.post("/api/sessions", json={"title": "t", "agent": "claude"}).json()["id"]
        events = run_turn(client, session_id, "go")
        # Only the tool that wrote the file names it; the turn's end names what no tool result reported.
        assert changed(events) == [("tool_result", "report.csv", "created"), ("completed", "late.txt", "created")]
        # A file that was already there is an edit, and untouched files aren't listed again.
        assert changed(run_turn(client, session_id, "again")) == [("tool_result", "report.csv", "edited")]
