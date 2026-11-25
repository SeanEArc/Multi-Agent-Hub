# App factory, API and auth

Paths like `app/...` and `tests/...` are inside `backend/`.

## App factory

**App factory** (`app/main.py`): `create_app(engine=None)` builds the app; tests pass an in-memory `StaticPool` SQLite engine. Lifespan runs `init_db`, which applies Alembic migrations to `head` on every start, so there is no `create_all`. Schema changes always need a migration.

## API

**API** (`app/api/`): routes under `/api`: health, folders, session CRUD (`PATCH /sessions/{id}` changes `title`, `limit_policy` and `chain`, only the keys sent, null meaning the Settings default, allowed mid-turn), `POST /sessions/{id}/messages`, `/stop`, `/handoff` (`{agent, model, note}`), `GET`/`POST /sessions/{id}/summary`, `GET /sessions/{id}/stints/{stint_id}` (with brief), `/events?after_id=N`, `/stream` (SSE), `GET/PUT /settings`, and `GET /usage` (each agent's latest usage windows from any session, since limits are per account). CORS allows the Vite dev server (port 5173) because the UI will run in a browser first.

## Auth

**Auth** (`app/auth.py`): every `/api` router is included with `Depends(require_caller)`, which checks `Authorization: Bearer` against the per-install token (`%LOCALAPPDATA%\MultiAgentHub\data\token`, created in the lifespan, ACL'd to the current user; `HUB_TOKEN` replaces it) with `secrets.compare_digest` and returns a `Caller`. `HostOriginMiddleware` (outermost) rejects non-loopback `Host`, unknown `Origin`, and `Sec-Fetch-Site: cross-site`. `HUB_HOST` must be loopback. In the browser the Vite proxy injects the token, so the page never holds it and `EventSource` still works. In the desktop app the UI holds it: `frontend/src/connection.ts` gets `{base_url, token}` from the shell's `hub_connection` command, `apiFetch` adds the header, and `openEventStream` reads SSE with `fetch` (reconnecting with `Last-Event-ID`); `http://tauri.localhost` is in the default `cors_origins`.
