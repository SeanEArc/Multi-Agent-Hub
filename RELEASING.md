# Releasing

How changes reach `main`, how a version is released, and the one-time GitHub setup that makes it work.

## Branches

```
feature branch ──PR──► staging ──(CI passes)──► main ──(tag vX.Y.Z)──► GitHub Release
```

- Work happens on feature branches (`revision/...`, `fix/...`, the hub's own `hub/...`). Open a pull
  request into **`staging`**, never into `main`.
- CI (`.github/workflows/ci.yml`) runs on every pull request and every push to `staging` and `main`:
  backend tests on Windows, the UI typecheck and build, and `scripts/version.py check`.
- When a push to `staging` passes, CI's `promote` job fast-forwards `main` to that exact commit. `main`
  never gets merge commits of its own and only holds commits that passed.
- If `promote` fails with "main has commits that staging doesn't", someone pushed to `main` directly.
  Merge `main` into `staging` and push `staging` again.

## Releasing a version

1. On a branch off `staging`, set the version everywhere and write the notes:
   ```powershell
   python scripts/version.py set 0.2.0      # or: uv run --project backend python scripts/version.py set 0.2.0
   ```
   In `CHANGELOG.md`, rename **Unreleased** to `## [0.2.0] - YYYY-MM-DD` and start a new empty
   Unreleased section above it.
2. Save a sample database at this version's schema, so later versions are tested upgrading from it:
   ```powershell
   cd backend; uv run python scripts/snapshot_release_db.py 0.2.0
   ```
3. Before shipping, run the live tests on your own machine with your own logins (never in CI). They
   check that the hub still understands the installed Claude Code and Codex CLIs:
   ```powershell
   cd backend
   $env:HUB_LIVE_CLAUDE="1"; $env:HUB_LIVE_CODEX="1"; $env:HUB_LIVE_OLLAMA="1"
   uv run pytest tests/test_claude_live.py tests/test_codex_live.py tests/test_ollama_live.py
   ```
   Note the CLI versions you tested in the changelog entry.
4. Merge the branch into `staging` and wait for CI to promote it to `main`.
5. Tag `main` and push the tag:
   ```powershell
   git fetch origin; git tag v0.2.0 origin/main; git push origin v0.2.0
   ```
6. The release workflow (`.github/workflows/release.yml`) checks the tag (on `main`, CI passed, versions
   match, changelog notes and sample database present), builds the installer, then waits for your
   approval in the `release` environment. Download the installer from the run's **Artifacts** and try
   it, then approve. The GitHub Release is published with the installer and the changelog notes.

A tag with a hyphen (`v0.3.0-beta.1`) is published as a prerelease: a beta channel for testers.

**Undoing a bad release:** you can't take back an installed version, so release a fixed one with a higher
version number. Mark the bad release as a prerelease or delete it on GitHub so new users don't get it.
Before each migration the app copies `hub.db` to `%LOCALAPPDATA%\MultiAgentHub\data\backups` (the last 5
are kept), so a user can go back to an older version by restoring the copy named after that version's
database revision. Downgrading without restoring a copy isn't supported.

## Database changes

Migrations run on every user's real database when they upgrade, so:
- Only add: new tables, `ALTER TABLE ... ADD COLUMN`. Never batch-rebuild `sessions` or `stints` (with
  foreign keys on, the rebuild cascades and deletes child rows) and never drop a history table.
  `tests/test_release_upgrades.py` fails the build if a migration does either.
- Every released schema has a dump in `backend/tests/release_dbs/`; CI upgrades each one to the newest
  schema and checks that no row was lost.

## One-time GitHub setup

These live in the repository settings, not in files:

1. **Create `staging`** from `main` and push it (`git push origin main:staging`).
2. **Settings → Rules → Rulesets → New branch ruleset** for `staging`:
   - Enforcement: Active. Target: `staging`.
   - Require a pull request before merging (0 approvals is fine while you are the only maintainer).
   - Require status checks to pass: `Versions match`, `Backend tests (Windows)`, `UI typecheck and build`
     (they appear in the list after CI has run once).
   - Block force pushes. Restrict deletions.
   - Bypass list: add yourself (Repository admin) if you want to push small fixes straight to `staging`;
     CI still runs on the push.
3. **Another ruleset** for `main`:
   - Target: `main`. Require status checks: the same three. Block force pushes. Restrict deletions.
   - Don't require a pull request on `main`: the `promote` job pushes the already-tested commit directly.
   - If the first promotion is refused, add **GitHub Actions** to the ruleset's bypass list.
4. **Settings → Environments → New environment** `release`: add yourself under **Required reviewers**,
   and under **Deployment branches and tags** allow only tags matching `v*`.
5. **Settings → Actions → General**: Workflow permissions "Read repository contents" (the workflows ask
   for more only where needed), and for outside contributors "Require approval for first-time
   contributors".
6. **Settings → Code security**: turn on Dependabot alerts and security updates (version updates are
   configured in `.github/dependabot.yml`, against `staging`), secret scanning and push protection.

## Later

- **Code signing**, so Windows SmartScreen stops warning about an unknown publisher (Azure Trusted
  Signing, or SignPath's free signing for open-source projects). The signing key would live only in the
  `release` environment's secrets.
- **Auto-update** with Tauri's updater plugin, which reads a `latest.json` from GitHub Releases and needs
  its own signing key pair (also a `release` environment secret).
