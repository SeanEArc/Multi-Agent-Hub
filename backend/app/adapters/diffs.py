"""Edits as unified diffs, the same shape for every agent, so the UI renders one kind of diff.

A tool result that edited files carries `diffs`: a list of
`{"path", "kind": "add"|"update"|"delete", "diff": str | None, "base": ..., "truncated": bool}`.
`diff` holds only hunks (`@@ -a,b +c,d @@` and their lines), no `---`/`+++` header, and is
None when the hub couldn't tell what changed (a binary or very large file, or no earlier copy).

Claude reports a structured patch for each edit, so its diffs come from the stream. Codex
reports only the paths and kinds of a patch, so `FileSnapshots` works the diff out from the
file contents: against the copy it saw after the previous edit, or the last commit the first
time it sees a file (`base` says which).
"""

import difflib
import os
import subprocess
from pathlib import Path
from typing import Any

from app.adapters.cli import HEADLESS_GIT_ENV

# Diffs are saved in the event, so a huge rewrite is cut instead of bloating the database.
MAX_DIFF_CHARS = 40_000
# Larger files are not read at all.
MAX_FILE_BYTES = 1_000_000
CONTEXT_LINES = 3


def hunks(old: str, new: str) -> str:
    """The unified diff of two texts, hunks only."""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), n=CONTEXT_LINES, lineterm="")
    # The first two lines are the ---/+++ header.
    return "\n".join(list(lines)[2:])


def from_structured_patch(patch: list[dict[str, Any]]) -> str:
    """Claude Code's `structuredPatch` (hunks with start lines, counts and +/-/space lines) as unified diff text."""
    out = []
    for hunk in patch:
        out.append(
            f"@@ -{hunk.get('oldStart', 0)},{hunk.get('oldLines', 0)} "
            f"+{hunk.get('newStart', 0)},{hunk.get('newLines', 0)} @@"
        )
        out.extend(str(line) for line in hunk.get("lines") or [])
    return "\n".join(out)


def file_diff(path: str, kind: str, diff: str | None, base: str | None = None) -> dict[str, Any]:
    truncated = diff is not None and len(diff) > MAX_DIFF_CHARS
    return {
        "path": path,
        "kind": kind,
        "diff": diff[:MAX_DIFF_CHARS] if truncated else diff,
        "base": base,
        "truncated": truncated,
    }


def claude_diffs(result: Any) -> list[dict[str, Any]] | None:
    """The diff of a Claude Edit, MultiEdit or Write, from the `tool_use_result` its stream reports."""
    if not isinstance(result, dict) or not isinstance(result.get("filePath"), str):
        return None
    path = result["filePath"]
    if result.get("type") == "create" and isinstance(result.get("content"), str):
        return [file_diff(path, "add", hunks("", result["content"]))]
    patch = result.get("structuredPatch")
    if isinstance(patch, list) and patch:
        return [file_diff(path, "update", from_structured_patch(patch))]
    return None


class FileSnapshots:
    """Remembers each file's contents after the hub saw it change, to diff the next change against.

    One per adapter, so it lasts for the session while the backend runs. After a restart the
    first diff of a file is against the last commit again.
    """

    def __init__(self, cwd: Path) -> None:
        self.cwd = Path(cwd)
        self._seen: dict[str, str | None] = {}
        self._repo_root: Path | None | bool = False  # False: not looked up yet

    def capture(self, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Diffs for a patch's `[{path, kind}]`, read from disk right after it was applied."""
        diffs = []
        for change in changes:
            path, kind = change.get("path"), change.get("kind") or "update"
            if not isinstance(path, str) or not path:
                continue
            file = Path(path) if Path(path).is_absolute() else self.cwd / path
            after = None if kind == "delete" else _read_text(file)
            if path in self._seen:
                before, base = self._seen[path], "previous_edit"
            elif kind == "add":
                before, base = "", None
            else:
                before, base = self._committed(file), "commit"
            self._seen[path] = after
            if before is None or (after is None and kind != "delete"):
                diffs.append(file_diff(path, kind, None))
            else:
                diffs.append(file_diff(path, kind, hunks(before, after or ""), base))
        return diffs

    def _committed(self, path: Path) -> str | None:
        """The file as of the last commit, or None outside a git repository or for an untracked file."""
        root = self._root()
        if root is None:
            return None
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except ValueError:
            return None
        shown = _git(root, "show", f"HEAD:{relative}")
        if shown is None or len(shown) > MAX_FILE_BYTES or b"\0" in shown:
            return None
        return shown.decode("utf-8", "replace")

    def _root(self) -> Path | None:
        if self._repo_root is False:
            top = _git(self.cwd, "rev-parse", "--show-toplevel")
            self._repo_root = Path(top.decode().strip()).resolve() if top else None
        return self._repo_root  # type: ignore[return-value]


def _git(cwd: Path, *args: str) -> bytes | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            timeout=10,
            env={**os.environ, **HEADLESS_GIT_ENV},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _read_text(path: Path) -> str | None:
    """A text file's contents; None if it's missing, binary or too large to diff."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    return data.decode("utf-8", "replace")
