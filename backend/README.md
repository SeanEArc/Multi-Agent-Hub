# Multi-Agent Hub backend

FastAPI backend: agent sessions, events and history (SQLite).

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.14 (pinned in `.python-version`)
and the exact dependency versions in `uv.lock`.

## Setup (Windows, PowerShell)

```powershell
cd backend
uv sync
```

## Run

```powershell
uv run python -m app.main                       # production-style
$env:HUB_RELOAD="true"; uv run python -m app.main  # dev: restart on code changes
```

API: http://127.0.0.1:8765/api, interactive docs: http://127.0.0.1:8765/docs.
The database is created at `backend/data/hub.db` and migrated to the latest schema on every start.

## Test

```powershell
uv run pytest
```

## Changing the database schema

1. Edit the models in `app/models.py`.
2. Generate a migration, then read it before committing:
   ```powershell
   uv run alembic revision --autogenerate -m "describe the change"
   ```
3. Restart the app; it applies the migration automatically.

## Dependencies

- Add one: `uv add <package>` (dev-only: `uv add --dev <package>`). This updates `uv.lock`.
- Upgrade all: `uv lock --upgrade`, then `uv sync` and run the tests.
- Commit `uv.lock`.

## Settings

Set as `HUB_*` environment variables or in `backend/.env`:
`HUB_DATABASE_URL`, `HUB_HOST`, `HUB_PORT`, `HUB_RELOAD`, `HUB_LOG_LEVEL`,
`HUB_CORS_ORIGINS`, `HUB_CLAUDE_PERMISSION_MODE`, `HUB_TOKEN_FILE`.

Optional: copy `.env.example` to `.env` and adjust the values. Shell variables take
precedence. `HUB_CORS_ORIGINS` must be a JSON array of URLs. Without a database
override, the app uses `backend/data/hub.db`. `HUB_HOST` must be a loopback address
(the app refuses to start otherwise). No API keys or CLI login tokens belong in this file.

## Authentication

Every `/api` route, including the event stream, needs `Authorization: Bearer <token>`.

- The token is created on first start in `backend/data/token` (or `HUB_TOKEN_FILE`), with
  access limited to your Windows account. Delete the file and restart to change it.
- The Vite dev proxy reads that file and adds the header, so the browser never holds it.
- Requests are also refused (403) if `Host` isn't a loopback name, if a browser `Origin`
  isn't the API itself or one of `HUB_CORS_ORIGINS`, or if they are cross-site.

To call the API by hand in PowerShell:

```powershell
$token = Get-Content backend\data\token
Invoke-RestMethod http://127.0.0.1:8765/api/sessions -Headers @{ Authorization = "Bearer $token" }
```

In `/docs`, use **Authorize** and paste the token.

`HUB_LIVE_CLAUDE=1` is a separate test-only shell variable that enables the live
Claude test and consumes subscription usage; it is not loaded from `.env` by the test.

## Layout

- `app/main.py`: app factory, CORS, routers
- `app/config.py`: settings
- `app/auth.py`: token file, bearer-token dependency, Host/Origin middleware
- `app/db.py`: SQLite engine (foreign keys, WAL), migrations at startup, request-scoped DB session
- `app/models.py`: sessions and events tables, status and agent enums
- `app/migrations/`: Alembic migrations
- `app/adapters/base.py`: the interface each agent adapter implements
- `app/api/`: HTTP routes
