from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """App settings. Override any field with an env var prefixed HUB_, or in backend/.env."""

    model_config = SettingsConfigDict(env_prefix="HUB_", env_file=BACKEND_DIR / ".env")

    database_url: str = f"sqlite:///{(BACKEND_DIR / 'data' / 'hub.db').as_posix()}"
    # Vite's default dev server; the React UI calls the API from here.
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    host: str = "127.0.0.1"
    port: int = 8765
    # Development only: restart the server when code changes.
    reload: bool = False
    log_level: str = "info"
    # acceptEdits: file edits run without asking; anything else needing approval is denied.
    claude_permission_mode: str = "acceptEdits"


@lru_cache
def get_settings() -> Settings:
    return Settings()
