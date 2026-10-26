import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app.adapters.base import AgentAdapter
from app.adapters.claude import ClaudeAdapter
from app.adapters.codex import CodexAdapter
from app.db import make_engine
from app.main import create_app
from app.models import AgentKind, AgentSession, Stint
from app.orchestrator import AgentUnavailable

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
FAKE_CODEX = Path(__file__).parent / "fake_codex.py"
TEST_TOKEN = "test-token"
# The Host check only accepts loopback names, so tests can't use TestClient's "testserver".
BASE_URL = "http://127.0.0.1:8765"


def make_client(app: FastAPI, token: str | None = TEST_TOKEN) -> TestClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, base_url=BASE_URL, headers=headers)


def fake_mode(title: str, agent: AgentKind) -> str:
    """The fake CLI mode from a session title: "ok", or per agent as in "claude:limit codex:question"."""
    modes = dict(part.split(":", 1) for part in title.split() if ":" in part)
    return modes.get(agent.value, "ok") if modes else title


def fake_adapter_factory(session: AgentSession, stint: Stint, cwd: Path) -> AgentAdapter:
    """Adapter driving tests/fake_claude.py or fake_codex.py; the session title picks its mode (ok, question, hang...)."""
    mode = fake_mode(session.title, stint.agent)
    if stint.agent is AgentKind.CODEX:
        return CodexAdapter(
            cwd,
            native_session_id=stint.native_session_id,
            command=[sys.executable, str(FAKE_CODEX), mode],
            codex_home=Path(session.project_path) / "codex-home",  # never the real ~/.codex
        )
    if stint.agent is AgentKind.CLAUDE:
        return ClaudeAdapter(
            cwd,
            native_session_id=stint.native_session_id,
            command=[sys.executable, str(FAKE_CLAUDE), mode],
        )
    raise AgentUnavailable(f"The {stint.agent} agent is not supported yet")


@pytest.fixture
def engine() -> Engine:
    # One shared in-memory database per test; StaticPool keeps it alive across connections.
    return make_engine("sqlite://", poolclass=StaticPool)


@pytest.fixture
def client(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    worktrees = tmp_path_factory.mktemp("worktrees")
    app = create_app(engine, fake_adapter_factory, token=TEST_TOKEN, worktrees_dir=worktrees)
    with make_client(app) as test_client:
        yield test_client
