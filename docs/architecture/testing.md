# Testing

Paths like `app/...` and `tests/...` are inside `backend/`.

## Live tests

Opt-in, from `backend/`. They use real CLIs and your own accounts; CI never has credentials.

```powershell
$env:HUB_LIVE_CLAUDE="1"; uv run pytest tests/test_claude_live.py  # opt-in, uses real claude CLI + subscription usage
$env:HUB_LIVE_CODEX="1"; uv run pytest tests/test_codex_live.py    # opt-in, uses real codex CLI + ChatGPT Codex allowance
$env:HUB_LIVE_OLLAMA="1"; uv run pytest tests/test_ollama_live.py  # opt-in, uses the local Ollama server (free)
$env:HUB_LIVE_OFFICE="1"; uv run pytest tests/test_office_live.py  # opt-in, real Microsoft Office (close Word/Excel first)
```

## Tests

**Tests**: `tests/fake_claude.py` and `tests/fake_codex.py` stand in for the CLIs (modes `ok`, `question`, `crash`, `hang`; Codex also `tools`, `limit`) and are injected via `command=[sys.executable, FAKE_..., mode]`. `conftest.fake_adapter_factory(session, stint, cwd)` picks the fake by the stint's agent and the mode from the session title, either one mode (`ok`) or per agent (`claude:limit codex:ok`). The local model is unavailable (`AgentUnavailable`) unless the title gives it a mode (`ollama:ok`), so chains ending in it behave as when Ollama is off; then `tests/fake_ollama.py` (`FakeOllama(mode).transport`, an `httpx2.MockTransport`; modes `ok`, `hang`, `empty`, `missing`, `error`, `midstream`, `down`, `tools`; `FakeOllama(script=[...])` replays tool calls, picked by position since the last user message so a fresh instance per turn follows it; `.calls` records request bodies) stands in for the server. `create_app` takes `local_dir`, `ollama_url` and `ollama_transport` for tests. Tests that use the real factory must point `ollama_url` at a closed port, or they reach the user's real Ollama. Fake Claude also has a `limit` mode and echoes its cwd. The `engine` fixture is a temp database **file** per test, never a shared in-memory `StaticPool` connection (a request thread's rollback undid the orchestrator's uncommitted status write: the old flaky handoff tests). The `client` fixture puts worktrees in a temp folder; `test_worktrees.py` builds a real temp git repo. Live tests are opt-in: `HUB_LIVE_CLAUDE=1` / `HUB_LIVE_CODEX=1` / `HUB_LIVE_OLLAMA=1`.
