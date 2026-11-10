"""A stand-in for Ollama's HTTP API, served through an httpx MockTransport.

Modes for /api/chat: ok (echoes the last user message and how many messages it got),
hang (one chunk, then never finishes), empty (no text), missing (404, model not installed),
error (HTTP 500), midstream (an error line after the first chunk), down (connection refused,
also for /api/tags).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx

MODEL = "qwen-test:9b"
TAGS = {
    "models": [
        {"name": "plain:1b", "size": 1, "details": {"parameter_size": "1B", "quantization_level": "Q4_0"},
         "capabilities": ["completion"]},
        {"name": MODEL, "size": 6_550_825_550,
         "details": {"parameter_size": "9.0B", "quantization_level": "Q4_K_M", "context_length": 262144},
         "capabilities": ["completion", "tools", "thinking"]},
    ]
}


def _lines(*chunks: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(c).encode() + b"\n" for c in chunks)


def _chunk(text: str, model: str) -> dict[str, Any]:
    return {"model": model, "message": {"role": "assistant", "content": text}, "done": False}


def _done(model: str) -> dict[str, Any]:
    return {"model": model, "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
            "prompt_eval_count": 12, "eval_count": 5, "total_duration": 1_500_000_000}


class FakeOllama:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []  # each /api/chat request body
        self.transport = httpx.MockTransport(self._handle)
        self.sync_transport = httpx.MockTransport(self._handle_sync)

    def _handle_sync(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "down":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=TAGS)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "down":
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=TAGS)
        body = json.loads(request.content)
        self.calls.append(body)
        model = body["model"]
        match self.mode:
            case "missing":
                return httpx.Response(404, json={"error": f"model '{model}' not found"})
            case "error":
                return httpx.Response(500, json={"error": "boom"})
            case "empty":
                return httpx.Response(200, content=_lines(_done(model)))
            case "midstream":
                return httpx.Response(200, content=_lines(_chunk("Partly", model), {"error": "out of memory"}))
            case "hang":
                return httpx.Response(200, content=self._hang(model))
        last = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
        reply = f"Echo: {last} ({len(body['messages'])} messages)"
        half = len(reply) // 2
        return httpx.Response(200, content=_lines(_chunk(reply[:half], model), _chunk(reply[half:], model), _done(model)))

    @staticmethod
    async def _hang(model: str) -> AsyncIterator[bytes]:
        yield _lines(_chunk("Thinking about it", model))
        await asyncio.sleep(3600)
