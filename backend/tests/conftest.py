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

# Shared helpers live in tests/support (on the path through `pythonpath` in pyproject.toml), so no test file imports
# another. Their asserts get pytest's detailed messages too; registered before the first import of them.
pytest.register_assert_rewrite("support")

import shutil
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.config import get_settings
from app.db import make_engine
from app.main import create_app
from app.orchestrator import default_adapter_factory

from support.hub import TEST_TOKEN, fake_adapter_factory, make_client, opened_files, pr2_seams
from support.office import running_office
from support.paths import FAKE_CLAUDE, FAKE_CODEX, FAKE_OFFICE
from support.sessions import git
from support.wired import Launch, OllamaServer, SwapProgramRunner, WiredHub, optional_seams


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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit and a subfolder."""
    path = tmp_path / "project"
    (path / "src").mkdir(parents=True)
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("hello\n")
    (path / "src" / "app.py").write_text("print('hi')\n")
    git(path, "add", "--all")
    git(path, "commit", "--quiet", "--message", "first")
    return path


@pytest.fixture(scope="session")
def hub_doc_exe() -> str:
    """`hub-doc` as the agents get it: `HUB_DOC_EXE` (e.g. the bundled `hub-doc.exe` from PyInstaller), else the
    launcher `uv sync` makes from `[project.scripts] hub-doc = ...` in this venv's Scripts folder."""
    override = os.environ.get("HUB_DOC_EXE")
    if override:
        return override
    found = shutil.which("hub-doc", path=str(Path(sys.executable).parent))
    if not found:
        pytest.fail("hub-doc isn't installed: add `[project.scripts] hub-doc = \"...\"` to backend/pyproject.toml "
                    "and run `uv sync` (or set HUB_DOC_EXE to a built hub-doc.exe).")
    return found


@pytest.fixture
def office_fakes(tmp_path: Path) -> Iterator[Path]:
    """A folder with `WINWORD.EXE` and `EXCEL.EXE`: copies of Python's interpreter (with a `._pth` naming its
    standard library), so they are real processes with those image names that the hub's cleanup can find and kill."""
    if os.name != "nt":
        pytest.skip("Office automation is Windows-only")
    folder = tmp_path / "office"
    folder.mkdir()
    base = Path(getattr(sys, "_base_executable", sys.executable))
    prefix = Path(sys.base_prefix)
    for exe in ("WINWORD", "EXCEL"):
        shutil.copy2(base, folder / f"{exe}.EXE")
        (folder / f"{exe}._pth").write_text(f"{prefix / 'Lib'}\n{prefix / 'DLLs'}\n", encoding="utf-8")
    for dll in base.parent.glob("*.dll"):
        shutil.copy2(dll, folder / dll.name)
    real = running_office(folder)
    if real:
        pytest.skip(f"Microsoft Office is running on this PC ({real[0]}); not risking the user's documents")
    yield folder


@pytest.fixture
def wired(engine: Engine, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Iterator[WiredHub]:
    base = tmp_path_factory.mktemp("wired")
    attachments = base / "attachments"
    attachments.mkdir()
    # The production factory reads the settings: point every folder it uses at temp folders.
    settings = get_settings()
    monkeypatch.setattr(settings, "attachments_dir", attachments)
    monkeypatch.setattr(settings, "skills_dir", base / "skills")
    monkeypatch.setattr(settings, "local_dir", base / "local")
    monkeypatch.setattr(settings, "ollama_model", None)
    monkeypatch.setenv("CODEX_HOME", str(base / "codex-home"))  # never the real ~/.codex
    # find_claude/find_codex only need a native-looking program to exist; the runner swaps it for the fake.
    programs = base / "programs"
    programs.mkdir()
    (programs / "claude.exe").write_bytes(b"")
    (programs / "codex.exe").write_bytes(b"")
    monkeypatch.setenv("PATH", f"{programs}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(settings, "codex_path", str(programs / "codex.exe"))
    ollama = OllamaServer()
    monkeypatch.setattr(settings, "ollama_url", ollama.url)

    launches: list[Launch] = []
    hub: WiredHub

    def factory(*args: Any, **kwargs: Any):
        adapter = default_adapter_factory(*args, **kwargs)
        kind = getattr(adapter, "kind", None)
        name = getattr(kind, "value", "")
        if name in ("claude", "codex"):
            adapter.runner = SwapProgramRunner(FAKE_CLAUDE if name == "claude" else FAKE_CODEX, hub.modes[name],
                                               attachments, launches)
        return adapter

    app = create_app(engine, factory, token=TEST_TOKEN, worktrees_dir=base / "worktrees", chats_dir=base / "chats",
                     codex_home=base / "codex-home", skills_dir=base / "skills", claude_home=base / "claude-home",
                     discover_native=False, attachments_dir=attachments, open_file=opened_files.append_call,
                     ollama_url=ollama.url,
                     **optional_seams(tools_bin=base / "bin",
                                      office_command=[sys.executable, str(FAKE_OFFICE), "missing"]))
    try:
        with make_client(app) as client:
            hub = WiredHub(client, attachments, launches, ollama)
            yield hub
    finally:
        ollama.close()
