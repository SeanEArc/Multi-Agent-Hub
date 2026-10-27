# Manual tests

Tests to run by hand with real agents. The automated tests use fake agents, and the screenshots used sample data, so these are the only checks of how the features behave with the real Claude Code and Codex CLIs. They use some real Claude and Codex usage.

**Before you start:** restart the backend (`uv run python -m app.main` in `backend/`, or `python -m app.main` if the backend's virtual environment is active and `uv` isn't on that terminal's PATH) so it serves the new fields, then run `npm run dev` in `frontend/` and open http://localhost:5173. No database migration is needed.

## Phase 4b: dashboard and progress view

- [x] **Claude subagents on the dashboard.** Confirmed on 2026-10-26: the working session's card lists each running subagent with its latest step.

- [x] **Plan from a checklist, live (Codex and Claude).** Confirmed on 2026-10-26 with both agents, after the instruction was made firmer for Claude: the plan card appears and ticks off as the agent re-posts its `[[PLAN]]` checklist.

- [x] **Plan on the dashboard cards.** Confirmed by the user on 2026-10-26. Restart the backend, then open **All sessions** while a session with a multi-step task is working, and again after it finishes.
  - Expect: the card shows "Plan · X of Y done" with a bar and up to 5 steps, the current one in bold, updating with the dashboard's 5 s poll. A longer plan shows the steps around the current one and "N more steps". Finished and idle sessions keep their last plan; after a handoff the card has no plan until the new agent writes one.
  - Why: only checked with tests and the type-checker so far, not in a browser.

- [ ] **Claude running commands in `auto` mode.** Start the backend with `$env:HUB_CLAUDE_PERMISSION_MODE="auto"`, then ask a Claude session in a worktree to run its tests, commit, and push its branch.
  - Expect: the tests and the commit run without "denied: needs approval". Pushing straight to `main` may still be refused by the safety check; note which commands were refused.
  - Why: with the default `acceptEdits`, every shell command is denied headless. `auto` has only been tried with `git status` and `python --version`; this decides whether it becomes the default.

- [ ] **Panel / Split switch.** In any session, click **Split**, then **Panel**.
  - Expect: Split shows a side panel with the plan, subagents and **Files changed** (with edit counts). Below about 960 px wide, the side panel gives way to the card. The choice is still set after reloading the page, and matches the setting in **Settings**. **Hide** collapses the card.
  - Why: the layout is saved as a hub setting through the API; this checks that saving, reloading and the narrow-window fallback work in a real browser.

- [ ] **Claude subagents in the session view.** In a Claude session that starts subagents, open the session.
  - Expect: each subagent is listed in the card (or side panel), and clicking it shows its steps. In the transcript, its steps are folded under its row instead of mixed into the conversation.
  - Why: the dashboard was confirmed; the session view's subagent list and the folded transcript rows haven't been checked with real Claude output.

- [ ] **Light mode.** Switch Windows (or the browser) to light mode and look over the dashboard and a session.
  - Why: new colors were added for the dashboard and progress view; the screenshots covered light mode with sample data only.

- [ ] **Handoff still works.** In a session, click **Hand off** and pick another agent.
  - Expect: the new agent picks up the task, and the plan card starts fresh for it instead of showing the previous agent's plan.
  - Why: the progress view reads the plan from the current agent's events only; this checks that it switches cleanly at a handoff.

## Phase 4a: worktrees

- [ ] **Worktree on a real project.** Create a session on one of your own git projects with **Work in a separate git worktree** checked, and ask the agent for a small change.
  - Expect: your original project folder is unchanged, and the change is on the session's `hub/...` branch. Click the branch chip and choose **Commit and remove**: the worktree folder is removed and the branch is kept.
  - Why: this is Phase 4a's acceptance test. It has only run against temporary test repos, and Phase 4a isn't marked done in `plan.md` until it passes on a real project.

## Before Phase 4d

- [ ] **Install Rust** (https://rustup.rs). Why: the Tauri desktop shell in Phase 4d needs it to build.
