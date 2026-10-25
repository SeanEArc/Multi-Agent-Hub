2026-10-20 20:43 (UTC-05:00)

# Session 1: Built the FastAPI backend skeleton and a tested Claude adapter, the first half of Phase 1.

Plan: [plan.md](plan.md). Backend: [backend/](backend/) (see [backend/README.md](backend/README.md) for commands).

## What was done

1. **Reviewed `plan.md` for Phase 1.** Found gaps: headless tool permissions, the "reply to a question" fallback, schema design for multiple sessions, and a scratch working folder for tests. Recommended moving the Electron vs. Tauri decision out of Phase 1 (the UI can run in a browser against the local API for now). `plan.md` itself was not edited.
2. **Created the FastAPI backend.** App factory, settings (`HUB_*` env vars), SQLite `sessions` and `events` tables, session CRUD endpoints under `/api`, and the shared agent adapter interface.
3. **Production-readiness fixes.**
   - Moved to Python 3.14.8 and the `uv` package manager, with exact versions locked in `uv.lock`.
   - SQLite now enforces foreign keys, uses WAL mode and waits when busy. Deleting a session also deletes its events.
   - Alembic migrations run at startup. The first migration is `app/migrations/versions/20261020_139e95ea6763_initial_schema.py`.
   - Enums are stored by value (`ready`, `claude`).
   - Auto-reload is now a setting that defaults to off, logging is configured, and `updated_at` updates on every change.
4. **Checked how `claude -p` behaves** (Claude Code 2.1.305), settling open questions in the plan:
   - Output is one JSON event per line, ending with a `result` event.
   - `AskUserQuestion` is not available headless. Claude is told to end such messages with `[[NEEDS_INPUT]]`, which marks "waiting for input". This relies on Claude following the instruction, so it can occasionally be missed.
   - Replies work by starting a new process with `--resume <session id>`.
   - With `--permission-mode acceptEdits --permission-prompts none`, edits are allowed and anything else that needs approval is denied immediately and reported. Nothing hangs.
   - Every turn reports five-hour and seven-day usage, which gives early warning before a limit (useful for Phase 3).
5. **Built the Claude adapter** in `backend/app/adapters/claude.py`.
   - One `claude` process per turn, with the prompt sent over stdin.
   - Supports start, reply, stop (kills the whole process tree), and Claude's very long output lines.
   - A parser turns Claude's output into the shared event types.
   - Interface changes: adapters receive their folder and model when created, and the `LimitReached` exception became a `limit_reached` event.
6. **Tests:** 19 pass, plus an opt-in live test against the real CLI (`HUB_LIVE_CLAUDE=1`), which also passed.

---

2026-10-20 21:26 (UTC-05:00)

# Session 2: Connected Claude to the API and built the React UI, completing Phase 1 apart from the deferred desktop shell.

Frontend: [frontend/](frontend/) (see [frontend/README.md](frontend/README.md)).

## What was done

1. **Connected the adapter to the API** through a new orchestrator (`backend/app/orchestrator.py`).
   - `POST /api/sessions/{id}/messages` starts the task, or replies if it already started. The turn runs in the background.
   - `POST /api/sessions/{id}/stop` stops a running turn. `GET /api/sessions/{id}/stream` sends live updates (server-sent events), resuming from the last event seen after a reconnect.
   - Every event is saved to SQLite, including the user's own messages and Claude's raw JSON (new `raw` column, migration `20261020_eb23da6d5410`).
   - Status moves automatically: working, then waiting for input, completed, or failed. A stopped turn goes back to ready. Turns interrupted by a backend restart are marked failed on the next start.
   - Session creation now rejects a `project_path` that isn't an existing folder.
2. **Backend tests:** 29 pass. A real two-turn run over HTTP also worked: Claude asked for a color, the session showed waiting for input, the reply "blue" created `color.txt`, and the session completed with full history.
3. **The user tested the API by hand in PowerShell.** It worked (18 events saved). Two PowerShell 5.1 quirks came up:
   - `Invoke-RestMethod` passes JSON lists down the pipeline as one item, so wrap the call in parentheses before piping it.
   - It shows non-ASCII text such as `—` wrongly, but the stored data is correct.
4. **Built the React UI** in `frontend/` (React 19, TypeScript 7, Vite 8). It runs in the browser for now; there is no Tauri or Electron shell yet.
   - Sidebar: session list with live status dots, and a New session dialog (title, project folder, agent, model, optional first message).
   - Session view: the transcript in Claude Code style (your prompts, Claude's replies as Markdown, tool calls you can expand to see input and result, denied actions flagged, turn footers with cost, steps and time), a status badge, five-hour and seven-day usage bars, a Stop button and Delete.
   - Composer: Enter sends and Shift+Enter starts a new line. It's highlighted when Claude is waiting for your answer.
   - Dark theme by default, light when the system uses light. It works at phone width.
5. **Verified the Phase 1 acceptance test in headless Edge:** create a session, see the status and events, answer Claude's question, see the completed session, and reload with history intact. There were no console errors.
   - Fixed while testing: Vite now binds to `127.0.0.1`, because `localhost` bound IPv6 only. Added a favicon. Tool rows show paths relative to the project.
6. **Answered setup questions:**
   - The frontend uses TypeScript and React but not Tauri. The desktop shell is deferred to Phase 4, and wrapping the UI later won't require rewriting it.
   - How to run the app (below).

## How to run

Two terminals, backend first, each starting in the repo folder:

```powershell
# Terminal 1: backend (ready at "Uvicorn running on http://127.0.0.1:8765")
cd backend
uv run python -m app.main

# Terminal 2: frontend, then open http://localhost:5173
cd frontend
npm run dev
```

Stop either one with Ctrl+C. Backend tests: `uv run pytest` in `backend/`.

---

2026-10-20 21:40 (UTC-05:00)

# Session 3: Prepared the project for GitHub and published it as the first commit.

Repository: https://github.com/SeanEArc/Multi-Agent-Hub (branch `main`).

## What was done

1. **Added a root `.gitignore`** covering local environment files (`.env`, `.env.*`, with `*.example` files kept), Python and Node build output and caches, SQLite databases and their sidecar files (including `backend/data/`, which holds session history and prompts), logs, and editor or OS files.
2. **Added `backend/.env.example`** with safe defaults for every backend setting, including `HUB_CLAUDE_PERMISSION_MODE`. It notes that the app needs no API keys or login tokens, and that the live-test switch `HUB_LIVE_CLAUDE` belongs in the shell, not in `.env`.
3. **Updated the READMEs.** `frontend/README.md` now explains that `HUB_API_URL` is a shell variable read by the Vite dev proxy, not a frontend `.env` setting.
4. **Checked before publishing.** The ignore rules exclude environment files, the database, dependencies and build output, while `.env.example`, `uv.lock` and `package-lock.json` stay included. No credentials were found in the source.
5. **First commit** (`b896866`, 53 files) pushed to `origin/main`. No `.env` or database files are tracked.

---

2026-10-22 23:35 (UTC-05:00)

# Session 4: Reviewed what Phase 1 still needs, chose Tauri and Tailscale, let headless agents run git, and opened the `feature/authentication` branch.

This session ran inside Multi-Agent Hub itself (headless `claude -p`).

## What was done

1. **Reviewed the repo against Phase 1.** Commit `3c6a69a` (earlier on 2026-10-22) added minimal local authentication to Phase 1, so Phase 1 is no longer complete. Missing:
   - **Auth, not started:** no per-install token, no bearer-token dependency, no `Host`/`Origin` checks, and `HUB_HOST` can still be set to something other than loopback.
   - **Design issue:** the UI's `EventSource` (`frontend/src/useSessionStream.ts`) can't send an `Authorization` header. Putting the token in the URL would break the "never logged" rule, because uvicorn logs URLs. The recommended fix is a fetch-based stream reader that resumes with `after_id`.
2. **Updated `plan.md` (commit `3209d92`).**
   - Tauri is the desktop shell, replacing the Electron default. Building the shell and packaging the backend as its sidecar moved to Phase 4. Until then, the Vite dev proxy adds the token.
   - Tailscale (`tailscale serve` for HTTPS) is the Phase 6 remote-access method.
   - Added the costs: some Rust in the shell, and the Tauri window's origin (reportedly `http://tauri.localhost`, unverified) must pass the `Origin` check.
3. **Compared auth options for a single user:** loopback only, static bearer token, password plus cookie, passkey, device pairing, Tailscale identity headers, client certificates and OAuth.
   - Tailscale identity can't replace the token. Local requests don't go through `tailscale serve`, and any local process can fake the identity header.
   - Recommended: a static bearer token now, behind one FastAPI dependency that returns who is calling. Phase 6 adds Tailscale identity, passkeys and device pairing to that same dependency. **The user hasn't decided yet.**
4. **Found why tables showed as one run-on line in the UI.** `Transcript.tsx` uses `react-markdown` without `remark-gfm`, so tables, task lists and strikethrough aren't rendered. The fix (add `remark-gfm` and table CSS) was proposed but not made.
5. **Let headless agents run shell commands (commit `9c731f9`).** Before this, every command was denied (`--permission-prompts none`).
   - Compared five approaches: allowlist, skip all permissions, approve after a denial, live approval through `--permission-prompt-tool`, and a PreToolUse hook. Chose the allowlist for now.
   - The user created `.claude/settings.json` in the repo root. It allows read-only git plus `add`, `commit`, `switch` and `push`, and the `uv` and `npm` scripts. It denies force pushes, `reset --hard`, `clean`, `branch -D`, recursive deletes and `.env` files. Headless agents can't write this file themselves.
   - `ClaudeAdapter` now runs `claude` with `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never`, so a missing git login fails instead of hanging the turn. **Not tested yet** (see next steps).
6. **Pushed `main`** (`fcf04d2..9c731f9`). **Created and pushed `feature/authentication`**, which tracks `origin/feature/authentication` and is the current branch.

## Current state

- Phase 1: everything except minimal local authentication. The UI acceptance test passed in Session 2. The shell decision is made (Tauri), and building the shell is in Phase 4.
- Branch: `feature/authentication`, with no commits yet beyond `main`.
- Agents in this repo can run git and push. Settings changes take effect on the next message, but the `claude.py` change only takes effect after a backend restart.
- Known gaps in the allowlist:
  - The `uv run pytest`, `uv sync` and `npm run` rules only match when the command runs inside `backend/` or `frontend/`. Sessions run from the repo root, so these commands are still denied.
  - Deny rules only match how a command starts, so `git push origin main --force` gets through.
  - `git push` to `main` is allowed.
- Not done: Codex (Phase 2), handoff (Phase 3), the dashboard and worktrees (Phase 4), Ollama (Phase 5) and iPhone access (Phase 6).
- `CLAUDE.md` is out of date: it says only `backend/` exists and that the adapter isn't connected to the API. It also doesn't mention `.claude/settings.json`.

## Next steps

**For the user**
- [ ] In `.claude/settings.json`, replace the four `uv`/`npm` allow entries with `Bash(uv sync --directory backend:*)`, `Bash(uv run --directory backend pytest:*)`, `Bash(npm --prefix frontend run build:*)` and `Bash(npm --prefix frontend run typecheck:*)`. Then restart the backend.
- [ ] Decide on the auth approach (recommended: a static bearer token now, Tailscale identity and passkeys in Phase 6).
- [ ] Decide whether the stream should use a fetch-based reader (recommended) or a cookie.
- [ ] Make sure Git Credential Manager has your GitHub login saved (one manual `git push` does it).
- [ ] Decide whether to record the `claude -p` findings in `plan.md` (sections 4 and 7 still list them as unverified).
- [ ] Check Anthropic's own pages on subscription use through the CLI (still unverified in the plan).
- [x] Install and log in to the Codex CLI before Phase 2. Done 2026-10-24: `codex-cli 0.164.0` via `npm install -g @openai/codex` (in `%APPDATA%
pm`), and `codex login status` shows "Logged in using ChatGPT". In PowerShell, `codex` needs `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (or run `codex.cmd`); in Git Bash the interactive UI exits silently because mintty isn't a Windows console.
- [ ] Optional: remove the leftover Python 3.13 install if nothing else uses it.

**For the agent**
- [ ] Run the backend tests on the `claude.py` change once the allowlist fix is in.
- [ ] Build minimal local auth on `feature/authentication`: a per-install token file readable only by the user, a bearer dependency with constant-time comparison on every route and the stream, `Host` and `Origin` checks, loopback-only binding, token injection in the Vite proxy, a fetch-based stream reader in the UI, and tests.
- [ ] Add `remark-gfm` and table styles to the transcript, if the user wants it.
- [ ] Optional: a PreToolUse hook to block force pushes anywhere in a command and limit agent pushes to `feature/*` branches.
- [ ] Update `CLAUDE.md`: frontend, orchestrator, streaming endpoints, the permissions allowlist, and the Tauri/Tailscale decisions.
- [ ] Mark Phase 1 done in `plan.md` once auth is merged.
- [ ] Phase 2: the Codex adapter (`codex exec --json` plus resume), once the Codex CLI is ready.

---

2026-10-23 00:05 (UTC-05:00)

# Session 5: Added a folder picker to the New session dialog, so a project folder can be browsed to instead of pasted.

This session ran inside Multi-Agent Hub itself (headless `claude -p`).

## What was done

1. **Backend: `GET /api/folders?path=&show_hidden=`** (`backend/app/api/folders.py`, registered in `app/main.py`).
   - Lists subfolders only, never files, sorted by name. Dot folders, `$` folders and Windows hidden or system folders are left out unless `show_hidden` is set.
   - With no `path`, it returns the home folder and the drives as starting points. A drive root has no parent, so going Up from it returns to that top level.
   - A missing path or a file gives 404, and an unreadable folder gives 403.
   - Added 5 tests in `backend/tests/test_folders.py`.
2. **Why the backend lists folders instead of opening a native dialog.** A browser's folder picker only gives the page a folder's name, not its full path. A Windows dialog opened by the backend would appear on the host PC, so it wouldn't work from the phone over Tailscale (Phase 6). Listing through the API works in the browser now, in the Tauri window and remotely.
3. **Frontend: `FolderPicker`** (`frontend/src/components/FolderPicker.tsx`) replaces the plain path input in `NewSessionDialog.tsx`.
   - The path can still be typed or pasted.
   - **Browse…** opens a folder list inside the dialog at the typed path, or at the top level if that path doesn't exist. Clicking a folder opens it and makes it the selected path. **Up** goes back a level, **Hidden** shows hidden folders, and **Done** closes the list.
   - Up to 5 recent project folders from existing sessions appear as one-click chips (`App.tsx` passes them in).
   - Added `api.listFolders` in `frontend/src/api.ts` and the picker styles in `styles.css`.
4. **Checks:** `npm run typecheck` and `npm run build` pass. **The backend tests were not run.** `uv` isn't on the Bash tool's PATH, and the PowerShell `uv` calls needed an approval the headless session couldn't give.
5. **The user committed and pushed the work** as `74833c4` on `feature/authentication`. That commit also added `git merge --ff-only` and `git pull --ff-only` to the allowlist in `.claude/settings.json`.

---

2026-10-23 13:32 (UTC-05:00)

# Session 6: Resolved the merge conflict between `main` and `feature/authentication`, merged the folder picker into `main`, and ran the backend tests.

## What was done

1. **Merge conflict.** `main` (`7be4759`, the allowlist update) and `feature/authentication` (`74833c4`, the folder picker) both changed `.claude/settings.json` and `progress.md`.
   - Merged `main` into `feature/authentication` as `5414c63`, without a rebase.
   - Both files took `main`'s version, since it already held everything the branch added: the `--ff-only` git entries, the `uv --directory backend` and `npm --prefix frontend` entries, and the fuller Session 5 recap. `settings.json` still parses as JSON.
2. **Pushed `feature/authentication`, then fast-forwarded `main` to `5414c63` and pushed it.** The folder picker is now on `main`, and both branches point at the same commit.
3. **Backend tests: 34 passed, 1 skipped** (the opt-in live Claude test), including the 5 tests in `tests/test_folders.py` and the `claude.py` change from Session 4.
   - Plain `uv` wasn't found in the agent's PowerShell. Its WinGet folder (`%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_…`) is on the user `Path`, but the shell was started before that entry existed. The tests ran with `uv.exe`'s full path. A process started after the install (a new terminal, Claude Code, or the backend) will find `uv`.

## Current state

- `main` and `feature/authentication` are both at `5414c63` and pushed. Auth work can continue on `feature/authentication`.
- `/api/folders` is unauthenticated like every other route. It shows folder names on the machine to anything that can reach the API, so it needs the same token check when auth lands.
- The picker hasn't been tried in the browser yet.
- Phase 1 is done apart from minimal local auth.

## Next steps

**For the user**
- [ ] Restart Claude Code and the backend from a new terminal, so they and the headless sessions they spawn get a `Path` that includes `uv`.
- [ ] Decide on the auth approach (recommended: a static bearer token now, Tailscale identity and passkeys in Phase 6).
- [ ] Decide whether the stream should use a fetch-based reader (recommended) or a cookie.

**For the agent**
- [ ] Check the picker in the browser: Browse, Up, Hidden, the recent chips, and a typed path.
- [ ] Build minimal local auth on `feature/authentication`, covering `/api/folders` too: a per-install token file readable only by the user, a bearer dependency with constant-time comparison on every route and the stream, `Host` and `Origin` checks, loopback-only binding, token injection in the Vite proxy, a fetch-based stream reader in the UI, and tests.
- [ ] Add `remark-gfm` and table styles to the transcript, if the user wants it.
- [ ] Update `CLAUDE.md`: frontend, orchestrator, streaming and folder endpoints, the permissions allowlist, and the Tauri/Tailscale decisions.
- [ ] Mark Phase 1 done in `plan.md` once auth is merged.
- [ ] Phase 2: the Codex adapter (`codex exec --json` plus resume), once the Codex CLI is ready.

---

2026-10-23 21:08 (UTC-05:00)

# Session 7: Built minimal local authentication (static bearer token plus Host/Origin checks) on `feature/authentication`.

## What was done

1. **Switched to `feature/authentication`** (same commit as `main`, `5414c63`). The uncommitted Session 6 recap in this file came along.
2. **The user chose the static bearer token.**
3. **Backend: `backend/app/auth.py`.**
   - `load_or_create_token`: on first start, writes a random 32-byte token to `backend/data/token` (setting `HUB_TOKEN_FILE`). On Windows, `icacls` removes inherited access and grants only the current user. Later starts reuse the file. It is created in the lifespan, so importing `app.main` (tests) never touches it.
   - `require_caller`: a FastAPI dependency on every `/api` router (health, sessions including the stream, folders). It checks `Authorization: Bearer` with `secrets.compare_digest`, returns `Caller(method="token")`, and answers 401 with `WWW-Authenticate: Bearer` otherwise. `/docs` has an **Authorize** button.
   - `HostOriginMiddleware`, the outermost middleware, returns 403 when `Host` isn't a loopback name (stops DNS rebinding), when a browser `Origin` isn't the API itself or in `HUB_CORS_ORIGINS`, or when `Sec-Fetch-Site: cross-site` arrives without an Origin (no-cors GETs).
   - `HUB_HOST` must be a loopback address, or settings validation fails at startup.
4. **Frontend: `vite.config.ts`.** The `/api` proxy reads the token file on every request and sets the `Authorization` header, with `changeOrigin` so the backend sees its own Host.
5. **No fetch-based stream reader.** Because the proxy adds the header, the browser never holds the token, so `EventSource` keeps working unchanged. A fetch reader is only needed once the UI calls the backend directly (the Tauri window in Phase 4, or the phone in Phase 6). Revisit then.
6. **Tests: 65 passed, 1 skipped** (new `tests/test_auth.py`, and `conftest.make_client` gives every test a loopback `base_url` and the token). `npm run build` passes.
7. **End-to-end check** with a temporary backend (port 8799, scratch database and token) behind Vite (port 5174): direct without token 401; via proxy 200; evil Origin 403; cross-site 403; UI Origin 200. The token file's ACL lists only the current user, and the token doesn't appear in the backend log.
8. **Fixed overlapping folder rows in the picker** (reported by the user after restarting). The list is a column flexbox capped at 220px, and rows with `overflow: hidden` were allowed to shrink to fit it instead of scrolling. `.folder-item` is now `flex: none`, and the name sits in `.folder-item-name` so long names end in an ellipsis.
9. **Docs:** `backend/README.md` (Authentication section, PowerShell example), `frontend/README.md`, `backend/.env.example`, and an Auth bullet in `CLAUDE.md`.

## Current state

- Auth is built but **not committed**. Phase 1 is complete once it is committed and merged.
- Any script or tool that calls the API directly now needs the token header (see `backend/README.md`).
- The user checked it after restarting: a request without the token got 401, and the UI (session list, sending a message, the live stream) works through the proxy.
- The Tauri origin (`http://tauri.localhost`, unverified) will need adding to the allowed origins in Phase 4.

## Next steps

**For the user**
- [ ] Review and commit the auth work on `feature/authentication`, then merge to `main`.
- [ ] Restart Claude Code and the backend from a new terminal so `uv` is on `Path` (from Session 6).

**For the agent**
- [ ] Check the folder picker in the browser after the row fix: Browse, Up, Hidden, recent chips, typed path.
- [ ] Mark Phase 1 done in `plan.md` once auth is merged.
- [ ] Update the rest of `CLAUDE.md`: frontend, orchestrator, streaming and folder endpoints, the permissions allowlist, and the Tauri/Tailscale decisions.
- [ ] Add `remark-gfm` and table styles to the transcript, if the user wants it.
- [ ] Phase 2: the Codex adapter (`codex exec --json` plus resume), once the Codex CLI is ready.

---

2026-10-24 00:27 (UTC-05:00)

# Session 8: Committed the auth work, marked Phase 1 done, and merged `feature/authentication` into `main`.

## What was done

1. **Pre-commit check.** No secrets, API keys or real tokens in the code (`TEST_TOKEN` is a fake test value). Hosts and ports are overridable `HUB_*` defaults. `backend/data/token` and the database are ignored, and git history shows no `.env` or `backend/data/` file was ever committed.
2. **`.gitignore`:** added `.claude/settings.local.json` and `.claude/worktrees/`. The shared `.claude/settings.json` stays tracked.
3. **Removed the Windows username from `progress.md`.** The Session 2 "How to run" commands now use `cd backend` and `cd frontend` from the repo folder. (Older commits still contain the full path.)
4. **Committed and pushed `49276b4`** on `feature/authentication`: auth, the folder picker row fix, the ignore rules and the docs.
5. **The user checked the folder picker in the browser:** rows no longer overlap.
6. **Marked Phase 1 done in `plan.md`** (2026-10-24), noting that passing the token from Tauri to the sidecar belongs to Phase 4.
7. **Fast-forwarded `main` to `feature/authentication` and pushed both.**

## Current state

- **Phase 1 is complete.** `main` and `feature/authentication` point at the same commit.
- Any script or tool that calls the API directly needs `Authorization: Bearer <token>` (see `backend/README.md`).
- The Tauri origin (`http://tauri.localhost`, unverified) will need adding to the allowed origins in Phase 4. A fetch-based stream reader is only needed once the UI calls the backend directly (Phase 4 or 6).

## Next steps

**For the user**
- [ ] Install and log in to the Codex CLI before Phase 2.
- [ ] Restart Claude Code and the backend from a new terminal so `uv` is on `Path` (from Session 6), if not done yet.
- [ ] Decide whether to record the `claude -p` findings in `plan.md` (sections 4 and 7 still list them as unverified).

**For the agent**
- [ ] Update the rest of `CLAUDE.md`: frontend, orchestrator, streaming and folder endpoints, the permissions allowlist, and the Tauri/Tailscale decisions.
- [ ] Add `remark-gfm` and table styles to the transcript, if the user wants it.
- [ ] Phase 2: the Codex adapter (`codex exec --json` plus resume) on `feature/codex-adapter` (created from `main` at `51d3b4b`, no changes yet). First probe `codex exec` in a scratch folder (uses some of the user's Codex allowance; confirm with the user first): event format, resume, questions and approvals, limit reporting, the native `codex.exe` inside the npm package (the adapter must not run the `.cmd`/`.ps1` launchers), and the sandbox mode (likely workspace-write plus optional extra folders). Then the adapter, a fake `codex` for tests, an opt-in live test, wiring it into the orchestrator, and the billing check.

---

2026-10-25 14:32 (UTC-05:00)

# Session 9: Probed the Codex CLI live and built the Codex adapter (Phase 2) on `feature/codex-adapter`.

## What was done

1. **Offline checks.** The native binary is at `%APPDATA%\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe`. The npm launcher only adds update-hint env vars, so running the binary directly is safe. `exec` and `exec resume` read the prompt from stdin with `-`. `resume` has no `--sandbox` or `--cd`.
2. **Live probes (user approved, 5 short turns, codex-cli 0.164.0, default model).** Event format: `thread.started` (thread id), `turn.started`, `item.started|completed` (`agent_message`, `command_execution`, `file_change`...), `turn.completed` with token usage. Resume keeps the context and the thread id, and `-c sandbox_mode=...` works on resume. Codex follows the `[[NEEDS_INPUT]]` instruction. A write outside the workspace is **denied** by the `elevated` Windows sandbox and shows up only as a failed command (exit 1, "Access denied"), with no approval prompt or denied event. Exception: `workspace-write` also allows writes under `%TEMP%`, so the first try at that probe "escaped" because the scratch folder is in `%TEMP%`. Codex prints no rate-limit status; the limit wording ("You've hit your usage limit...") was taken from the binary, not seen live.
3. **`app/adapters/cli.py`:** moved the shared process loop (stdin prompt, streaming, crash detection, stop/kill tree) out of `claude.py` into `CliAdapter`. The Claude adapter now subclasses it, with no behavior change.
4. **`app/adapters/codex.py`:** `CodexAdapter` + `TurnParser` + `find_codex`. Commands are shown without the PowerShell wrapper (`Shell`), file changes as `Edit`, MCP calls as `mcp__server__tool`. Tool ids get a per-turn prefix because Codex restarts `item_0` every turn. A limit reported as both `error` and `turn.failed` produces one `LIMIT_REACHED`.
5. **Orchestrator:** builds the Codex adapter and saves the thread id from the `STARTED` event, so replies resume it, even after a backend restart. New settings `HUB_CODEX_SANDBOX` and `HUB_CODEX_PATH`.
6. **Tests:** `fake_codex.py`, `test_codex_adapter.py` (parser cases copied from the live probes, binary discovery, process handling), two orchestrator tests and an opt-in `test_codex_live.py`. **84 passed, 2 skipped** (the live tests).
7. **UI:** Codex is enabled in the New session dialog with its own model list (`gpt-6.1-sol`, `gpt-6-sol`, `gpt-6-luna`, or the default).
8. **Billing check, local part:** `codex login status` shows "Logged in using ChatGPT", and neither `OPENAI_API_KEY` nor `CODEX_API_KEY` is set (process, user or machine), so Codex should not bill the API.

## Current state

- Phase 2 code is done and passes the tests, but nothing is committed yet on `feature/codex-adapter`.
- Side effects of running Codex headless: each project folder gets added as `trust_level = "trusted"` in `~/.codex/config.toml` (the probe repo's entry is there now), and the `notify` hook in that config (`codex-computer-use.exe turn-ended`) fires after every hub turn.

## Next steps

- Superseded by Session 10.

---

2026-10-25 15:02 (UTC-05:00)

# Session 10: Ran the live tests, confirmed no API charges, and added the usage meter and token count for Codex sessions.

## What was done

1. **Live tests (user asked):** `test_claude_live.py` passed. `test_codex_live.py` first failed after the question and reply had worked: reading `color.txt` gave "Permission denied". Cause: on Python 3.14 for Windows, pytest's `tmp_path` gets an owner-only ACL (`OWNER RIGHTS`, SYSTEM, Administrators), and the file was owned by the Codex sandbox user, so the user's account had no access. Files in normal project folders inherit the user's access, as the probe files did. The test now uses a plain `%TEMP%\hub-codex-live-*` folder and deletes it afterwards, and the rerun passed. This confirms `developer_instructions` as a `-c` override and resume through the adapter. These runs used 2 Claude turns and 4 Codex turns.
2. **Billing check:** the user confirmed the OpenAI API usage page shows no charges after the probe and live-test turns. Recorded in `plan.md`; the Windows billing question is closed.
3. **Codex usage meter:** Codex's JSON stream has no limit or price information. Its session file (`~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread id>.jsonl`, appended on resume) records a `token_count` per turn with the plan's 5h (`primary`, 300 min) and weekly (`secondary`, 10080 min) windows. A new `_after_turn` hook in `CliAdapter` lets `CodexAdapter` read the latest one (last 512 KB of the file) after each turn and emit a `RATE_LIMIT` event in Claude's shape, so the existing meter works unchanged. The format is undocumented, so if a Codex update changes it, the meter disappears and nothing breaks. Checked against the real session file from the probe: 5h 1%, 7d 0%, plan `plus`. User's choice: session files over `codex app-server` (more stable, but a bigger change).
4. **Turn summary:** the user chose a token count over a price estimated from hardcoded API prices. When a turn has no `cost_usd`, the summary shows e.g. "18.8k tokens · 6.2s". The meter tooltip no longer says "Claude".
5. **Tests:** tests use a temporary `codex_home`, never the real `~/.codex`. New tests cover reading the latest window, a missing file or missing limits, a turn ending with usage windows, and a reached limit reported as `rejected`. **88 passed, 2 skipped** (the live tests). Frontend type-checks.
6. **Acceptance test passed (user, 2026-10-25):** a Codex session in the UI asked a question, resumed on the reply, completed, showed the usage meter and token count, and kept its history. **Phase 2 is done.**

## Current state

- **Phase 2 is complete.** Sessions 9 and 10 are committed on `feature/codex-adapter` and merged into `main`.
- Side effects of running Codex headless are unchanged: each project folder gets added as `trust_level = "trusted"` in `~/.codex/config.toml` (the probe repo's entry is there now), and the `notify` hook in that config (`codex-computer-use.exe turn-ended`) fires after every hub turn.
- The failed live run left `%TEMP%\pytest-of-<user>\pytest-5\test_question_then_reply1\color.txt`, which the user's account can't read. It's harmless.

## Next steps

**For the user**
- [ ] Decide whether to remove the probe repo's trusted-project entry from `~/.codex/config.toml`.

**For the agent**
- [ ] Start Phase 3 (automatic handoff) on a new `feature/` branch from `main`.
- [ ] Phase 3: handoff keys off `LIMIT_REACHED`. Claude warns early through `RATE_LIMIT` events. For Codex, `RATE_LIMIT` arrives only after a turn ends, and its `status` is `rejected` when `rate_limit_reached_type` is set, so the next turn could be handed off before it starts.
- [ ] Carried over: the rest of the `CLAUDE.md` update (frontend, streaming, folder endpoints), and `remark-gfm` for tables.
