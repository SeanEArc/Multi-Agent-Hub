from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.auth import is_loopback

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """App settings. Override any field with an env var prefixed HUB_, or in backend/.env."""

    model_config = SettingsConfigDict(env_prefix="HUB_", env_file=BACKEND_DIR / ".env")

    database_url: str = f"sqlite:///{(BACKEND_DIR / 'data' / 'hub.db').as_posix()}"
    # Vite's default dev server; the React UI calls the API from here.
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Loopback only. Remote access (Phase 6) goes through `tailscale serve`, which proxies to loopback.
    host: str = "127.0.0.1"
    port: int = 8765
    # Per-install API token, created on first start. The Vite dev proxy reads it from here.
    token_file: Path = BACKEND_DIR / "data" / "token"
    # Development only: restart the server when code changes.
    reload: bool = False
    log_level: str = "info"
    # acceptEdits: file edits run without asking; anything else needing approval is denied.
    claude_permission_mode: str = "acceptEdits"

    @field_validator("host")
    @classmethod
    def _loopback_host(cls, value: str) -> str:
        if not is_loopback(value):
            raise ValueError(f"HUB_HOST must be a loopback address such as 127.0.0.1, not {value!r}")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
