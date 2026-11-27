"""Folders and fake programs the tests use, found from this file so they don't depend on where a test sits."""

from pathlib import Path

SUPPORT = Path(__file__).resolve().parent
TESTS = SUPPORT.parent
BACKEND = TESTS.parent
ROOT = BACKEND.parent  # the repository

# Stand-ins for the CLIs and the Office check script, run by path (`[sys.executable, FAKE_..., mode]`).
FAKE_CLAUDE = SUPPORT / "fake_claude.py"
FAKE_CODEX = SUPPORT / "fake_codex.py"
FAKE_OFFICE = SUPPORT / "fake_office.py"

# Released schemas' dumps for the upgrade tests; the release workflow writes here (`.github/workflows/release.yml`).
RELEASE_DB_DIR = TESTS / "release_dbs"
