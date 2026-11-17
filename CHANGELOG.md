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

### Changed
- README rewritten for people installing the app with their own accounts. Development notes moved to `docs/`.
- `/compact` in a Claude chat uses Claude's own compaction, so the conversation continues instead of
  starting fresh from the hub's summary. `/clear`, `/model` and `/rename` answer with where to find that
  in the hub.
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
