import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app.adapters.claude import ClaudeAdapter
from app.db import make_engine
from app.main import create_app
from app.models import AgentSession

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
TEST_TOKEN = "test-token"
# The Host check only accepts loopback names, so tests can't use TestClient's "testserver".
BASE_URL = "http://127.0.0.1:8765"


def make_client(app: FastAPI, token: str | None = TEST_TOKEN) -> TestClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, base_url=BASE_URL, headers=headers)


def fake_adapter_factory(session: AgentSession) -> ClaudeAdapter:
    """Claude adapter driving tests/fake_claude.py; the session title picks its mode (ok, question, hang...)."""
    return ClaudeAdapter(
        Path(session.project_path),
        native_session_id=session.native_session_id,
        command=[sys.executable, str(FAKE_CLAUDE), session.title],
    )


@pytest.fixture
def engine() -> Engine:
    # One shared in-memory database per test; StaticPool keeps it alive across connections.
    return make_engine("sqlite://", poolclass=StaticPool)


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    with make_client(create_app(engine, fake_adapter_factory, token=TEST_TOKEN)) as test_client:
        yield test_client
