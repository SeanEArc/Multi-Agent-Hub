# Manual tests

Tests to run by hand with real agents. The automated tests use fake agents, and the screenshots used sample data, so these are the only checks of how the features behave with the real Claude Code and Codex CLIs. They use some real Claude and Codex usage.

**Before you start:** restart the backend (`uv run python -m app.main` in `backend/`, or `python -m app.main` if the backend's virtual environment is active and `uv` isn't on that terminal's PATH) so it serves the new fields, then run `npm run dev` in `frontend/` and open http://localhost:5173. The backend applies database migrations itself on start.

## APPLICATION REVISION 2: model picker and reasoning effort

- [ ] **Centered new session.** Click **New session**: "What should we build today?" sits above the text box in the middle. Pick a folder: it changes to "What should we build in {folder}?" (the repository's name when the folder is inside one).
- [ ] **One model box.** Beside Send there is one box with the agent's mark (the Claude spark, the Codex knot, or "Local"), the model and the effort. Click it: three columns open (provider, models, effort). Click the Codex logo: your Codex models appear, and picking one shows only the efforts that model supports. Claude lists only full names (no "(latest)" entries). The effort column has no "Default" row. Picking an effort closes the menu and updates the box; Escape or a click outside changes nothing.
- [ ] **Effort reaches the agent.** In a Claude chat, pick **Low** and send a message, then switch to **Max** and send another: the second turn should think noticeably longer. Do the same in a Codex chat. (Checked from the CLI on 2026-11-12: Haiku and Sonnet 5.5 accept low, extra high and max without errors.)
- [ ] **Default effort in Settings.** In Settings, set the effort next to the default agent and model to **High**: a new session's box starts on High. It is hidden when the default agent is the local model.
- [ ] **Switch dialog.** In a chat with a few messages, pick another agent in the model box: the dialog shows the chosen model and effort (no model dropdown) and offers **Switch with brief**, **My messages only** and **Start clean**. Choose **My messages only** and ask "what did I ask before?": the agent knows your messages but not the previous agent's replies, and the transcript row says "your messages only" with a **brief** button showing what was sent.

## APPLICATION REVISION: session flow

- [ ] **New session opens a chat.** Restart the app (or the backend and `npm run dev`). Click **New session**: an empty chat opens with the text box focused and no dialog. Under the text box, pick Codex and a model, choose a project folder (try a git repo: the worktree checkbox appears), and send a message. The session appears in the sidebar, titled from your message, and the folder now shows as locked.
- [ ] **Switch model with and without a brief.** In that session, pick Claude under the text box: a dialog asks about passing a brief. Choose **Switch with brief**, then send "what were we doing?": Claude should know the task. Switch again with **Start clean**: the next agent should not know it. The transcript rows say "no brief" and "waiting for your message".
- [ ] **Model lists follow your plan.** Restart the app. In the model box, Claude shows its current full names (Fable 5.2, Opus 5.5, Sonnet 5.5, Haiku 5.5, no "(latest)" aliases since revision 2); Codex shows your own Codex models from your account. In Settings, **New sessions start with** has no "Default" choice. Click **Check which models my plan can use** for Claude (uses a little usage, takes a minute or two): models your plan can't use become "(not on your plan)" and can't be picked. Try **Other…** and type a model name.
- [ ] **Chaining switches.** In Settings, check that **Allow chaining** shows the chain and **Auto handoff by default**, and pick a default agent and model (a new chat should start with them). Then start the backend with `$env:HUB_SIMULATE_LIMIT="claude"`: with **Auto handoff** on in a Claude chat, the next agent takes over at once; switch it off mid-chat and the next limit offers the handoff instead; with **Allow chaining** off in Settings, a limit shows a yellow notice and the session waits.

## Phase 5c: local model robustness and choice

- [x] **Model choice, notices and handoff.** Restart the backend.
  - Settings: **Default local model** lists your installed models; pick one and save. The new-session dialog's Model list then says "Default (that model)". The chain editor's local entry and the **Hand off** dialog also list the installed models, and Hand off is disabled with a message when Ollama isn't running.
  - A local session shows a one-line note under its header (runs on this PC, slower and weaker at coding).
  - Hand a Codex session off to the local model (Hand off → Local model): it continues from the brief.
  - Optional, to see the context notice: start the backend with `$env:HUB_OLLAMA_NUM_CTX="8192"`, ask the local model to read several files: a yellow notice says the conversation was shortened, and the follow-up still answers.
  - Why: the API and the loop are tested, the dialogs and notice row only built and type-checked.

## Phase 5b: local model tools

- [x] **A small coding task with the local model.** Restart the backend, click **New session**, pick a scratch folder (or a test project in a worktree) and **Local model (Ollama)**, and ask for something small, for example "create hello.py that prints the sum of 17 and 25, run it with python and tell me the output", then "add a function that returns the average of a list, and test it".
  - Expect: rows for the model's steps like Claude's: **Write** / **Edit** with a diff, **PowerShell** with its output (and a red exit code when one fails), **Read**, **List**, **Search**. A plan card when it makes a plan. The edited files under **Files changed** in Split. The turn ends with its summary (or just "Done"; small models often skip the summary).
  - Also try: **Stop** while it runs a slow command (ask it to run `Start-Sleep 60`): the turn ends at once. Ask for something that needs your input ("ask me which name to use, then create the file"): it ends with a question, and your answer continues it.
  - Why: the tools and loop passed tests and live runs against your Ollama, but not in the app's transcript.

## Phase 5a: local model chat

- [x] **Chat with the local model in the app.** Restart the app (or the backend), click **New session**, leave the folder empty and pick **Local model (Ollama)** as the agent.
  - Expect: the Model list shows your installed models, with "Default (qwen3.5:9b-q4_K_M)" first, and the hint says it runs with no subscription. Ask a question: it answers in a few seconds to a minute, and the header shows "Local model (qwen3.5:9b-q4_K_M)". A follow-up continues the conversation, also after closing and reopening the app. **Stop** during a long answer ends the turn. Deleting the session removes its file under `%LOCALAPPDATA%\MultiAgentHub\local`.
  - Also try: quit Ollama (tray icon → Quit) and open the dialog again: the hint says Ollama isn't running and **Create** is disabled.
  - Why: checked with the API, the real Ollama server and tests, but the dialog itself hasn't been looked at in the app.

## Phase 4b: dashboard, progress view, history and summaries

- [x] **Claude subagents on the dashboard.** Confirmed on 2026-10-26: the working session's card lists each running subagent with its latest step.

- [x] **Plan from a checklist, live (Codex and Claude).** Confirmed on 2026-10-26 with both agents, after the instruction was made firmer for Claude: the plan card appears and ticks off as the agent re-posts its `[[PLAN]]` checklist.

- [x] **Plan on the dashboard cards.** Confirmed by the user on 2026-10-26. Restart the backend, then open **All sessions** while a session with a multi-step task is working, and again after it finishes.
  - Expect: the card shows "Plan · X of Y done" with a bar and up to 5 steps, the current one in bold, updating with the dashboard's 5 s poll. A longer plan shows the steps around the current one and "N more steps". Finished and idle sessions keep their last plan; after a handoff the card has no plan until the new agent writes one.
  - Why: only checked with tests and the type-checker so far, not in a browser.

- [x] **Claude running commands in `auto` mode.** Passed (user, 2026-10-27). Start the backend with `$env:HUB_CLAUDE_PERMISSION_MODE="auto"`, then ask a Claude session in a worktree to run its tests, commit, and push its branch.
  - Expect: the tests and the commit run without "denied: needs approval". Pushing straight to `main` may still be refused by the safety check; note which commands were refused.
  - Why: with the default `acceptEdits`, every shell command is denied headless. `auto` has only been tried with `git status` and `python --version`; this decides whether it becomes the default.

- [x] **Panel / Split switch.** (passed 2026-11-02) In any session, click **Split**, then **Panel**.
  - Expect: Split shows a side panel with the plan, subagents and **Files changed** (with edit counts). Below about 960 px wide, the side panel gives way to the card. The choice is still set after reloading the page, and matches the setting in **Settings**. **Hide** collapses the card.
  - Why: the layout is saved as a hub setting through the API; this checks that saving, reloading and the narrow-window fallback work in a real browser.

- [x] **Claude subagents in the session view.** (passed 2026-11-02) In a Claude session that starts subagents, open the session.
  - Expect: each subagent is listed in the card (or side panel), and clicking it shows its steps. In the transcript, its steps are folded under its row instead of mixed into the conversation.
  - Why: the dashboard was confirmed; the session view's subagent list and the folded transcript rows haven't been checked with real Claude output.

- [x] **Session history.** (passed 2026-11-02) Open a session that has had a few turns (ideally one with a handoff) and click **History** in the header.
  - Expect: totals at the top (agents, turns, tool calls, files edited, tokens, API price for Claude, time span), then one card per agent in order, with why it took over and its **View brief**. Each turn shows how it ended (Done, Asked you, Usage limit, Stopped, Error, Cut off), when, its prompt, and its tool calls, files, plan, cost or tokens. Clicking a prompt shows the full message and the files edited; **Show in transcript** switches back and highlights that turn. Sending a message from History switches to the transcript.
  - Why: built from the session's events in the browser and checked only with sample data; real sessions may have event orders the sample didn't cover.

- [x] **Session summaries.** (passed 2026-11-02) Restart the backend (it applies a new database migration on start), open a session that has had a few turns, and click **History**.
  - Expect: a **Summary** card at the top with the task, the latest request (if you sent more than one), the plan step, how the last turn ended and the agent's last message in it, and the changes (files edited, the branch's diff total, uncommitted files). Click **Re-summarize**: it shows "Writing…" for a few seconds to a minute, then a short written summary with who wrote it, when, and the tokens it used. Nothing new appears in the transcript, and the agent's own conversation isn't touched (the next turn still resumes it). **All sessions** shows the summary's first line on the card. Send another message: the summary is marked **Outdated**, on the card too.
  - Also try: Re-summarize while a turn is running (it works alongside), and with `$env:HUB_SIMULATE_LIMIT="claude"` on a Claude session (Codex should write it, noting Claude was out of usage).
  - Why: the one-off call (`claude --tools "" --no-session-persistence`, `codex exec --ephemeral` read-only) passed the live adapter tests and one real Codex summary on sample data; not yet on a real session.

- [x] **Light mode.** Pick **Light** under Theme in Settings (or leave it on System and switch Windows) and look over the dashboard and a session.
  - Why: new colors were added for the dashboard and progress view; the screenshots covered light mode with sample data only.

- [x] **Handoff still works.** (passed 2026-11-02) In a session, click **Hand off** and pick another agent.
  - Expect: the new agent picks up the task, and the plan card starts fresh for it instead of showing the previous agent's plan.
  - Why: the progress view reads the plan from the current agent's events only; this checks that it switches cleanly at a handoff.

## Phase 4c: style polish

- [x] **Tables, task lists and code colors (4c-1).** Reload the page and ask an agent for something that answers with a markdown table, a checklist and code (for example "compare two ways to do X in a table, then show the code").
  - Expect: a bordered table (scrolling sideways if it's wide), checkboxes inline with their items, ~~strikethrough~~ in gray, and colored code in blocks that name a language (`ts`, `python`, `diff` with red/green lines). Blocks without a language stay plain. Check light mode too.
  - Also: a Codex edit row reads like `Edit src\theme.ts, src\useTheme.ts (new)`, not JSON; and at a narrow window width the session header's buttons wrap onto a second line instead of running off the side.
  - Why: checked only with sample data in screenshots.
- [x] **Codex diffs (4c-2).** Restart the backend, start a Codex session on a git project and ask for a small edit to an existing file and a new file (for example "add type hints to one function and create `todo.md`"). Then ask for a second edit to the same file.
  - Expect: each Codex **Edit** row shows its diff under it, with line numbers and `+N −M` on the row. A diff that changes more than 20 lines starts closed and opens on click; a smaller one starts open. The second edit's diff shows only that edit, not both. **Files changed** in the Split layout lists the file with 2 edits; opening it shows each edit's time, agent and diff.
  - Why: the hub works out Codex's diffs by reading the files right after each patch. This was tested with a fake Codex and the real snapshot code, but not with the live CLI.
- [x] **Claude diffs and command rows (4c-2).** In a Claude session, ask for an edit to an existing file and for a command that fails (for example "run `git log` on a branch that doesn't exist").
  - Expect: the Edit row shows the real diff with line numbers. A failed command shows a red `exit 128` (or whatever the code is) and the first and last lines of its output under the row. A successful command shows its first lines. The turn's last row reads like `✓ Done · 1 file edited`.

## Chats (no project folder)

- [x] **Quick question with no folder.** (passed 2026-11-02) Restart the backend (it applies a migration), click **New session**, leave **Project folder** and **Title** empty, type a question as the first message and create it.
  - Expect: the session is titled after your question, shows "Chat" instead of a folder, and the agent answers. A follow-up continues the same conversation. Deleting the session removes its folder under `%LOCALAPPDATA%\MultiAgentHub\chats`.
  - Why: checked by tests with the fake CLIs only, not with real Claude or Codex.

## Phase 4a: worktrees

- [x] **Worktree on a real project.** (passed 2026-11-02) Create a session on one of your own git projects with **Work in a separate git worktree** checked, and ask the agent for a small change.
  - Expect: your original project folder is unchanged, and the change is on the session's `hub/...` branch. Click the branch chip and choose **Commit and remove**: the worktree folder is removed and the branch is kept.
  - Why: this is Phase 4a's acceptance test. It has only run against temporary test repos, and Phase 4a isn't marked done in `plan.md` until it passes on a real project.

## Before Phase 4d

- [x] **Install Rust** (https://rustup.rs). Why: the Tauri desktop shell in Phase 4d needs it to build.
