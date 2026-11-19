"""Entry point for the bundled backend (PyInstaller); `uv run python -m app.main` is the dev equivalent."""

import sys

if __name__ == "__main__":
    if sys.argv[1:2] == ["--extract-worker"]:
        # The bundle has no separate Python, so app.extract starts this exe again to make a text copy.
        from app.extract_worker import main as extract

        extract()
    else:
        from app.server import run

        run()
