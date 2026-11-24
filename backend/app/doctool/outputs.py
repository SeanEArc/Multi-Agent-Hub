"""The only place `hub-doc` writes: the session's outputs folder (`HUB_DOC_OUTPUTS`).

An agent may run `hub-doc` with any arguments (Claude has it pre-approved), and the program can write anywhere
the agent's Windows user can, so this module is the gate (probe 2026-11-21, section 2):

- `--out` is a name inside the outputs folder or an absolute path inside it; anything that resolves elsewhere
  (`..`, another session, next to an original, through a junction an agent made) is refused.
- Never a path equal to one of the command's inputs (an earlier output can be an input), never a read-only file,
  never a folder.
- A name that already exists (and is writable) is replaced: it is the agent's own earlier output (decided by the
  coordinator, 2026-11-23; the preview pane reloads on overwrite).
- With no outputs folder set, nothing is written at all.

Files are written to a staging folder inside the outputs folder first and moved into place only when the work
succeeded, so a failure or an out-of-memory abort leaves no partial file.
"""

import os
import secrets
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path

from app.doctool.contract import OUTPUTS_ENV, STAGING_PREFIX, Failed, Refused


def same_file(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.normcase(os.path.commonpath([path, root])) == os.path.normcase(root) and \
            os.path.normcase(path) != os.path.normcase(root)
    except ValueError:  # different drives
        return False


class OutputsFolder:
    def __init__(self, raw: str | None) -> None:
        self.root: Path | None = None
        if raw and raw.strip():
            root = Path(os.path.realpath(raw.strip()))
            if root.is_dir():
                self.root = root
        self.raw = raw

    @classmethod
    def from_env(cls) -> "OutputsFolder":
        return cls(os.environ.get(OUTPUTS_ENV))

    def require(self) -> Path:
        """The outputs folder, or Refused when there is none (nothing may be written)."""
        if self.root is None:
            where = f" ({self.raw} doesn't exist)" if self.raw else ""
            raise Refused(f"hub-doc has no outputs folder{where}, so it can't write files. It writes only to the "
                          f"session's outputs folder, which the hub sets in {OUTPUTS_ENV}.")
        return self.root

    def target(self, out: str | None, default_name: str, inputs: Sequence[str | Path],
               suffixes: Sequence[str]) -> Path:
        """The checked absolute path for `--out` (or `default_name` when it's missing). Raises Refused or Failed."""
        root = self.require()
        name = (out or default_name).strip()
        if not name:
            raise Failed("--out needs a file name.")
        candidate = Path(name)
        if not candidate.is_absolute():
            candidate = root / name
        # realpath follows junctions and symbolic links in the parts that exist, so a link inside outputs that
        # points elsewhere resolves to where it really leads.
        resolved = Path(os.path.realpath(candidate))
        if not _inside(resolved, root) or ":" in str(resolved.relative_to(root)):
            raise Refused(f"{name} is outside the outputs folder. hub-doc writes only inside {root}; give "
                          "--out a file name such as \"report (edited).docx\".")
        if any(part.startswith(STAGING_PREFIX) for part in resolved.relative_to(root).parts):
            raise Refused(f"{name}: names starting with {STAGING_PREFIX} are hub-doc's own.")
        if not valid_name(resolved.relative_to(root)):
            # Found before the work, not by the final rename after it (`a?.csv`), and never a device name such as
            # NUL.png, which older Windows (Windows 10, Server 2022) still opens as the device.
            raise Failed(f"--out {name} isn't a valid Windows file name (no < > \" | ? * or control characters, "
                         "no device names such as CON or NUL, no space or dot at the end).")
        if suffixes and resolved.suffix.lower() not in suffixes:
            raise Failed(f"--out {name}: the file name must end in {' or '.join(suffixes)}.")
        for source in inputs:
            if same_file(source, resolved):
                raise Refused(f"{name} is one of this command's inputs; hub-doc never writes over its input. "
                              "Choose a new name.")
        if resolved.exists():
            if resolved.is_dir():
                raise Refused(f"{name} is a folder.")
            if not resolved.stat().st_mode & stat.S_IWRITE:
                raise Refused(f"{name} is read-only; hub-doc never changes a read-only file. Choose a new name.")
        return resolved


def valid_name(relative: Path) -> bool:
    """Whether every part of a path inside the outputs folder is a name Windows can create as a plain file."""
    if os.name != "nt":
        return True
    text = str(relative)
    if any(c in '<>"|?*' or ord(c) < 32 for c in text):
        return False
    return not os.path.isreserved(text)


class Staging:
    """A private folder inside outputs for one command's files; `commit` moves them into place."""

    def __init__(self, folder: OutputsFolder) -> None:
        assert folder.root is not None
        self.root = folder.root
        self.path = folder.root / f"{STAGING_PREFIX}{secrets.token_hex(6)}"
        self.path.mkdir()

    def file(self, suffix: str) -> Path:
        return self.path / f"{secrets.token_hex(6)}{suffix}"

    def commit(self, staged: Path, target: Path) -> Path:
        """Move a finished file to its checked target, replacing an earlier output of the same name."""
        if not staged.is_file() or not staged.resolve().is_relative_to(self.path.resolve()):
            raise Failed("hub-doc's worker didn't write the file it reported.")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Checked again right before the move: the work can take minutes, and a junction made meanwhile in place of
        # a folder on the way (or of the outputs folder itself) would otherwise take the file out of outputs. This
        # narrows the window to the rename itself; it is not a sandbox (an agent with a shell can write anyway).
        final = Path(os.path.realpath(target))
        if not _inside(final, self.root) or os.path.normcase(final) != os.path.normcase(target):
            raise Refused(f"{target.name}: its folder changed while hub-doc worked (it now leads outside the outputs "
                          "folder), so nothing was written.")
        try:
            os.replace(staged, target)
        except PermissionError:
            raise Refused(f"{target.name} couldn't be replaced: it is read-only or open in another program.") from None
        return target

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
