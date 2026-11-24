# Repository Guidelines

## Project Structure & Module Organization

Multi-Agent Hub is a Windows desktop application using FastAPI, React/TypeScript, and Tauri.
- `backend/app/`: API routes (`api/`), agent integrations (`adapters/`), orchestration, SQLModel models, and Alembic migrations.
- `backend/tests/`: pytest tests, fake agent processes, and released database fixtures.
- `frontend/src/`: React components, API helpers, streaming hooks, and CSS theme tokens.
- `frontend/src-tauri/`: Rust desktop shell, installer configuration, and icon assets.
- `docs/`: plans, progress recaps, and manual checks. Consult `CLAUDE.md` for architecture details.

## Build, Test, and Development Commands

Use Windows 10/11, Node.js 22, and `uv` (Python 3.14 is pinned). Desktop builds also require stable Rust.

From `backend/`:
- `uv sync --locked`: install locked dependencies.
- `uv run python -m app.main`: start the API on port 8765.
- `uv run pytest`: run backend tests.
- `uv run python ../scripts/version.py check`: verify consistent project versions.

From `frontend/`:
- `npm ci`: install locked dependencies.
- `npm run dev`: start Vite; run the backend separately.
- `npm run typecheck` and `npm run build`: check TypeScript and build the UI.
- `npm run tauri dev`: launch the desktop app with its backend.
- `npm run tauri build`: package the Windows installer.

## Coding Style & Naming Conventions

Follow surrounding code: four-space Python indentation, two-space TypeScript/TSX indentation, double-quoted strings, and TypeScript semicolons. Use Python `snake_case`, React component `PascalCase` filenames, and `camelCase` helpers/hooks. Keep TypeScript strict checks passing. No dedicated formatter or linter is configured. Reuse CSS theme tokens.

## Testing Guidelines

Name pytest files `test_*.py` and functions `test_*`. Add regression tests for behavior changes; use fake adapters and isolated databases. No numeric coverage threshold is configured. Live tests require explicit `HUB_LIVE_CLAUDE`, `HUB_LIVE_CODEX`, or `HUB_LIVE_OLLAMA` opt-in. Validate UI changes with typecheck/build and relevant manual checks in `docs/TODO.md`.

## Commit & Pull Request Guidelines

Use concise, descriptive subjects; history uses prefixes such as `progress:`, `plan:`, and revision labels, without enforcing Conventional Commits. Branch from and target `staging`. Describe behavior changes and validation; include screenshots for UI changes. CI must pass. Add user-visible changes to `CHANGELOG.md` under Unreleased.

## Security & Database Changes

Never read, store, or transmit CLI login tokens. Configure through `HUB_*` variables or `backend/.env`; keep secrets uncommitted. Schema changes require reviewed, additive Alembic migrations and release-upgrade tests. Never batch-rebuild `sessions` or `stints`, which can cascade-delete history.
