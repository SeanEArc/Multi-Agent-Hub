"""Entry point for the bundled backend (PyInstaller); `uv run python -m app.main` is the dev equivalent."""

from app.server import run

if __name__ == "__main__":
    run()
