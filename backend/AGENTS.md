# backend/AGENTS.md

The hub's API, agent adapters, orchestrator and `hub-doc`. The root `AGENTS.md` rules apply here too.

## Commands

All from `backend/`, using `uv` (Python 3.14 pinned in `.python-version`, exact versions in `uv.lock`; commit `uv.lock`):

```powershell
uv sync                                          # install
uv run python -m app.main                        # run API on http://127.0.0.1:8765 (docs at /docs)
$env:HUB_RELOAD="true"; uv run python -m app.main   # dev auto-reload
$env:HUB_SIMULATE_LIMIT="claude"; uv run python -m app.main   # dev: Claude's turns end in a fake usage limit (try handoff)
uv run pytest                                    # all tests
uv run pytest tests/adapters/test_claude_adapter.py::test_name   # single test
uv run alembic revision --autogenerate -m "describe change"  # after editing app/models.py; review the file
uv add <pkg> / uv add --dev <pkg>
uv run pyinstaller hub-backend.spec --noconfirm  # bundle for the desktop app -> dist/hub-backend/ (hub-backend.exe, hub-doc.exe)
uv run python scripts/snapshot_release_db.py 0.2.0  # at release: sample DB dump for the upgrade tests
uv run python ../scripts/version.py check        # every file has the same version (also: set X, notes X)
uv run python ../scripts/notices.py generate     # rewrite THIRD-PARTY-NOTICES.md and licenses/ (needs npm ci and cargo); `check` at release
```

Live tests (real CLIs, your own accounts) are opt-in; their commands are in `docs/architecture/testing.md`. Settings are `HUB_*` env vars or `backend/.env` (full list in `docs/architecture/settings.md`); all app data lives in `%LOCALAPPDATA%\MultiAgentHub`.

## Rules for backend changes

- **Migrations:** `init_db` applies them to `head` on every start. After autogenerate, review the file: drop the false `events.stint_id` FK change, and never batch-rebuild `sessions` or `stints` (`test_release_upgrades.py` fails on it). Enums are stored by value, so a new member needs no migration.
- **Adapters:** the orchestrator, UI and history depend only on the `AgentAdapter` interface (`app/adapters/base.py`). A usage limit is a `LIMIT_REACHED` event, never an exception. Every CLI launch goes through a `Runner`. Prompts go over stdin, and the native `claude.exe`/`codex.exe` are used, never `.cmd`/`.ps1` shims.
- **Tests:** `create_app(...)` takes injectable folders, engines and fakes; use them so tests never touch the user's real data, `~/.codex`, skills or Ollama (point `ollama_url` at a closed port when using the real factory). The `engine` fixture is a temp database file per test, never a shared in-memory connection. Test files never import each other: shared helpers go in `tests/support/`, shared fixtures in `tests/conftest.py`.
- **Dependencies:** no pandas or numpy (they break the 512 MB extract worker; `test_pandas_isnt_shipped`). A new runtime package must be in the third-party notices (`tests/core/test_notices.py`).
- **Style:** four-space indentation, double-quoted strings, `snake_case`; follow the surrounding code. No formatter or linter is configured. Name tests `test_*.py` / `test_*`.

## Where to read before changing something

Read the matching file in `docs/architecture/` before changing that part:

| If you're changing… | Read |
|---|---|
| Models, migrations, settings stored in the DB | `database.md` |
| The adapter interface, runner, Claude or Codex adapters, diffs | `adapters.md` |
| The Ollama adapter, its tools, loop or context fitting | `local-model.md` |
| Model pickers, availability or reasoning effort | `models.md` |
| The skills library or how skills reach each agent | `skills.md` |
| Slash commands for any agent, `/compact`, `/context`, `/code-review` | `commands.md` |
| The orchestrator, turns, handoff, worktrees, chats, summaries | `sessions.md` |
| Uploads, text extraction, the extract worker | `attachments.md` |
| `hub-doc`, the outputs folder, Office automation | `documents.md` |
| App startup, API routes, auth and the token | `api-and-auth.md` |
| Fakes, fixtures, live tests | `testing.md` |
| `HUB_*` settings and data folders | `settings.md` |
| Third-party notices and licenses | `notices.md` |
| The PyInstaller bundle or how the desktop shell starts the backend | `desktop.md` |
