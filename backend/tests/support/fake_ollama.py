"""A stand-in for Ollama's HTTP API, served through an httpx MockTransport.

Modes for /api/chat: ok (echoes the last user message and how many messages it got),
hang (one chunk, then never finishes), empty (no text), missing (404, model not installed),
error (HTTP 500), midstream (an error line after the first chunk), down (connection refused,
also for /api/tags), tools (TOOLS_SCRIPT: a plan, a new file, a command, then a summary).

A script is a list of replies, one per request in a turn: a string is a plain answer, a dict
`{"content": ..., "tool_calls": [(name, args), ...]}` calls tools. The reply is picked by how
many assistant messages already follow the last user message, so the fake keeps no state and a
fresh instance per turn (as the test factory makes) still follows the script. Past its end it
answers "Done.".
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2 as httpx

Reply = str | dict[str, Any]

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


# What `tools` mode does: the 5b acceptance task in miniature.
TOOLS_SCRIPT: list[Reply] = [
    {"content": "I'll make the file.", "tool_calls": [
        ("update_plan", {"steps": [{"text": "Create hello.txt", "done": False}, {"text": "Show it", "done": False}]}),
        ("write_file", {"path": "hello.txt", "content": "hello from the local model\n"}),
    ]},
    {"tool_calls": [("run_command", {"command": "Get-Content hello.txt"})]},
    "Created hello.txt and printed it.",
]


def _lines(*chunks: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(c).encode() + b"\n" for c in chunks)


def _chunk(text: str, model: str) -> dict[str, Any]:
    return {"model": model, "message": {"role": "assistant", "content": text}, "done": False}


def _done(model: str) -> dict[str, Any]:
    return {"model": model, "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
            "prompt_eval_count": 12, "eval_count": 5, "total_duration": 1_500_000_000}


class FakeOllama:
    def __init__(self, mode: str = "ok", script: list[Reply] | None = None) -> None:
        self.mode = mode
        self.script = script if script is not None else TOOLS_SCRIPT if mode == "tools" else None
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
        if self.script is not None:
            return httpx.Response(200, content=_lines(*self._scripted(body), _done(model)))
        last = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
        reply = f"Echo: {last} ({len(body['messages'])} messages)"
        half = len(reply) // 2
        return httpx.Response(200, content=_lines(_chunk(reply[:half], model), _chunk(reply[half:], model), _done(model)))

    def _scripted(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        messages = body["messages"]
        # The hub's one-off nudge for an answer isn't a new request from the user; it follows a quiet reply
        # that wasn't kept, so it counts as one more step.
        nudged = [i for i, m in enumerate(messages) if m["role"] == "user" and m["content"].startswith("(From the hub")]
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user" and i not in nudged)
        step = sum(1 for m in messages[last_user:] if m["role"] == "assistant") + len(nudged)
        reply = self.script[step] if step < len(self.script) else "Done."
        if isinstance(reply, str):
            return [_chunk(reply, body["model"])]
        chunk = _chunk(reply.get("content", ""), body["model"])
        chunk["message"]["tool_calls"] = [
            {"function": {"name": name, "arguments": args}} for name, args in reply.get("tool_calls", [])
        ]
        return [chunk]

    @staticmethod
    async def _hang(model: str) -> AsyncIterator[bytes]:
        yield _lines(_chunk("Thinking about it", model))
        await asyncio.sleep(3600)
