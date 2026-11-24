"""The reply contract shared by `hub-doc`'s parent and child: exit codes, errors and settings from the environment."""

import os

OK, REFUSED, FAILED = 0, 1, 2
OUTPUTS_ENV = "HUB_DOC_OUTPUTS"
MEMORY_ENV = "HUB_DOC_MEMORY_LIMIT"
# The folders hub-doc may read (os.pathsep-separated), set by the hub: the session's attachments, its outputs and its
# working folder. Unset (hub-doc run by hand outside the hub): any file.
READ_ROOTS_ENV = "HUB_DOC_READ_ROOTS"
# The work's memory cap. BetterOffice peaked at 248 MB committed on a 20,000-row workbook with 60,000 formulas
# (probe 2026-11-21), so 2 GB leaves room for real workbooks while a zip bomb or a runaway file still stops.
DEFAULT_MEMORY_LIMIT = 2 * 1024**3
# Below this even Python and the libraries can't start; a lower value is raised to it.
MIN_MEMORY_LIMIT = 64 * 1024**2
# The longest one command may run before the parent stops it.
WORK_TIMEOUT = 600.0
# Passed as the first argument when the parent starts its child.
CHILD_FLAG = "--hub-doc-child"
# Files and folders the parent stages in the outputs folder while a command runs (the outputs list hides them).
STAGING_PREFIX = ".~hub-doc-"


class HubDocError(Exception):
    code = FAILED


class Refused(HubDocError):
    """Something hub-doc won't do (exit 1): a write outside the outputs folder, a Word paragraph it would damage."""

    code = REFUSED


class Failed(HubDocError):
    """Something that went wrong (exit 2): a missing or unreadable file, bad arguments, out of memory."""

    code = FAILED


def memory_limit() -> int:
    """The cap for this run: `HUB_DOC_MEMORY_LIMIT` may lower the default, never raise it."""
    raw = os.environ.get(MEMORY_ENV, "").strip()
    try:
        asked = int(raw) if raw else DEFAULT_MEMORY_LIMIT
    except ValueError:
        asked = DEFAULT_MEMORY_LIMIT
    return max(MIN_MEMORY_LIMIT, min(asked, DEFAULT_MEMORY_LIMIT))


def read_roots() -> list[str] | None:
    """The folders hub-doc may read, or None when `HUB_DOC_READ_ROOTS` isn't set at all (hub-doc run by hand: any
    file). Set but empty, or holding only relative parts, means nothing may be read: the hub always sets absolute
    folders, and a relative one would depend on the agent's current folder."""
    raw = os.environ.get(READ_ROOTS_ENV)
    if raw is None:
        return None
    return [part.strip() for part in raw.split(os.pathsep) if part.strip() and os.path.isabs(part.strip())]


def short(text: object, limit: int = 400) -> str:
    message = " ".join(str(text).split())
    return message if len(message) <= limit else message[: limit - 1] + "…"
