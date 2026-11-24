import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# Never the user's real attachments folder, even for code that reads the settings (set before the app is imported).
os.environ["HUB_ATTACHMENTS_DIR"] = tempfile.mkdtemp(prefix="hub-test-attachments-")
# Never the real app-data bin folder for hub-doc's launcher, and never the real Office check (hidden Word and Excel
# on the user's PC) for apps a test builds without office_command; the opt-in live Office tests do run it. Set, not
# defaulted: a developer's own HUB_OFFICE_CHECK=true or HUB_TOOLS_BIN in the shell must not reach the suite.
os.environ["HUB_TOOLS_BIN"] = tempfile.mkdtemp(prefix="hub-test-bin-")
# Office runs' lock and registry of started processes (app/doctool/office_registry.py): the suite's own, never the
# folder the user's real hub-doc runs share.
os.environ["HUB_OFFICE_REGISTRY"] = tempfile.mkdtemp(prefix="hub-test-office-")
if os.environ.get("HUB_LIVE_OFFICE") != "1":
    os.environ["HUB_OFFICE_CHECK"] = "false"

# Never the user's real skill library, even for apps a test builds without passing skills_dir. Set before the app is
# imported: `app.main` builds a module-level app on import, which caches the settings (`get_settings`).
os.environ["HUB_SKILLS_DIR"] = tempfile.mkdtemp(prefix="hub-test-skills-")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.adapters.base import AgentAdapter
from app.adapters.claude import ClaudeAdapter
from app.adapters.codex import CodexAdapter
from app.adapters.ollama import OllamaAdapter
from app.db import make_engine
from app.main import create_app
from app.models import AgentKind, AgentSession, Stint
from app.orchestrator import AgentUnavailable

import fake_ollama


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


class OpenedFiles(list):
    """Files the open route would have opened, as (path, reveal); nothing is really opened in tests."""

    def append_call(self, path: Path, reveal: bool) -> None:
        self.append((path, reveal))


opened_files = OpenedFiles()


FAKE_OFFICE = Path(__file__).parent / "fake_office.py"


def pr2_seams(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Revision 4 PR 2 seams, passed only once `create_app` has them: the test suite must never start the real
    Office check (Word and Excel on the user's PC) or copy hub-doc into the real app-data folder."""
    import inspect

    accepted = inspect.signature(create_app).parameters
    seams = {"office_command": [sys.executable, str(FAKE_OFFICE), "missing"],
             "tools_bin": tmp_path_factory.mktemp("bin")}
    return {name: value for name, value in seams.items() if name in accepted}


def _bin_state(folder: Path) -> dict[str, int] | None:
    try:
        return {p.name: p.stat().st_mtime_ns for p in folder.iterdir()}
    except OSError:
        return None


@pytest.fixture(scope="session", autouse=True)
def never_real_app_data_or_office() -> Iterator[None]:
    """Guard for the whole run (revision 4 PR 2): no test may start the real Office check script (hidden Word and
    Excel on the developer's PC) unless HUB_LIVE_OFFICE=1, and none may copy hub-doc into the real app-data bin
    folder. The whole app-data folder isn't watched: the developer's own desktop app may be running and writing its
    log and database meanwhile. A blocked Office run answers "not found" and fails the run at the end."""
    from app.config import APP_DATA_DIR, get_settings
    from app.doctool import office

    settings = get_settings()
    for name in ("attachments_dir", "skills_dir", "tools_bin"):
        assert not Path(getattr(settings, name)).resolve().is_relative_to(APP_DATA_DIR.resolve()), name
    live = os.environ.get("HUB_LIVE_OFFICE") == "1"
    assert live or settings.office_check is False
    real_bin = APP_DATA_DIR / "bin"
    before = _bin_state(real_bin)
    blocked: list[list[str]] = []
    original_run = office.OfficeRun.run

    def guarded_run(self: office.OfficeRun) -> dict:
        if not live and any(str(office.SCRIPT) in str(part) for part in self.command):
            blocked.append(list(self.command))
            return {"ok": False, "error": "blocked by the test suite's guard"}
        return original_run(self)

    office.OfficeRun.run = guarded_run
    try:
        yield
    finally:
        office.OfficeRun.run = original_run
    assert not blocked, f"a test started the real Office check script: {blocked[0]}"
    assert _bin_state(real_bin) == before, f"a test changed the real {real_bin}"


@pytest.fixture
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    # A database file per test, so each thread gets its own connection, as in the app. A shared in-memory
    # connection (StaticPool) let a request thread's rollback undo the orchestrator's uncommitted write of a
    # turn's final status, leaving sessions "working" forever: the flaky handoff tests. Kept outside
    # tmp_path, which tests use as a project folder.
    db_file = tmp_path_factory.mktemp("db") / "hub.db"
    engine = make_engine(f"sqlite:///{db_file.as_posix()}")
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    worktrees = tmp_path_factory.mktemp("worktrees")
    chats = tmp_path_factory.mktemp("chats")
    codex_home = tmp_path_factory.mktemp("codex-home")  # never the real ~/.codex
    app = create_app(engine, fake_adapter_factory, token=TEST_TOKEN, worktrees_dir=worktrees, chats_dir=chats,
                     codex_home=codex_home, skills_dir=tmp_path_factory.mktemp("skills"),
                     claude_home=tmp_path_factory.mktemp("claude-home"), discover_native=False,
                     attachments_dir=tmp_path_factory.mktemp("attachments"), open_file=opened_files.append_call,
                     **pr2_seams(tmp_path_factory))
    with make_client(app) as test_client:
        yield test_client
