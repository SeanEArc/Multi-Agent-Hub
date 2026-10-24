"""Minimal local authentication: one per-install bearer token, plus Host and Origin checks.

The token lives in a file only the current user can read. The Vite dev proxy (and later the
Tauri shell) reads that file and adds the header, so the browser never holds the token.
"""

import getpass
import ipaddress
import logging
import os
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


def is_loopback(host: str) -> bool:
    host = host.strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_or_create_token(path: Path) -> str:
    """Read the install's token, creating it (readable by this user only) on first run."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = ""
    if token:
        return token

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    # O_EXCL: if another process created the file first, use its token instead.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(token)
    _restrict_to_current_user(path)
    logger.info("Created API token file at %s", path)
    return token


def _restrict_to_current_user(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    # Windows ignores chmod bits: drop inherited access and grant only the current user.
    result = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("Could not restrict access to %s: %s", path, result.stdout.strip() or result.stderr.strip())


@dataclass(frozen=True)
class Caller:
    """Who made the request. Phase 6 adds other methods (Tailscale identity, passkeys)."""

    method: str


_bearer = HTTPBearer(auto_error=False, description="The token in backend/data/token")


def require_caller(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Caller:
    expected: str = request.app.state.token
    if credentials is not None and secrets.compare_digest(credentials.credentials.encode(), expected.encode()):
        return Caller(method="token")
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Missing or invalid API token",
        headers={"WWW-Authenticate": "Bearer"},
    )


class HostOriginMiddleware:
    """Rejects requests that didn't come from this machine's own pages.

    - Host must be a loopback name, which blocks DNS rebinding.
    - A browser Origin must be the API itself or one of the allowed UI origins.
    - Cross-site requests without an Origin (e.g. no-cors GETs) are rejected via Sec-Fetch-Site.
    """

    def __init__(self, app: ASGIApp, allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed_origins = {origin.rstrip("/") for origin in allowed_origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        reason = self._rejection(Headers(scope=scope), scope.get("scheme", "http"))
        if reason:
            await JSONResponse({"detail": reason}, status.HTTP_403_FORBIDDEN)(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _rejection(self, headers: Headers, scheme: str) -> str | None:
        host = headers.get("host", "")
        if not is_loopback(_hostname(host)):
            return "Host not allowed"
        origin = headers.get("origin")
        if origin is not None:
            if origin.rstrip("/") not in self.allowed_origins and origin != f"{scheme}://{host}":
                return "Origin not allowed"
        elif headers.get("sec-fetch-site") == "cross-site":
            return "Cross-site request not allowed"
        return None


def _hostname(host: str) -> str:
    """'127.0.0.1:8765' -> '127.0.0.1', '[::1]:8765' -> '::1'."""
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host
