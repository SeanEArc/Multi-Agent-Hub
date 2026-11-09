"""Starts the API server: `uv run python -m app.main`, or the desktop app's bundled backend."""

import logging
import os
import sys
from pathlib import Path
from typing import IO

from app.config import get_settings

logger = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    pass


def acquire_lock(path: Path) -> IO[str]:
    """Take an exclusive lock on `path` for as long as the returned file stays open.

    The OS drops the lock when the process ends, even after a crash, so a stale file never blocks.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AlreadyRunning(
            f"Another Multi-Agent Hub server is already using this data folder ({path.parent}). "
            "Close the desktop app or the other server first."
        ) from None
    return handle


def run() -> None:
    import uvicorn

    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        lock = acquire_lock(settings.lock_file)
    except AlreadyRunning as error:
        logger.error("%s", error)
        sys.exit(3)
    try:
        uvicorn.run(
            "app.main:app",
            host=settings.host,
            port=settings.port,
            reload=settings.reload and not getattr(sys, "frozen", False),
            log_level=settings.log_level,
        )
    finally:
        lock.close()
