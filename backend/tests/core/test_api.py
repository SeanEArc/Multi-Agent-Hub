from pathlib import Path

from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_session_lifecycle(client: TestClient, tmp_path: Path) -> None:
    created = client.post("/api/sessions", json={"title": "Try it", "project_path": str(tmp_path)})
    assert created.status_code == 201
    body = created.json()
    assert body["agent"] == "claude"
    assert body["status"] == "ready"

    session_id = body["id"]
    assert client.get(f"/api/sessions/{session_id}").json()["title"] == "Try it"
    assert [s["id"] for s in client.get("/api/sessions").json()] == [session_id]
    assert client.get(f"/api/sessions/{session_id}/events").json() == []

    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_rejects_unknown_agent(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/api/sessions", json={"title": "x", "project_path": str(tmp_path), "agent": "gemini"}
    )
    assert response.status_code == 422


def test_rejects_missing_project_path(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/api/sessions", json={"title": "x", "project_path": str(tmp_path / "nope")})
    assert response.status_code == 422
