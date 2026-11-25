# Architecture notes

How each part of Multi-Agent Hub works, and why, one file per topic. Read the file for the part you're changing; `backend/AGENTS.md` and `frontend/AGENTS.md` say which one. Update it in the same pull request when that part changes.

| File | Covers |
|---|---|
| [`database.md`](database.md) | SQLite, tables, settings, migrations and their safety rules |
| [`adapters.md`](adapters.md) | The adapter interface, runner, shared CLI plumbing, Claude and Codex adapters, diffs |
| [`local-model.md`](local-model.md) | The Ollama adapter, its tools and loop, context fitting and loop detection |
| [`models.md`](models.md) | Model lists, availability and reasoning effort |
| [`skills.md`](skills.md) | The skills library and how skills reach each agent |
| [`commands.md`](commands.md) | Slash commands for Claude, Codex and the local model |
| [`sessions.md`](sessions.md) | The orchestrator, worktrees, chats, handoff briefs and summaries |
| [`attachments.md`](attachments.md) | Uploads, text extraction and the memory-capped worker |
| [`documents.md`](documents.md) | `hub-doc`, the outputs folder and Office automation |
| [`api-and-auth.md`](api-and-auth.md) | The app factory, API routes, the token and origin checks |
| [`desktop.md`](desktop.md) | The Tauri shell, backend supervision and job objects |
| [`frontend.md`](frontend.md) | Theme and styling conventions, the preview pane |
| [`testing.md`](testing.md) | Fakes, fixtures and live tests |
| [`settings.md`](settings.md) | `HUB_*` settings and app data folders |
| [`notices.md`](notices.md) | Third-party notices and license checks |
