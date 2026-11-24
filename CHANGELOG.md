# Changelog

All notable changes to Multi-Agent Hub are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/) (0.x while the app and its data format still change).

Add what you change under **Unreleased** as you go. When releasing, rename it to the new version
(`## [0.2.0] - YYYY-MM-DD`), start a fresh Unreleased section, and tag the release on `main`; the release
workflow uses that section as the release notes and refuses a tag without one. See `RELEASING.md`.

## [Unreleased]

### Added
- CI on every pull request and push to `staging` and `main`: backend tests on Windows, UI typecheck and
  build, and a check that every file has the same version. A push to `staging` that passes moves `main`
  forward to it.
- Release workflow: a tag like `v0.2.0` on `main` builds the Windows installer and, after approval,
  publishes it as a GitHub Release with notes from this file.
- The backend copies `hub.db` to `data\backups` before applying database migrations, keeping the last 5.
- `SECURITY.md` (private vulnerability reporting) and `CONTRIBUTING.md`.
- Claude's own commands in the `/` menu: every command Claude Code supports without a terminal, such as
  `/context`, `/usage` and `/code-review`, is sent to Claude as typed. A "Conversation compacted" row shows
  the token drop, including when Claude compacts on its own.
- Codex commands: `/compact`, `/context`, `/usage`, `/status`, `/mcp` and `/code-review` (also `/review`,
  with optional focus text) run through Codex's own app server, so a review's findings stay in Codex's
  conversation and "fix those" works next. `/diff` shows the session's changes and `/init` asks Codex for
  an `AGENTS.md`.
- Local model commands: `/compact` has the local model summarize its own conversation (the hub's summary if it
  can't), with **View summary** on the "Conversation compacted" row; `/context` shows how full its context is;
  `/usage` says it has no limits; `/code-review` asks it to review the session's git changes.
- Attachments: attach images (PNG, JPEG, GIF, WebP), PDF, Word, Excel, CSV, text and code files with the
  paperclip button, by pasting a screenshot, or by dropping files on the window. Claude sees images and PDFs
  directly, Codex sees images, and the local model sees images when it has vision; every agent can open the
  files, and the hub makes a plain-text copy of PDF, Word, Excel and CSV files for them. The transcript shows
  images inline and opens other files with a click. After a handoff the next agent gets the list of files and
  the recent images again. Files are kept in `%LOCALAPPDATA%\MultiAgentHub\attachments` and deleted with
  the session.
- Working on documents: every agent gets `hub-doc`, the hub's document toolkit. Agents fix Excel formulas in a
  recalculated copy that lists the errors left (charts kept), read sheets and Word files, review a Word file's
  formatting from a report (styles, fonts, spacing, lists, tracked changes, inconsistencies), replace Word text
  without losing its formatting, and list changed paragraphs. With Microsoft Office, Word page images and a
  tracked-changes copy of an agent's edits. Agents write only to the session's outputs folder; its files show
  above the text box with **Open** and **Save as…**. Settings shows whether Microsoft Office was found.
- Preview pane: click an attached file, or **Preview** on a file from an agent, to see it beside the chat
  without leaving the hub. PDF, Word (with tracked changes as stored) and Excel (`.xlsx`) open in built-in
  viewers that work offline; images, text, CSV and code show as they are; anything else (such as `.xls`) shows
  the hub's text copy with **Open**. When an agent overwrites the file, the pane shows the new version. Links in
  a document open in your browser.

### Changed
- README rewritten for people installing the app with their own accounts. Development notes moved to `docs/`.
- `/compact` compacts the agent's own conversation (Claude's and Codex's own compaction, the local model's
  own summary), so the conversation continues instead of starting fresh from the hub's summary. `/clear`, `/model` and `/rename` answer with where to
  find that in the hub.
- Codex offers four reasoning efforts, as in Codex itself: Light, Medium, High and Extra high. A Max
  effort (from Settings' default or a switch) reaches Codex as Extra high.

### Fixed
- Handoff tests that sometimes failed with "session still working": the tests shared one database
  connection between threads, so a status update could be lost. Tests now use a database file each, as the
  app does; the app itself wasn't affected.

## [0.1.0] - 2026-11-12

First version: Claude Code, Codex and a local Ollama model as parallel sessions, with handoff on usage
limits, per-session git worktrees, history and summaries, a skills library and `/` commands, and the
Windows desktop app. Not released as an installer.
