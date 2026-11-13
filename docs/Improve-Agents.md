# Improving the Agents While Using Fewer Tokens

A **token** is a small chunk of text (about 4 characters) that a model reads or writes. Usage limits count tokens. Most tokens go to **input**, not output. On every turn the agent re-reads its whole conversation so far, its system prompt (the hidden instructions it starts with) and every `CLAUDE.md` file. Tool output (file reads, command logs, diffs), handoff briefs and long, chatty replies then add more on top.

Each item has a tag: **Effort / Impact**, rated Low, Medium or High.

## Quick wins

- **Keep CLAUDE.md short.** Claude Code loads every `CLAUDE.md` from the working folder and the folders above it into each session, so every line costs tokens on every turn. Cut history and detail out of it. Point to `progress.md` or `plan.md` for that, and let the agent open them only when needed. *Low / High*
- **Clear context between unrelated tasks.** In an interactive Claude Code window, `/clear` starts fresh and `/compact` swaps the long history for a short summary. A long chat about task A makes every turn of task B cost more. In this app, starting a new session for a new task has the same effect. *Low / High*
- **Ask for short answers.** Tell the agent "reply in a few sentences, no recap of the code." Output tokens are fewer than input tokens, but they count heavily, and long replies also get re-read on every later turn. *Low / Medium*
- **Give exact file paths.** "Fix the cap in `backend/app/handoff.py`" stops the agent from searching and reading ten files to find the right spot. Searching is one of the most expensive things an agent does. *Low / High*

## In this app's code

- **Keep using `--resume` for replies.** `backend/app/adapters/claude.py` uses `--resume <session id>` and `codex.py` uses `codex exec resume`. Replies carry on the same conversation, so the agent doesn't have to re-read files it already saw. Restarting would mean re-exploring the project. *Already done / High*
- **Keep the prompt start stable for caching.** **Prompt caching** means the provider remembers the unchanged start of a prompt for a few minutes, and re-reading that part is cheaper and faster. Each turn's prompt begins with the system prompt, `HEADLESS_PROMPT` (`backend/app/adapters/cli.py`) and `CLAUDE.md`. Don't add per-turn details like timestamps or session titles to them, or the cache resets. *Low / Medium*
- **Shrink the handoff brief.** `backend/app/handoff.py` caps the brief at 40,000 characters (about 10,000 tokens), and the diff alone can take 20,000 of those. Try lowering `MAX_DIFF` to 8–10k. The brief already tells the agent to run `git diff HEAD` if it needs the rest. *Low / Medium*
- **Leave lockfiles and generated files out of the brief's diff.** A change to `uv.lock` or `package-lock.json` can fill the whole diff budget with useless lines. In `git_state`, add git exclude pathspecs (e.g. `":(exclude)uv.lock"`) to the `git diff` calls. They will still show up in `--stat`. *Low / Medium*
- **Trim the other brief sections.** `LAST_AGENT_MESSAGES = 3` at up to `MAX_MESSAGE = 4_000` characters each, plus 20 commands, adds up. The last message usually holds the latest state. Try 2 messages and 10 commands and see whether handoffs still go well. *Low / Low-Medium*
- **Only hand off when you need to.** Each handoff starts a fresh native session, so the new agent re-reads the project and the cache is empty. The loop guard in `backend/app/orchestrator.py` (`_limited_agents`) already stops agents from bouncing back and forth. Avoid needless manual handoffs too. *Low / Medium*
- **Stop runaway turns.** If an agent is stuck repeating a failing command, the Stop button (`/stop`, which kills the process tree) ends the waste. A later feature could warn when one turn has used a lot of tokens. *Medium / Medium*

## How you prompt and set up projects

- **One clear task per session.** Small, well-scoped sessions keep the history short, so each turn re-reads less. They also make handoff briefs smaller and clearer. *Low / High*
- **Use worktrees with a project subfolder.** If you start a session on a subfolder, the agent's cwd is that `subdir` inside the worktree (`backend/app/worktrees.py`). It tends to look closer to home, which means fewer broad searches. *Low / Low-Medium*
- **Use subagents for exploration, but with care.** A **subagent** is a helper agent with its own fresh context. It can read many files and send back only a short answer, which keeps the main conversation small. But each one starts cold and has to load its own context, so for a small lookup it costs more than doing the job directly. Use them for big searches only. *Low / Medium*
- **Don't paste big logs.** Paste the 20 relevant lines of an error, not the full test run. Everything you paste is re-read on each later turn. *Low / Medium*

## Choosing models

- **Send simple tasks to cheaper models.** Each chain entry can name a model, and the adapters pass it through `--model`. Use a smaller, faster model for renames, small fixes and docs. Save the strongest model for hard design or debugging work. *Low / High*
- **Use the local Ollama model for routine work (phase 5).** Once the Ollama adapter exists, it costs no subscription usage at all. Good fits are summaries, commit messages, formatting and simple edits. Putting it first in the chain for low-risk sessions, or last as a fallback, saves your Claude and Codex limits. *High / High*
- **Use a small model for summaries (phase 4b history).** When session summaries get built, write them with a small or local model, not the main coding agent. A summary only needs to read text, not reason hard about code. *Medium / Medium*

## Measuring

- **Track tokens per session and per stint.** Claude's `result` event reports token usage (and a cost), and Codex's `token_count` reports tokens (`read_rate_limits` in `codex.py` already reads that file). Saving totals per stint would show which tasks, prompts or brief sizes cost the most. *Medium / High*
- **Watch the usage windows.** `GET /api/usage` shows each agent's latest usage window. Check it before starting a big task, and pick the agent with the most room left. *Low / Medium*
- **See what fills the context.** In interactive Claude Code, `/context` shows what takes up space (system prompt, memory files, tools, messages). Use it once to see how much your `CLAUDE.md` really costs. *Low / Medium*

## Where to start

1. **Trim CLAUDE.md** to what every turn needs, and move the details to linked files.
2. **Route easy tasks to a cheaper model** in the chain settings, and plan Ollama in as the free tier.
3. **Lower `MAX_DIFF` and exclude lockfiles** in `backend/app/handoff.py`, so each handoff costs less.
