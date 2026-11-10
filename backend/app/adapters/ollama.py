"""Local model adapter: the hub's own agent loop on a model served by Ollama, over its HTTP API.

Unlike Claude and Codex there is no CLI: the hub calls the chat API itself, keeps the
conversation (Ollama stores none) and turns the replies into the shared AgentEvents.
Phase 5a is chat only; tools and the loop that runs them come in 5b.

Behavior verified against Ollama 0.40.15:
- `POST /api/chat` with `stream: true` returns one JSON object per line,
  `{message: {role, content, thinking?}, done: false}`, and a last one with `done: true`,
  `done_reason`, `prompt_eval_count`, `eval_count` and `total_duration` (nanoseconds).
- Errors come back as `{"error": "..."}` with an HTTP error status (404: model not installed).
  An error can also arrive as a line in the middle of a stream.
- `options.num_ctx` sets the context size per request. Without it Ollama loads the model
  with 4096 tokens and silently drops the start of longer prompts.
- `GET /api/tags` lists installed models, with `capabilities` (such as "tools", "thinking")
  and `details.context_length`.
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx2 as httpx

from app.adapters.base import AgentAdapter, AgentEvent, EventType
from app.models import AgentKind

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_NUM_CTX = 32768
# Loading a large model and thinking before the first token can take minutes on a modest GPU.
TIMEOUT = httpx.Timeout(600.0, connect=5.0)
LIST_TIMEOUT = 3.0
HISTORY_VERSION = 1

CHAT_PROMPT = (
    "You are {model}, a local model running inside Multi-Agent Hub on the user's Windows PC. "
    "The session's folder is {cwd}. You can't read or change files or run commands in this version, "
    "so answer from the conversation, and say so plainly when a request needs those."
)


class OllamaUnavailable(RuntimeError):
    """Ollama isn't running, or the model asked for isn't installed."""


class OllamaError(RuntimeError):
    """A request to Ollama failed during a turn."""


class _Stopped(Exception):
    pass


_END = object()


# --- Models ---------------------------------------------------------------------------------------


def _not_running(base_url: str) -> OllamaUnavailable:
    return OllamaUnavailable(f"Ollama isn't running at {base_url}. Start Ollama and try again.")


def models_from_tags(body: Any) -> list[dict[str, Any]]:
    """Installed models from `/api/tags`, with what the hub needs to show and pick them."""
    models = []
    for raw in (body or {}).get("models") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        details = raw.get("details") or {}
        capabilities = raw.get("capabilities")
        models.append({
            "name": raw["name"],
            "size": raw.get("size"),
            "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            "context_length": details.get("context_length"),
            # None when this Ollama version doesn't report capabilities.
            "tools": "tools" in capabilities if isinstance(capabilities, list) else None,
            "thinking": "thinking" in capabilities if isinstance(capabilities, list) else None,
        })
    return models


def list_models_sync(base_url: str = DEFAULT_URL, transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    """Installed models, for the (synchronous) adapter factory. Raises OllamaUnavailable."""
    try:
        with httpx.Client(base_url=base_url, timeout=LIST_TIMEOUT, transport=transport, trust_env=False) as client:
            response = client.get("/api/tags")
            response.raise_for_status()
            return models_from_tags(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise _not_running(base_url) from exc


async def list_models(
    base_url: str = DEFAULT_URL, transport: httpx.AsyncBaseTransport | None = None
) -> list[dict[str, Any]]:
    """Installed models. Raises OllamaUnavailable."""
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=LIST_TIMEOUT, transport=transport,
                                     trust_env=False) as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
            return models_from_tags(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise _not_running(base_url) from exc


def _find(models: list[dict[str, Any]], name: str) -> str | None:
    names = [m["name"] for m in models]
    for candidate in (name, f"{name}:latest"):
        if candidate in names:
            return candidate
    return None


def pick_model(models: list[dict[str, Any]], requested: str | None, default: str | None = None) -> str:
    """The installed model to run: the one asked for, else the default setting, else the first with tools."""
    for name in (requested, default):
        if name:
            if found := _find(models, name):
                return found
            raise OllamaUnavailable(f"The model {name} isn't installed in Ollama. Install it with `ollama pull {name}`.")
    if not models:
        raise OllamaUnavailable("No models are installed in Ollama. Install one with `ollama pull <model>`.")
    with_tools = [m for m in models if m["tools"]]
    return (with_tools or models)[0]["name"]


# --- Conversation history -------------------------------------------------------------------------


def history_path(history_dir: Path, conversation_id: str) -> Path:
    # The id is always a UUID the adapter made; checking it keeps a stored value from naming another file.
    return Path(history_dir) / f"{uuid.UUID(conversation_id)}.json"


def delete_history(history_dir: Path, conversation_id: str) -> None:
    try:
        history_path(history_dir, conversation_id).unlink(missing_ok=True)
    except ValueError:
        pass


def load_history(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("messages") or [])


def save_history(path: Path, model: str, messages: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"version": HISTORY_VERSION, "model": model, "messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


# --- Adapter --------------------------------------------------------------------------------------


class OllamaAdapter(AgentAdapter):
    kind = AgentKind.OLLAMA

    def __init__(
        self,
        cwd: Path,
        model: str,
        native_session_id: str | None = None,
        *,
        history_dir: Path,
        base_url: str = DEFAULT_URL,
        num_ctx: int = DEFAULT_NUM_CTX,
        think: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cwd = Path(cwd)
        self.model = model
        self.history_dir = Path(history_dir)
        self.base_url = base_url
        self.num_ctx = num_ctx
        self.think = think
        self._transport = transport
        self._conversation_id = native_session_id
        self._reader: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def native_session_id(self) -> str | None:
        return self._conversation_id

    def start_task(self, prompt: str) -> AsyncGenerator[AgentEvent]:
        if self._conversation_id is not None:
            raise RuntimeError("Task already started; use send_reply")
        # Chosen up front like Claude's, so the orchestrator saves it when the turn starts.
        self._conversation_id = str(uuid.uuid4())
        return self._turn(prompt, new=True)

    def send_reply(self, reply: str) -> AsyncGenerator[AgentEvent]:
        if self._conversation_id is None:
            raise RuntimeError("No task started; use start_task")
        return self._turn(reply, new=False)

    def summarize(self, prompt: str, instructions: str) -> AsyncGenerator[AgentEvent]:
        return self._one_off(instructions, prompt)

    async def stop(self) -> None:
        if self._reader is not None and not self._reader.done():
            self._stopping = True
            self._reader.cancel()

    def _system_prompt(self) -> str:
        return CHAT_PROMPT.format(model=self.model, cwd=self.cwd)

    async def _turn(self, text: str, new: bool) -> AsyncGenerator[AgentEvent]:
        path = history_path(self.history_dir, self._conversation_id)
        if new:
            history: list[dict[str, Any]] = []
        else:
            try:
                history = load_history(path)
            except (OSError, ValueError):
                yield AgentEvent(EventType.ERROR, {
                    "message": f"This conversation's history ({path}) is missing or unreadable, so the local "
                               "model can't continue it. Hand the session off to start again from a brief.",
                })
                return

        yield AgentEvent(EventType.STARTED, {"session_id": self._conversation_id, "model": self.model,
                                             "cwd": str(self.cwd)})
        user = {"role": "user", "content": text}
        messages = [{"role": "system", "content": self._system_prompt()}, *history, user]
        parts: list[str] = []
        final: dict[str, Any] = {}
        try:
            async for chunk in self._chat(messages):
                parts.append((chunk.get("message") or {}).get("content") or "")
                if chunk.get("done"):
                    final = chunk
        except _Stopped:
            # Keep what the user saw, so the next message follows on from it.
            partial = "".join(parts).strip()
            save_history(path, self.model, [*history, user, *([{"role": "assistant", "content": partial}] if partial else [])])
            yield AgentEvent(EventType.ERROR, {"message": "Stopped by user", "stopped": True})
            return
        except OllamaError as exc:
            # Not saved: sending the message again shouldn't put it in the conversation twice.
            yield AgentEvent(EventType.ERROR, {"message": str(exc)})
            return

        reply = "".join(parts).strip()
        save_history(path, self.model, [*history, user, {"role": "assistant", "content": reply}])
        if reply:
            yield AgentEvent(EventType.MESSAGE, {"text": reply})
        yield _usage(final)
        if not reply:
            yield AgentEvent(EventType.ERROR, {"message": "The local model returned an empty answer."})
        else:
            yield AgentEvent(EventType.COMPLETED, {"text": reply, "done_reason": final.get("done_reason")})

    async def _one_off(self, instructions: str, prompt: str) -> AsyncGenerator[AgentEvent]:
        """A call outside the conversation, saving nothing."""
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}]
        parts: list[str] = []
        final: dict[str, Any] = {}
        try:
            async for chunk in self._chat(messages):
                parts.append((chunk.get("message") or {}).get("content") or "")
                if chunk.get("done"):
                    final = chunk
        except _Stopped:
            yield AgentEvent(EventType.ERROR, {"message": "Stopped", "stopped": True})
            return
        except OllamaError as exc:
            yield AgentEvent(EventType.ERROR, {"message": str(exc)})
            return
        yield _usage(final)
        yield AgentEvent(EventType.COMPLETED, {"text": "".join(parts).strip()})

    async def _chat(self, messages: list[dict[str, Any]]) -> AsyncGenerator[dict[str, Any]]:
        """Ollama's response chunks. Raises OllamaError, or _Stopped after stop().

        The request runs in its own task, so stop() can cancel it at any point, even while
        the model is still loading or thinking and nothing has arrived yet.
        """
        if self._reader is not None:
            raise RuntimeError("A turn is already running")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"num_ctx": self.num_ctx},
        }
        if self.think is not None:
            payload["think"] = self.think
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._stopping = False
        self._reader = asyncio.create_task(self._read(payload, queue))
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    return
                if isinstance(item, Exception):
                    raise item
                if item.get("error"):
                    raise OllamaError(f"Ollama: {item['error']}")
                yield item
        finally:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None

    async def _read(self, payload: dict[str, Any], queue: asyncio.Queue[Any]) -> None:
        try:
            async with (
                httpx.AsyncClient(base_url=self.base_url, timeout=TIMEOUT, transport=self._transport,
                                  trust_env=False) as client,
                client.stream("POST", "/api/chat", json=payload) as response,
            ):
                if response.status_code >= 400:
                    raise OllamaError(_error_text(response.status_code, await response.aread(), self.model))
                async for line in response.aiter_lines():
                    if line.strip():
                        try:
                            queue.put_nowait(json.loads(line))
                        except json.JSONDecodeError:
                            raise OllamaError(f"Ollama sent a line that isn't JSON: {line[:200]}") from None
            queue.put_nowait(_END)
        except asyncio.CancelledError:
            queue.put_nowait(_Stopped() if self._stopping else _END)
            raise
        except OllamaError as exc:
            queue.put_nowait(exc)
        except httpx.ConnectError:
            queue.put_nowait(OllamaError(f"Ollama isn't running at {self.base_url}. Start Ollama and try again."))
        except httpx.TimeoutException:
            queue.put_nowait(OllamaError("Ollama took too long to answer."))
        except httpx.HTTPError as exc:
            queue.put_nowait(OllamaError(f"The request to Ollama failed: {exc}"))


def _error_text(status: int, body: bytes, model: str) -> str:
    try:
        message = json.loads(body).get("error") or ""
    except (ValueError, AttributeError):
        message = body.decode("utf-8", "replace")[:500]
    if status == 404:
        return f"The model {model} isn't installed in Ollama ({message or 'not found'}). Install it with `ollama pull {model}`."
    return f"Ollama returned HTTP {status}: {message or 'no details'}"


def _usage(final: dict[str, Any]) -> AgentEvent:
    duration = final.get("total_duration")
    return AgentEvent(
        EventType.USAGE,
        {
            "usage": {"input_tokens": final.get("prompt_eval_count") or 0,
                      "output_tokens": final.get("eval_count") or 0},
            "duration_ms": round(duration / 1_000_000) if isinstance(duration, int | float) else None,
        },
        raw=final or None,
    )
