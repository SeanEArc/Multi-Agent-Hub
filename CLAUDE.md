# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Multi-Agent Hub: a personal Windows desktop app that runs Claude Code, Codex and a local Ollama model as parallel agent sessions on different projects, with automatic handoff to the next agent when one hits a usage limit. `plan.md` is the confirmed plan (requirements, tradeoffs, build phases 1–6). `progress.md` holds per-session recaps and the current next steps; read it first to see where work stopped, and append a new recap at the end of each session.

Only `backend/` exists so far (Phase 1, first half). The React + TypeScript UI, Codex/Ollama adapters, handoff, and git worktrees are not built yet. Agents are driven through their official CLIs under the user's own logins; the app must never read or store login tokens.

## Commands

All from `backend/`, using `uv` (Python 3.14 pinned in `.python-version`, exact versions in `uv.lock`; commit `uv.lock`):

```powershell
uv sync                                          # install
uv run python -m app.main                        # run API on http://127.0.0.1:8765 (docs at /docs)
$env:HUB_RELOAD="true"; uv run python -m app.main   # dev auto-reload
uv run pytest                                    # all tests
uv run pytest tests/test_claude_adapter.py::test_name   # single test
$env:HUB_LIVE_CLAUDE="1"; uv run pytest tests/test_claude_live.py  # opt-in, uses real claude CLI + subscription usage
$env:HUB_LIVE_CODEX="1"; uv run pytest tests/test_codex_live.py    # opt-in, uses real codex CLI + ChatGPT Codex allowance
uv run alembic revision --autogenerate -m "describe change"  # after editing app/models.py; review the file
uv add <pkg> / uv add --dev <pkg>
```

Settings are `HUB_*` env vars or `backend/.env` (`HUB_DATABASE_URL`, `HUB_HOST`, `HUB_PORT`, `HUB_RELOAD`, `HUB_LOG_LEVEL`, `HUB_CORS_ORIGINS`). The default DB is `backend/data/hub.db`.

## Architecture

- **App factory** (`app/main.py`): `create_app(engine=None)` builds the app; tests pass an in-memory `StaticPool` SQLite engine. Lifespan runs `init_db`, which applies Alembic migrations to `head` on every start, so there is no `create_all`. Schema changes always need a migration.
- **DB** (`app/db.py`, `app/models.py`): SQLModel on SQLite with foreign keys, WAL and busy_timeout set per connection. The `sessions` table (`AgentSession`) and `events` table (`SessionEvent`, `ON DELETE CASCADE`). Enums are stored by **value** as plain text with no CHECK constraint (`enum_column`), so adding a member needs no migration. Migrations use `render_as_batch=True` for SQLite. `migrations/env.py` uses the connection passed in by `init_db` when there is one, and otherwise the settings URL (CLI).
- **Adapter interface** (`app/adapters/base.py`): one `AgentAdapter` instance per session, constructed with its cwd and model. `start_task` and `send_reply` return async iterators of normalized `AgentEvent`s (`EventType`). A usage limit is a `LIMIT_REACHED` **event**, not an exception, and handoff will key off it. The orchestrator, UI and history should depend only on this interface.
- **Claude adapter** (`app/adapters/claude.py`): one `claude -p --output-format stream-json --verbose` process per turn, with the prompt sent over **stdin** (this avoids Windows quoting). Replies use `--resume <session id>`. `--permission-prompts none` means unapproved tools are denied rather than left hanging. AskUserQuestion is unavailable headless, so an appended system prompt makes Claude end question turns with `[[NEEDS_INPUT]]`, which `TurnParser` turns into a `QUESTION` event. `rate_limit_event` with status `rejected` is the main limit signal; regex on the result text is the fallback. `stop()` kills the whole process tree (`taskkill /T`). It refuses `.cmd`/`.bat` shims and requires the native `claude.exe`. Behavior was verified against Claude Code 2.1.305.
- **Shared CLI plumbing** (`app/adapters/cli.py`): `CliAdapter` runs one process per turn, sends the prompt over stdin, streams parsed lines, adds an error if the CLI exits before reporting the end of the turn, and `stop()` kills the process tree. It also holds `NEEDS_INPUT_MARKER` / `HEADLESS_PROMPT` and the headless git env. Both adapters subclass it.
- **Codex adapter** (`app/adapters/codex.py`): `codex exec --json -` per turn, `codex exec resume --json <thread id> -` for replies. Codex chooses the thread id and reports it in `thread.started`, so the adapter learns it from the stream and the orchestrator saves it from the `STARTED` event (Claude's id is chosen up front). Sandbox, `approval_policy="never"` and the marker instruction (`developer_instructions`) are passed as `-c` overrides, because `resume` has no `--sandbox`. `find_codex` runs the native `codex.exe` inside the npm package, never the `.cmd`/`.ps1` launchers. Exec mode never waits for approval: blocked actions fail as ordinary command errors. The stream has no rate-limit events, so after each turn `read_rate_limits` takes the plan's usage windows from the latest `token_count` in Codex's session file (`$CODEX_HOME/sessions/.../rollout-*-<thread id>.jsonl`, an undocumented format, read best effort) and emits the same `RATE_LIMIT` shape as Claude. A limit is detected from the error text. Codex reports no price, so the UI shows tokens. Verified against codex-cli 0.164.0. Settings: `HUB_CODEX_SANDBOX` (default `workspace-write`, which also allows `%TEMP%`) and `HUB_CODEX_PATH`.
- **Auth** (`app/auth.py`): every `/api` router is included with `Depends(require_caller)`, which checks `Authorization: Bearer` against the per-install token (`backend/data/token`, created in the lifespan, ACL'd to the current user) with `secrets.compare_digest` and returns a `Caller`. `HostOriginMiddleware` (outermost) rejects non-loopback `Host`, unknown `Origin`, and `Sec-Fetch-Site: cross-site`. `HUB_HOST` must be loopback. The Vite proxy injects the token, so the browser never holds it and `EventSource` still works. Tests use `make_client` in `conftest.py` (loopback `base_url` plus `TEST_TOKEN`); `create_app(..., token=)` skips the file.
- **Tests**: `tests/fake_claude.py` and `tests/fake_codex.py` stand in for the CLIs (modes `ok`, `question`, `crash`, `hang`; Codex also `tools`, `limit`) and are injected via `command=[sys.executable, FAKE_..., mode]`. `conftest.fake_adapter_factory` picks the fake by the session's agent and the mode from its title. Live tests are opt-in: `HUB_LIVE_CLAUDE=1` / `HUB_LIVE_CODEX=1`.
- **API** (`app/api/`): routes under `/api`: health, plus session CRUD and `GET /sessions/{id}/events?after_id=N` for incremental polling. The adapter is **not yet wired to the API**. Sending prompts and replies, persisting events, status transitions and live streaming are the next step (see `progress.md`). CORS allows the Vite dev server (port 5173) because the UI will run in a browser first.
