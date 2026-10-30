# Manual tests

Tests to run by hand with real agents. The automated tests use fake agents, and the screenshots used sample data, so these are the only checks of how the features behave with the real Claude Code and Codex CLIs. They use some real Claude and Codex usage.

**Before you start:** restart the backend (`uv run python -m app.main` in `backend/`, or `python -m app.main` if the backend's virtual environment is active and `uv` isn't on that terminal's PATH) so it serves the new fields, then run `npm run dev` in `frontend/` and open http://localhost:5173. The backend applies database migrations itself on start.

## Phase 4b: dashboard, progress view, history and summaries

- [x] **Claude subagents on the dashboard.** Confirmed on 2026-10-26: the working session's card lists each running subagent with its latest step.

- [x] **Plan from a checklist, live (Codex and Claude).** Confirmed on 2026-10-26 with both agents, after the instruction was made firmer for Claude: the plan card appears and ticks off as the agent re-posts its `[[PLAN]]` checklist.

- [x] **Plan on the dashboard cards.** Confirmed by the user on 2026-10-26. Restart the backend, then open **All sessions** while a session with a multi-step task is working, and again after it finishes.
  - Expect: the card shows "Plan · X of Y done" with a bar and up to 5 steps, the current one in bold, updating with the dashboard's 5 s poll. A longer plan shows the steps around the current one and "N more steps". Finished and idle sessions keep their last plan; after a handoff the card has no plan until the new agent writes one.
  - Why: only checked with tests and the type-checker so far, not in a browser.

- [x] **Claude running commands in `auto` mode.** Passed (user, 2026-10-27). Start the backend with `$env:HUB_CLAUDE_PERMISSION_MODE="auto"`, then ask a Claude session in a worktree to run its tests, commit, and push its branch.
  - Expect: the tests and the commit run without "denied: needs approval". Pushing straight to `main` may still be refused by the safety check; note which commands were refused.
  - Why: with the default `acceptEdits`, every shell command is denied headless. `auto` has only been tried with `git status` and `python --version`; this decides whether it becomes the default.

- [ ] **Panel / Split switch.** In any session, click **Split**, then **Panel**.
  - Expect: Split shows a side panel with the plan, subagents and **Files changed** (with edit counts). Below about 960 px wide, the side panel gives way to the card. The choice is still set after reloading the page, and matches the setting in **Settings**. **Hide** collapses the card.
  - Why: the layout is saved as a hub setting through the API; this checks that saving, reloading and the narrow-window fallback work in a real browser.

- [ ] **Claude subagents in the session view.** In a Claude session that starts subagents, open the session.
  - Expect: each subagent is listed in the card (or side panel), and clicking it shows its steps. In the transcript, its steps are folded under its row instead of mixed into the conversation.
  - Why: the dashboard was confirmed; the session view's subagent list and the folded transcript rows haven't been checked with real Claude output.

- [ ] **Session history.** Open a session that has had a few turns (ideally one with a handoff) and click **History** in the header.
  - Expect: totals at the top (agents, turns, tool calls, files edited, tokens, API price for Claude, time span), then one card per agent in order, with why it took over and its **View brief**. Each turn shows how it ended (Done, Asked you, Usage limit, Stopped, Error, Cut off), when, its prompt, and its tool calls, files, plan, cost or tokens. Clicking a prompt shows the full message and the files edited; **Show in transcript** switches back and highlights that turn. Sending a message from History switches to the transcript.
  - Why: built from the session's events in the browser and checked only with sample data; real sessions may have event orders the sample didn't cover.

- [ ] **Session summaries.** Restart the backend (it applies a new database migration on start), open a session that has had a few turns, and click **History**.
  - Expect: a **Summary** card at the top with the task, the latest request (if you sent more than one), the plan step, how the last turn ended and the agent's last message in it, and the changes (files edited, the branch's diff total, uncommitted files). Click **Re-summarize**: it shows "Writing…" for a few seconds to a minute, then a short written summary with who wrote it, when, and the tokens it used. Nothing new appears in the transcript, and the agent's own conversation isn't touched (the next turn still resumes it). **All sessions** shows the summary's first line on the card. Send another message: the summary is marked **Outdated**, on the card too.
  - Also try: Re-summarize while a turn is running (it works alongside), and with `$env:HUB_SIMULATE_LIMIT="claude"` on a Claude session (Codex should write it, noting Claude was out of usage).
  - Why: the one-off call (`claude --tools "" --no-session-persistence`, `codex exec --ephemeral` read-only) passed the live adapter tests and one real Codex summary on sample data; not yet on a real session.

- [ ] **Light mode.** Switch Windows (or the browser) to light mode and look over the dashboard and a session.
  - Why: new colors were added for the dashboard and progress view; the screenshots covered light mode with sample data only.

- [ ] **Handoff still works.** In a session, click **Hand off** and pick another agent.
  - Expect: the new agent picks up the task, and the plan card starts fresh for it instead of showing the previous agent's plan.
  - Why: the progress view reads the plan from the current agent's events only; this checks that it switches cleanly at a handoff.

## Phase 4c: style polish

- [ ] **Tables, task lists and code colors (4c-1).** Reload the page and ask an agent for something that answers with a markdown table, a checklist and code (for example "compare two ways to do X in a table, then show the code").
  - Expect: a bordered table (scrolling sideways if it's wide), checkboxes inline with their items, ~~strikethrough~~ in gray, and colored code in blocks that name a language (`ts`, `python`, `diff` with red/green lines). Blocks without a language stay plain. Check light mode too.
  - Also: a Codex edit row reads like `Edit src\theme.ts, src\useTheme.ts (new)`, not JSON; and at a narrow window width the session header's buttons wrap onto a second line instead of running off the side.
  - Why: checked only with sample data in screenshots.

## Phase 4a: worktrees

- [ ] **Worktree on a real project.** Create a session on one of your own git projects with **Work in a separate git worktree** checked, and ask the agent for a small change.
  - Expect: your original project folder is unchanged, and the change is on the session's `hub/...` branch. Click the branch chip and choose **Commit and remove**: the worktree folder is removed and the branch is kept.
  - Why: this is Phase 4a's acceptance test. It has only run against temporary test repos, and Phase 4a isn't marked done in `plan.md` until it passes on a real project.

## Before Phase 4d

- [ ] **Install Rust** (https://rustup.rs). Why: the Tauri desktop shell in Phase 4d needs it to build.
