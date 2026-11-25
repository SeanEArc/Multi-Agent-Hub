# Agent adapters: interface, runner, Claude and Codex

Paths like `app/...` and `tests/...` are inside `backend/`.

## Adapter interface

**Adapter interface** (`app/adapters/base.py`): one `AgentAdapter` instance per session, constructed with its cwd and model. `start_task` and `send_reply` return async iterators of normalized `AgentEvent`s (`EventType`). A usage limit is a `LIMIT_REACHED` **event**, not an exception, and handoff will key off it. The orchestrator, UI and history should depend only on this interface.

## Runner

**Runner** (`app/adapters/runner.py`): every CLI launch goes through a `Runner` (`start(command, cwd, env)` returns a `RunningProcess` with `send_input`, `lines`, `read_stderr`, `wait`, `kill`). `HostRunner` (the default) runs a local process and kills its tree with `taskkill /T`. Phase 9's Docker runner plugs in here; adapters take an optional `runner=`.

## Shared CLI plumbing

**Shared CLI plumbing** (`app/adapters/cli.py`): `CliAdapter` runs one process per turn through its runner, sends the prompt over stdin, streams parsed lines, adds an error if the CLI exits before reporting the end of the turn, and `stop()` kills the process tree. It also holds `NEEDS_INPUT_MARKER` / `HEADLESS_PROMPT` and the headless git env. Neither CLI has a plan tool headless, so `HEADLESS_PROMPT` asks for the plan as a markdown checklist under a `PLAN_MARKER` line, re-posted after each step; `split_plan` strips the marker from messages and both parsers turn the checklist into a `PLAN` event when it changes (`plan_items`: the first unchecked step is in progress). Both adapters subclass it.

## Claude adapter

**Claude adapter** (`app/adapters/claude.py`): one `claude -p --input-format stream-json --output-format stream-json --verbose` process per turn, with the prompt sent over **stdin** (this avoids Windows quoting) as one stream-json user line for **every** call (turns, `run_command`, `summarize`; `user_line`), so images and PDFs can go as content blocks; fake Claude refuses plain-text input. Replies use `--resume <session id>`. `--permission-prompts none` means unapproved tools are denied rather than left hanging. AskUserQuestion is unavailable headless, so an appended system prompt makes Claude end question turns with `[[NEEDS_INPUT]]`, which `TurnParser` turns into a `QUESTION` event. `rate_limit_event` with status `rejected` is the main limit signal; regex on the result text is the fallback. `stop()` kills the whole process tree (`taskkill /T`). It refuses `.cmd`/`.bat` shims and requires the native `claude.exe`. Behavior was verified against Claude Code 2.1.305.

## Codex adapter

**Codex adapter** (`app/adapters/codex.py`): `codex exec --json -` per turn, `codex exec resume --json <thread id> -` for replies. Codex chooses the thread id and reports it in `thread.started`, so the adapter learns it from the stream and the orchestrator saves it from the `STARTED` event (Claude's id is chosen up front). Sandbox, `approval_policy="never"` and the marker instruction (`developer_instructions`) are passed as `-c` overrides, because `resume` has no `--sandbox`. `find_codex` runs the native `codex.exe` inside the npm package, never the `.cmd`/`.ps1` launchers. Exec mode never waits for approval: blocked actions fail as ordinary command errors. The stream has no rate-limit events, so after each turn `read_rate_limits` takes the plan's usage windows from the latest `token_count` in Codex's session file (`$CODEX_HOME/sessions/.../rollout-*-<thread id>.jsonl`, an undocumented format, read best effort) and emits the same `RATE_LIMIT` shape as Claude. A limit is detected from the error text. Codex reports no price, so the UI shows tokens. Verified against codex-cli 0.164.0. Settings: `HUB_CODEX_SANDBOX` (default `workspace-write`, which also allows `%TEMP%`) and `HUB_CODEX_PATH`.

## Diffs

**Diffs** (`app/adapters/diffs.py`): an edit's `TOOL_RESULT` carries `diffs: [{path, kind add|update|delete, diff, base, truncated}]`, where `diff` is unified hunks with no file header (capped at 40k characters) or None when unknown. Claude's come from the `structuredPatch` (or a create's content) in the stream's `tool_use_result`. Codex's come from `FileSnapshots`: `CodexAdapter._on_event` (async, a `CliAdapter` hook) reads each patched file after `item.completed` and diffs it against the copy it kept after the previous patch, or against `HEAD` the first time (`base: "commit"`), in memory per adapter. Codex command results also carry `exit_code`. The UI (`frontend/src/diffs.ts`, `DiffView.tsx`) rebuilds diffs without line numbers from Claude's input for events saved before this, and reads Claude's exit code from the `Exit code N` line its results start with.
