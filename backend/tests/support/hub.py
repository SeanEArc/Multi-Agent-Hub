"""The test hub: the API client, fake adapters picked by the session title, and seams every test app gets."""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.base import AgentAdapter
from app.adapters.claude import ClaudeAdapter
from app.adapters.codex import CodexAdapter
from app.adapters.ollama import OllamaAdapter
from app.main import create_app
from app.models import AgentKind, AgentSession, Stint
from app.orchestrator import AgentUnavailable

from support import fake_ollama
from support.paths import FAKE_CLAUDE, FAKE_CODEX, FAKE_OFFICE


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
    """Adapter driving tests/support/fake_claude.py or fake_codex.py; the session title picks its mode (ok, question, hang...)."""
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


class OpenedFiles(list):
    """Files the open route would have opened, as (path, reveal); nothing is really opened in tests."""

    def append_call(self, path: Path, reveal: bool) -> None:
        self.append((path, reveal))


opened_files = OpenedFiles()


def pr2_seams(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Revision 4 PR 2 seams, passed only once `create_app` has them: the test suite must never start the real
    Office check (Word and Excel on the user's PC) or copy hub-doc into the real app-data folder."""
    import inspect

    accepted = inspect.signature(create_app).parameters
    seams = {"office_command": [sys.executable, str(FAKE_OFFICE), "missing"],
             "tools_bin": tmp_path_factory.mktemp("bin")}
    return {name: value for name, value in seams.items() if name in accepted}
