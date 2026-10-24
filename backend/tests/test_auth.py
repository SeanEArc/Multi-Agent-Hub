import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import Engine

from app.auth import is_loopback, load_or_create_token
from app.config import Settings
from app.main import create_app

from conftest import TEST_TOKEN, fake_adapter_factory, make_client


@pytest.fixture
def anonymous(engine: Engine) -> TestClient:
    with make_client(create_app(engine, fake_adapter_factory, token=TEST_TOKEN), token=None) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/health"),
        ("GET", "/api/sessions"),
        ("POST", "/api/sessions"),
        ("GET", "/api/sessions/x/events"),
        ("GET", "/api/sessions/x/stream"),
        ("POST", "/api/sessions/x/messages"),
        ("GET", "/api/folders"),
    ],
)
def test_every_route_needs_the_token(anonymous: TestClient, method: str, path: str) -> None:
    response = anonymous.request(method, path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_rejected(anonymous: TestClient) -> None:
    assert anonymous.get("/api/health", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert anonymous.get("/api/health", headers={"Authorization": f"Basic {TEST_TOKEN}"}).status_code == 401


def test_right_token_is_accepted(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("host", ["evil.example:8765", "192.168.1.5:8765", "127.0.0.1.evil.example"])
def test_non_loopback_host_is_rejected(client: TestClient, host: str) -> None:
    # DNS rebinding: a hostile domain resolving to 127.0.0.1 still sends its own name as Host.
    response = client.get("/api/health", headers={"Host": host})
    assert response.status_code == 403
    assert response.json()["detail"] == "Host not allowed"


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1", "[::1]:8765"])
def test_loopback_hosts_are_accepted(client: TestClient, host: str) -> None:
    assert client.get("/api/health", headers={"Host": host}).status_code == 200


def test_foreign_origin_is_rejected_even_with_token(client: TestClient) -> None:
    response = client.post("/api/sessions", json={}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Origin not allowed"


@pytest.mark.parametrize("origin", ["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8765"])
def test_ui_and_own_origins_are_accepted(client: TestClient, origin: str) -> None:
    assert client.get("/api/health", headers={"Origin": origin}).status_code == 200


def test_cross_site_request_without_origin_is_rejected(client: TestClient) -> None:
    response = client.get("/api/folders", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403


def test_token_file_is_created_once_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "data" / "token"
    token = load_or_create_token(path)
    assert len(token) >= 40
    assert path.read_text(encoding="utf-8") == token
    assert load_or_create_token(path) == token


def test_token_survives_a_restart(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "token"
    monkeypatch.setattr("app.main.get_settings", lambda: Settings(token_file=path))
    with make_client(create_app(engine, fake_adapter_factory), token=None) as test_client:
        token = path.read_text(encoding="utf-8")
        headers = {"Authorization": f"Bearer {token}"}
        assert test_client.get("/api/health", headers=headers).status_code == 200


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_token_file_is_private_on_windows(tmp_path: Path) -> None:
    import subprocess

    path = tmp_path / "token"
    load_or_create_token(path)
    acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    # Inheritance removed: only the explicit grant for the current user is left.
    assert "(I)" not in acl
    assert "Users" not in acl.replace(str(path), "")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_loopback_bind_addresses_are_allowed(host: str) -> None:
    assert Settings(host=host).host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com"])
def test_non_loopback_bind_address_is_refused(host: str) -> None:
    assert not is_loopback(host)
    with pytest.raises(ValidationError, match="loopback"):
        Settings(host=host)
