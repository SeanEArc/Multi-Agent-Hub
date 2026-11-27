"""Attachments: stored files, uploads through the API, and what the fake CLIs echo back."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.base import AgentEvent, EventType
from app.attachments import Attachment, classify


def collect(events: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    async def run() -> list[AgentEvent]:
        return [event async for event in events]

    return asyncio.run(run())


def stored(folder: Path, name: str, data: bytes, **extra) -> Attachment:
    kind, mime = classify(name)
    attachment_id = uuid.uuid4().hex[:12]
    path = folder / attachment_id / name
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return Attachment(id=attachment_id, name=name, mime=mime, size=len(data), path=str(path),
                      text_path=extra.pop("text_path", None), kind=kind, **extra)


def codex_echo(events: list[AgentEvent]) -> dict:
    return json.loads([e for e in events if e.type is EventType.MESSAGE][-1].data["text"])


def last_echo(client: TestClient, session_id: str) -> dict:
    events = client.get(f"/api/sessions/{session_id}/events").json()
    return json.loads([e for e in events if e["type"] == "message"][-1]["payload"]["text"])


def echoed(events: list[AgentEvent]) -> dict:
    assert events[-1].type is EventType.COMPLETED, events[-1].data
    return json.loads(events[-1].data["text"])


def upload(client: TestClient, session_id: str, name: str, data: bytes) -> dict:
    response = client.post(f"/api/sessions/{session_id}/attachments", params={"name": name}, content=data)
    assert response.status_code == 201, response.text
    return response.json()
