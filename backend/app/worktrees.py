"""Git worktrees: each session works in its own folder on its own branch.

Two sessions on the same repository then never edit the same files, and the
user's own checkout is left alone. Worktrees live under one hub folder
(HUB_WORKTREES_DIR), outside the hub's repository: Claude Code reads every
CLAUDE.md in the folders above where it runs. Removing a worktree deletes the
folder and keeps the branch for the user to merge, unless the branch has no
commits of its own.

Not a sandbox: an agent can still read and write outside its worktree (phase 9).
"""

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.adapters.cli import HEADLESS_GIT_ENV

GIT_TIMEOUT = 60.0
BRANCH_PREFIX = "hub/"


class WorktreeError(RuntimeError):
    pass


@dataclass
class RepoInfo:
    root: Path  # the repository's top level
    subdir: str  # the folder relative to `root`, "" at the top level
    head: str | None  # None for a repository with no commits
    branch: str | None  # None on a detached HEAD
    changes: list[str]  # `git status --short` lines


@dataclass
class CreatedWorktree:
    path: Path
    branch: str
    base_commit: str


async def run_git(cwd: Path, *args: str) -> str:
    """Output of a git command; raises WorktreeError with git's message if it fails."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "core.quotepath=false", *args,
            cwd=cwd,
            env={**os.environ, **HEADLESS_GIT_ENV},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise WorktreeError(f"Could not run git: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise WorktreeError(f"git {args[0]} timed out") from None
    if proc.returncode != 0:
        message = stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed"
        raise WorktreeError(message)
    return stdout.decode("utf-8", "replace")


async def repo_info(folder: Path) -> RepoInfo | None:
    """The git repository `folder` is in, or None if it isn't in one (or git is missing)."""
    try:
        root = (await run_git(folder, "rev-parse", "--show-toplevel")).strip()
    except WorktreeError:
        return None
    root_path = Path(root).resolve()
    subdir = Path(folder).resolve().relative_to(root_path).as_posix()
    head = await _optional(folder, "rev-parse", "--verify", "--quiet", "HEAD")
    branch = await _optional(folder, "symbolic-ref", "--short", "--quiet", "HEAD")
    status = await run_git(root_path, "status", "--short")  # paths relative to the top level
    return RepoInfo(
        root=root_path,
        subdir="" if subdir == "." else subdir,
        head=head,
        branch=branch,
        changes=status.splitlines(),
    )


async def create_worktree(worktrees_dir: Path, repo: RepoInfo, session_id: str, title: str) -> CreatedWorktree:
    """A new worktree for a session, branched from the repository's current HEAD."""
    if repo.head is None:
        raise WorktreeError("The repository has no commits yet, so there is nothing to branch from. "
                            "Make a first commit, or run the session without a worktree.")
    short_id = session_id[:8]
    path = Path(worktrees_dir) / f"{_slug(repo.root.name, 40)}-{short_id}"
    branch = f"{BRANCH_PREFIX}{_slug(title, 40)}-{short_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    await run_git(repo.root, "worktree", "add", "-b", branch, str(path), repo.head)
    return CreatedWorktree(path=path, branch=branch, base_commit=repo.head)


async def worktree_changes(path: Path) -> list[str]:
    """Uncommitted changes in the worktree, as `git status --short` lines."""
    return (await run_git(path, "status", "--short")).splitlines()


async def commit_all(path: Path, message: str) -> None:
    await run_git(path, "add", "--all")
    await run_git(path, "commit", "--quiet", "--message", message)


async def remove_worktree(repo_path: Path, path: Path, branch: str, base_commit: str, force: bool) -> bool:
    """Delete the worktree folder. Returns whether the branch was kept (False if it had no commits of its own)."""
    if Path(path).exists():
        args = ["worktree", "remove", *(["--force"] if force else []), str(path)]
        await run_git(repo_path, *args)
    else:
        # The user deleted the folder; drop git's record of it.
        await run_git(repo_path, "worktree", "prune")
    own_commits = await _optional(repo_path, "rev-list", "--count", f"{base_commit}..{branch}")
    if own_commits == "0":
        await run_git(repo_path, "branch", "--delete", "--force", branch)
        return False
    return own_commits is not None


async def _optional(cwd: Path, *args: str) -> str | None:
    try:
        return (await run_git(cwd, *args)).strip() or None
    except WorktreeError:
        return None


def _slug(text: str, limit: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-") or "session"
