import subprocess
import sys
from pathlib import Path

import pytest

from app.server import AlreadyRunning, acquire_lock


def test_lock_blocks_a_second_holder(tmp_path: Path) -> None:
    path = tmp_path / "data" / "server.lock"
    held = acquire_lock(path)
    try:
        # Another process, as a second server would be.
        code = (
            "import sys; from pathlib import Path; from app.server import acquire_lock, AlreadyRunning\n"
            "try: acquire_lock(Path(sys.argv[1]))\n"
            "except AlreadyRunning: sys.exit(3)\n"
        )
        result = subprocess.run([sys.executable, "-c", code, str(path)], cwd=Path(__file__).parent.parent)
        assert result.returncode == 3
    finally:
        held.close()
    acquire_lock(path).close()


def test_lock_is_released_when_closed(tmp_path: Path) -> None:
    path = tmp_path / "server.lock"
    acquire_lock(path).close()
    acquire_lock(path).close()


def test_lock_in_same_process_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "server.lock"
    held = acquire_lock(path)
    try:
        with pytest.raises(AlreadyRunning):
            acquire_lock(path)
    finally:
        held.close()
