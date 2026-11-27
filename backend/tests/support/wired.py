"""The hub's real wiring (`default_adapter_factory`) with only the CLI programs swapped for the fakes at the very
end, a loopback server that answers like Ollama, and checks on what each agent was launched with. The `wired`
fixture that builds it is in conftest.py."""

import asyncio
import inspect
import json
import os
import sys
import threading
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx2
from fastapi.testclient import TestClient

from app.adapters.runner import HostRunner, RunningProcess
from app.main import create_app

from support import fake_ollama
from support.attachments import upload
from support.sessions import wait_until_idle


def same_path(a: object, b: object) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def optional_seams(**candidates: Any) -> dict[str, Any]:
    """Keyword arguments `create_app` accepts once the engineer adds them; never passed before (no TypeError)."""
    accepted = inspect.signature(create_app).parameters
    return {name: value for name, value in candidates.items() if name in accepted}


@dataclass
class Launch:
    command: list[str]  # what the hub asked for (program first)
    env: dict[str, str]  # what the hub added to the environment
    effective_env: dict[str, str]  # what the process got
    existing_outputs: set[str]  # outputs folders that existed when the process started


class SwapProgramRunner(HostRunner):
    """Runs the fake CLI in place of the program the hub chose, and records each launch."""

    def __init__(self, fake: Path, mode: str, attachments: Path, launches: list[Launch]) -> None:
        self.fake, self.mode, self.attachments, self.launches = fake, mode, attachments, launches

    async def start(self, command: Sequence[str], cwd: Path, env: Mapping[str, str]) -> RunningProcess:
        existing = {os.path.normcase(str(p)) for p in self.attachments.glob("*/outputs") if p.is_dir()}
        self.launches.append(Launch(list(command), dict(env), {**os.environ, **env}, existing))
        return await super().start([sys.executable, str(self.fake), self.mode, *command[1:]], cwd, env)


class OllamaServer:
    """A loopback HTTP server that answers like Ollama, from a FakeOllama the test sets."""

    def __init__(self) -> None:
        self.fake = fake_ollama.FakeOllama()
        self.outputs_at_first_chat: list[bool] = []
        self.watch: Path | None = None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _answer(self, method: str) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else b""
                if self.path == "/api/chat" and server.watch is not None and not server.outputs_at_first_chat:
                    server.outputs_at_first_chat.append(server.watch.is_dir())

                async def forward() -> httpx2.Response:
                    async with httpx2.AsyncClient(transport=server.fake.transport, base_url="http://ollama") as client:
                        response = await client.request(method, self.path, content=body)
                        await response.aread()
                        return response

                response = asyncio.run(forward())
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/x-ndjson" if self.path == "/api/chat" else "application/json")
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            def do_GET(self) -> None:
                self._answer("GET")

            def do_POST(self) -> None:
                self._answer("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@dataclass
class WiredHub:
    client: TestClient
    attachments: Path
    launches: list[Launch]
    ollama: OllamaServer
    modes: dict[str, str] = field(default_factory=lambda: {"claude": "ok", "codex": "ok"})

    def outputs(self, session_id: str) -> Path:
        return self.attachments / session_id / "outputs"

    def chat(self, agent: str, **extra: Any) -> str:
        response = self.client.post("/api/sessions", json={"title": f"documents {agent}", "agent": agent, **extra})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def send(self, session_id: str, text: str, attachments: Sequence[str] = ()) -> dict:
        response = self.client.post(f"/api/sessions/{session_id}/messages",
                                    json={"text": text, "attachments": list(attachments)})
        assert response.status_code == 202, response.text
        return wait_until_idle(self.client, session_id)

    def events(self, session_id: str) -> list[dict]:
        return self.client.get(f"/api/sessions/{session_id}/events").json()

    def echo(self, session_id: str) -> dict:
        """What the fake CLI received on the last turn (it echoes it as its final message)."""
        for event in reversed(self.events(session_id)):
            try:
                echoed = json.loads((event.get("payload") or {}).get("text") or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(echoed, dict) and "args" in echoed:
                return echoed
        raise AssertionError("the fake CLI's echo isn't in the transcript")


def outputs_existed(launch: Launch, outputs: Path) -> bool:
    return os.path.normcase(str(outputs)) in launch.existing_outputs


def allowed_tools(args: list[str]) -> list[str]:
    """Every rule after --allowedTools / --allowed-tools (space- or comma-separated, as Claude Code accepts)."""
    rules: list[str] = []
    index = 0
    while index < len(args):
        if args[index] in ("--allowedTools", "--allowed-tools"):
            index += 1
            while index < len(args) and not args[index].startswith("--"):
                rules += [rule.strip() for rule in args[index].split(",") if rule.strip()]
                index += 1
            continue
        index += 1
    return rules


def writable_roots(args: list[str]) -> list[list[str]]:
    """Each `-c sandbox_workspace_write.writable_roots=<TOML>` value, parsed as codex-cli does (TOML)."""
    found = []
    for index, arg in enumerate(args[:-1]):
        value = args[index + 1]
        if arg == "-c" and value.startswith("sandbox_workspace_write.writable_roots="):
            found.append(tomllib.loads("v = " + value.split("=", 1)[1])["v"])
    return found


class OutputsApi:
    """The outputs routes (seam: names chosen here, behind the API token like every /api route):
    GET  /api/sessions/{id}/outputs               -> [{name, size, ...}]
    POST /api/sessions/{id}/outputs/open  {name}  -> {action: "opened" | "revealed"}
    POST /api/sessions/{id}/outputs/save  {name, destination} -> 200, a copy at destination
    GET  /api/sessions/{id}/outputs/file?name=     -> the bytes, for the preview pane (PR 3)
    """

    def __init__(self, client: TestClient, session_id: str) -> None:
        self.client, self.base = client, f"/api/sessions/{session_id}/outputs"

    def list(self):
        return self.client.get(self.base)

    def open(self, name: str):
        return self.client.post(f"{self.base}/open", json={"name": name})

    def save(self, name: str, destination: Path):
        return self.client.post(f"{self.base}/save", json={"name": name, "destination": str(destination)})

    def file(self, name: str):
        return self.client.get(f"{self.base}/file", params={"name": name})


def make_chat_with_original(client: TestClient) -> tuple[str, dict, Path]:
    response = client.post("/api/sessions", json={"title": "ok", "agent": "claude"})
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    note = upload(client, session_id, "notes.txt", b"the original\n")
    outputs = Path(note["path"]).parent.parent / "outputs"  # <attachments>/<session id>/outputs
    outputs.mkdir(exist_ok=True)
    return session_id, note, outputs
