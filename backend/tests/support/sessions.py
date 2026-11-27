"""Driving sessions through the API: create one, wait for its turn, read its events; and git for test repos."""

import json
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient


def create(client: TestClient, path: Path, mode: str, agent: str = "claude", **extra) -> str:
    body = {"title": mode, "project_path": str(path), "agent": agent, **extra}
    response = client.post("/api/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def wait_until_idle(client: TestClient, session_id: str, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/sessions/{session_id}").json()
        # handing_off is busy too: an automatic handoff passes through it between two turns.
        if body["status"] not in ("working", "handing_off"):
            return body
        time.sleep(0.05)
    events = client.get(f"/api/sessions/{session_id}/events").json()
    trail = "\n".join(f"  {e['created_at']} {e['agent']} {e['type']}" for e in events)
    stints = [(s["agent"], s["reason"], s["ended_at"]) for s in body["stints"]]
    raise AssertionError(f"session still {body['status']} after {timeout} s; stints {stints}; events:\n{trail}")


def event_types(client: TestClient, session_id: str) -> list[str]:
    return [e["type"] for e in client.get(f"/api/sessions/{session_id}/events").json()]


def events_of(client: TestClient, session_id: str) -> list[dict]:
    return client.get(f"/api/sessions/{session_id}/events").json()


def prompts_sent(client: TestClient, session_id: str, agent: str) -> list[str]:
    """What the fake CLIs received over stdin, from the text they echo back."""
    prompts = []
    for e in events_of(client, session_id):
        if e["type"] != "message" or e["agent"] != agent:
            continue
        text = e["payload"]["text"]
        prompts.append(text.removeprefix("echo: ") if agent == "claude" else json.loads(text)["prompt"])
    return prompts


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
