# Contributing

Thanks for helping. Multi-Agent Hub is a Windows desktop app; you need Windows 10 or 11 to run it.

## Setup

- Python via [uv](https://docs.astral.sh/uv/) (the Python version is pinned in `backend/.python-version`;
  uv installs it).
- Node.js 22 for the UI, and Rust (stable) only if you build the desktop shell.
- The Claude Code CLI, the Codex CLI and/or Ollama, signed in with **your own** accounts, only to use
  the app or run the live tests. The normal test suite needs none of them.

```powershell
cd backend; uv sync; uv run pytest          # backend tests (fake Claude, Codex and Ollama)
cd frontend; npm ci; npm run typecheck; npm run build
cd frontend; npm run tauri dev              # the desktop app from source
```

`CLAUDE.md` describes the architecture in detail.

## Pull requests

- Branch off `staging` and open your pull request into **`staging`** (not `main`). CI runs the backend
  tests on Windows, the UI typecheck and build, and the version check; it must pass before merging.
  Passing commits on `staging` reach `main` automatically. See `RELEASING.md`.
- Add a line under **Unreleased** in `CHANGELOG.md` for anything a user would notice.
- Schema changes need an Alembic migration (`uv run alembic revision --autogenerate -m "..."`) that only
  adds; `tests/test_release_upgrades.py` checks that released databases still upgrade without losing rows.
- Never add code that reads, stores or sends the Claude or Codex login tokens. The app drives the official
  CLIs under the user's own sign-in and must stay that way.
- Live tests (`HUB_LIVE_CLAUDE=1`, `HUB_LIVE_CODEX=1`, `HUB_LIVE_OLLAMA=1`) use your own accounts and run
  only on your machine; CI never has credentials.
