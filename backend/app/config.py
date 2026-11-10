import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.auth import is_loopback
from app.models import AgentKind

BACKEND_DIR = Path(__file__).resolve().parent.parent
# Per-user app data, outside any repository. The desktop app and `uv run` share it.
APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "MultiAgentHub"
DATA_DIR = APP_DATA_DIR / "data"


class Settings(BaseSettings):
    """App settings. Override any field with an env var prefixed HUB_, or in backend/.env."""

    model_config = SettingsConfigDict(env_prefix="HUB_", env_file=BACKEND_DIR / ".env")

    database_url: str = f"sqlite:///{(DATA_DIR / 'hub.db').as_posix()}"
    # Vite's default dev server, and the Tauri webview's origin on Windows.
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173", "http://tauri.localhost"]
    # Loopback only. Remote access (Phase 6) goes through `tailscale serve`, which proxies to loopback.
    host: str = "127.0.0.1"
    port: int = 8765
    # Per-install API token, created on first start. The Vite dev proxy reads it from here.
    token_file: Path = DATA_DIR / "token"
    # Set by the desktop shell, which makes a fresh token each launch; the token file is then not used.
    token: SecretStr | None = None
    # Held while the server runs, so a second server can't share the database (the desktop app and
    # `uv run` use the same data folder).
    lock_file: Path = DATA_DIR / "server.lock"
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
    # Chats (sessions with no project folder) run in an empty folder here, one each. Outside any repository
    # for the same reason as worktrees_dir.
    chats_dir: Path = APP_DATA_DIR / "chats"
    # Full path to the native codex.exe, if it isn't found on PATH or inside the npm install.
    codex_path: str | None = None
    # The local Ollama server (loopback only), the model used when a session doesn't name one (None: the
    # first installed model that supports tools), and the context size sent with every request (Ollama's
    # own default, 4096 tokens, is too small for a coding session).
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str | None = None
    ollama_num_ctx: int = 32768
    # Ollama's `think` option for models that support it; None leaves it to the model.
    ollama_think: bool | None = None
    # The local model's agent loop: tool rounds allowed in one turn, and how long one command may run (seconds).
    ollama_max_rounds: int = 30
    ollama_command_timeout: int = 300
    # The local model's conversations, one JSON file each: Ollama keeps no history of its own.
    local_dir: Path = APP_DATA_DIR / "local"
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

    @field_validator("ollama_url")
    @classmethod
    def _loopback_ollama(cls, value: str) -> str:
        host = urlsplit(value).hostname
        if urlsplit(value).scheme not in ("http", "https") or not host or not is_loopback(host):
            raise ValueError(f"HUB_OLLAMA_URL must be an http URL on this PC, such as http://127.0.0.1:11434, not {value!r}")
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
