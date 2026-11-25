# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex and others) working in this repository. `CLAUDE.md` files import the `AGENTS.md` beside them, so this file is the one source for both.

## Project

Multi-Agent Hub: an open-source (MIT) Windows desktop app that runs Claude Code, Codex and a local Ollama model as parallel agent sessions on different projects, with automatic handoff to the next agent when one hits a usage limit. Anyone can run it with their own CLI logins.

- `backend/`: FastAPI on SQLite (SQLModel, Alembic), the agent adapters, the orchestrator, and `hub-doc` (`app/doctool/`, a separate document program).
- `frontend/`: React and TypeScript UI (Vite); `frontend/src-tauri/` is the Rust desktop shell and installer.
- `scripts/`: version and third-party-notice tools. `.github/workflows/`: CI and releases.
- `docs/architecture/`: how each part works, one file per topic (index in `docs/architecture/README.md`).

## Read before you work

- **Before working in `backend/` or `frontend/`, read that folder's `AGENTS.md`.** It has the commands, local rules and a table saying which `docs/architecture/` file to read for each kind of change. Read that file before changing the part it covers.
- `docs/plan.md` (the confirmed plan: phases and revisions), `docs/progress.md` (per-session recaps and the current next steps; read it first to see where work stopped, and append a new recap at the end of each session) and `docs/TODO.md` (manual checks) are private notes. They exist only in the maintainer's main checkout, not in worktrees or public clones; if they're missing, carry on without them.
- Revisions between phases are added to `docs/plan.md` after the last completed phase, not by rewriting phases.

## Rules that always apply

- **Login tokens:** Claude and Codex are driven through their official CLIs under the user's own logins; the app must never read, store or send login tokens (for Codex: never `auth.json`, `identity` or `account/read`). The local model needs no sign-in.
- **Database:** every schema change needs an Alembic migration (there is no `create_all`). **Never batch-rebuild `sessions` or `stints`:** with foreign keys on, dropping the old table cascades and deletes history. Use plain `ALTER TABLE ADD/DROP COLUMN`. Details in `docs/architecture/database.md`.
- **Local only:** `HUB_HOST` and `HUB_OLLAMA_URL` must be loopback, and every `/api` router stays behind the bearer token (`require_caller`).
- **Folders agents run in:** worktrees and chat folders must stay outside any repository, because Claude Code loads every `CLAUDE.md` above its cwd. Worktrees are not a sandbox.
- **Agent sandboxes:** Codex gets write access to a session's `outputs` folder only, never the session folder, and stored attachments stay read-only. Nothing the hub imports (skills, archives) is ever run.
- **Tests never use real accounts:** the normal suite uses fake Claude, Codex and Ollama; live tests are opt-in (`HUB_LIVE_*`) and CI never has credentials.

## Branches, CI and pull requests

- Feature branches start from `staging` and merge into `staging` through pull requests, never into `main` (see `RELEASING.md`). CI (`.github/workflows/ci.yml`: Windows backend tests, UI typecheck and build, version check) must pass, and a passing push to `staging` fast-forwards `main` to it. Releases are tags `vX.Y.Z` on `main` (`.github/workflows/release.yml`).
- Add user-visible changes under Unreleased in `CHANGELOG.md`.
- Commits: concise, descriptive subjects; history uses prefixes such as `progress:`, `plan:` and revision labels, without enforcing Conventional Commits. PRs describe behavior changes and how they were checked, with screenshots for UI changes.
- Add regression tests for behavior changes. Configure through `HUB_*` variables or `backend/.env`; keep secrets uncommitted.

## Keeping these files current

When a change alters how a part works, update its `docs/architecture/` file in the same PR. Keep this file and the folder `AGENTS.md` files short: they load into every session, so add only rules that apply to nearly every task there, and put explanations in the topic files.
