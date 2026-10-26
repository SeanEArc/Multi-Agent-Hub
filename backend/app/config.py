import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.auth import is_loopback
from app.models import AgentKind

BACKEND_DIR = Path(__file__).resolve().parent.parent
# Per-user app data, outside any repository (the Tauri shell will keep its data here too).
APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "MultiAgentHub"


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
    # workspace-write: Codex may edit the project folder (and %TEMP%); read-only or danger-full-access also work.
    codex_sandbox: str = "workspace-write"
    # Sessions' git worktrees, one folder each. Keep it outside any repository: Claude Code reads the
    # CLAUDE.md files in every folder above the one it runs in.
    worktrees_dir: Path = APP_DATA_DIR / "worktrees"
    # Full path to the native codex.exe, if it isn't found on PATH or inside the npm install.
    codex_path: str | None = None
    # Development only: agents (e.g. "claude" or "claude,codex") whose turns end as if the usage limit was
    # reached, so handoff can be tried with the real CLIs. The turn still runs and uses real usage.
    simulate_limit: Annotated[list[AgentKind], NoDecode] = []

    @field_validator("simulate_limit", mode="before")
    @classmethod
    def _comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return value

    @field_validator("host")
    @classmethod
    def _loopback_host(cls, value: str) -> str:
        if not is_loopback(value):
            raise ValueError(f"HUB_HOST must be a loopback address such as 127.0.0.1, not {value!r}")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
