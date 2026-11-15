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

### Changed
- README rewritten for people installing the app with their own accounts. Development notes moved to `docs/`.

## [0.1.0] - 2026-11-12

First version: Claude Code, Codex and a local Ollama model as parallel sessions, with handoff on usage
limits, per-session git worktrees, history and summaries, a skills library and `/` commands, and the
Windows desktop app. Not released as an installer.
