# Testing

Paths like `app/...` and `tests/...` are inside `backend/`.

## Live tests

Opt-in, from `backend/`. They use real CLIs and your own accounts; CI never has credentials.

```powershell
$env:HUB_LIVE_CLAUDE="1"; uv run pytest tests/live/test_claude_live.py  # opt-in, uses real claude CLI + subscription usage
$env:HUB_LIVE_CODEX="1"; uv run pytest tests/live/test_codex_live.py    # opt-in, uses real codex CLI + ChatGPT Codex allowance
$env:HUB_LIVE_OLLAMA="1"; uv run pytest tests/live/test_ollama_live.py  # opt-in, uses the local Ollama server (free)
$env:HUB_LIVE_OFFICE="1"; uv run pytest tests/live/test_office_live.py  # opt-in, real Microsoft Office (close Word/Excel first)
```

## Tests

**Layout** (revision 10 step 3): test files are grouped by area in `tests/adapters/`, `tests/sessions/`, `tests/attachments/`, `tests/documents/`, `tests/core/` (API, auth, database, server, notices, release upgrades) and `tests/live/` (opt-in, above); `tests/release_dbs/` holds the released schemas' dumps (the release workflow writes there). **No test file imports another.** Shared helpers live in the `support` package (`tests/support/`): `hub` (`TEST_TOKEN`, `make_client`, `fake_adapter_factory`, `opened_files`), `sessions` (`create`, `wait_until_idle`, `events_of`, `prompts_sent`, `git`), `attachments`, `documents` (`HubDoc`), `office`, `ollama`, `wired` (the real wiring with only the CLIs swapped), `paths` (`BACKEND`, `ROOT`, `FAKE_CLAUDE`, `FAKE_CODEX`, `FAKE_OFFICE`, `RELEASE_DB_DIR`; never `Path(__file__)` in a test), and the fakes and samples. Shared fixtures (`engine`, `client`, `repo`, `hub_doc_exe`, `office_fakes`, `wired`) are in `tests/conftest.py`. pytest runs in `importlib` mode with `pythonpath = ["tests"]` (`backend/pyproject.toml`), so test files are never importable and may share names across folders; `conftest.py` registers `support` for assertion rewriting before importing it.

**Tests**: `tests/support/fake_claude.py` and `tests/support/fake_codex.py` stand in for the CLIs (modes `ok`, `question`, `crash`, `hang`; Codex also `tools`, `limit`) and are injected via `command=[sys.executable, FAKE_..., mode]`. `support.hub.fake_adapter_factory(session, stint, cwd)` picks the fake by the stint's agent and the mode from the session title, either one mode (`ok`) or per agent (`claude:limit codex:ok`). The local model is unavailable (`AgentUnavailable`) unless the title gives it a mode (`ollama:ok`), so chains ending in it behave as when Ollama is off; then `tests/support/fake_ollama.py` (`FakeOllama(mode).transport`, an `httpx2.MockTransport`; modes `ok`, `hang`, `empty`, `missing`, `error`, `midstream`, `down`, `tools`; `FakeOllama(script=[...])` replays tool calls, picked by position since the last user message so a fresh instance per turn follows it; `.calls` records request bodies) stands in for the server. `create_app` takes `local_dir`, `ollama_url` and `ollama_transport` for tests. Tests that use the real factory must point `ollama_url` at a closed port, or they reach the user's real Ollama. Fake Claude also has a `limit` mode and echoes its cwd. The `engine` fixture is a temp database **file** per test, never a shared in-memory `StaticPool` connection (a request thread's rollback undid the orchestrator's uncommitted status write: the old flaky handoff tests). The `client` fixture puts worktrees in a temp folder; the `repo` fixture builds a real temp git repo. Live tests are opt-in: `HUB_LIVE_CLAUDE=1` / `HUB_LIVE_CODEX=1` / `HUB_LIVE_OLLAMA=1`.
