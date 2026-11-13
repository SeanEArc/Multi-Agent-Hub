import os
import sys
import tempfile
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
from app.adapters.ollama import OllamaAdapter
from app.db import make_engine
from app.main import create_app
from app.models import AgentKind, AgentSession, Stint
from app.orchestrator import AgentUnavailable

import fake_ollama

# Never the user's real skill library, even for apps a test builds without passing skills_dir.
os.environ["HUB_SKILLS_DIR"] = tempfile.mkdtemp(prefix="hub-test-skills-")

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
            effort=stint.effort,
        )
    if stint.agent is AgentKind.CLAUDE:
        return ClaudeAdapter(
            cwd,
            native_session_id=stint.native_session_id,
            command=[sys.executable, str(FAKE_CLAUDE), mode],
            effort=stint.effort,
        )
    # The local model is off unless the title gives it a mode ("ollama:ok"), so chains that end in it
    # behave as when Ollama isn't running.
    if stint.agent is AgentKind.OLLAMA and f"ollama:{mode}" in session.title and mode != "down":
        return OllamaAdapter(
            cwd,
            model=stint.model or fake_ollama.MODEL,
            native_session_id=stint.native_session_id,
            history_dir=Path(session.project_path) / "local-history",
            transport=fake_ollama.FakeOllama(mode).transport,
        )
    raise AgentUnavailable(f"The {stint.agent} agent is not available")


@pytest.fixture
def engine() -> Engine:
    # One shared in-memory database per test; StaticPool keeps it alive across connections.
    return make_engine("sqlite://", poolclass=StaticPool)


@pytest.fixture
def client(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    worktrees = tmp_path_factory.mktemp("worktrees")
    chats = tmp_path_factory.mktemp("chats")
    codex_home = tmp_path_factory.mktemp("codex-home")  # never the real ~/.codex
    app = create_app(engine, fake_adapter_factory, token=TEST_TOKEN, worktrees_dir=worktrees, chats_dir=chats,
                     codex_home=codex_home, skills_dir=tmp_path_factory.mktemp("skills"),
                     claude_home=tmp_path_factory.mktemp("claude-home"), discover_native=False)
    with make_client(app) as test_client:
        yield test_client
