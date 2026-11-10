"""Local model adapter: the hub's own agent loop on a model served by Ollama, over its HTTP API.

Unlike Claude and Codex there is no CLI: the hub calls the chat API itself, keeps the
conversation (Ollama stores none) and turns the replies into the shared AgentEvents.

The loop (phase 5b): the request carries the tool definitions from `local_tools`. While the
model's reply has `tool_calls`, the hub runs each one, emits TOOL_USE and TOOL_RESULT, appends
the results and asks again; a reply without tool calls ends the turn. `update_plan` becomes a
PLAN event and `ask_user` ends the turn as a QUESTION, so no text markers are needed. A cap on
rounds per turn keeps a confused model from looping forever. Models without the `tools`
capability get plain chat.

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
- With `tools` in the request, a reply's tool calls arrive as `message.tool_calls`
  (`[{function: {name, arguments}}]`, arguments as an object) in a streamed chunk. Results go
  back as `{role: "tool", content, tool_name}` messages after the assistant message that made
  the calls. A model without the tools capability answers HTTP 400 to a request with tools.
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
from app.adapters.local_context import fit, prompt_budget
from app.adapters.local_tools import (
    DEFAULT_COMMAND_TIMEOUT,
    TOOL_DEFINITIONS,
    LocalTools,
    ToolError,
    plan_from_args,
)
from app.adapters.runner import Runner
from app.models import AgentKind

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_NUM_CTX = 32768
DEFAULT_MAX_ROUNDS = 30
QUIET_NUDGE = "(From the hub: reply to the user now, with your answer or a short summary of what you did.)"
# The same tool call with the same arguments, this many times in a row: the third is answered with a
# nudge instead of being run, the fourth ends the turn.
REPEAT_NUDGE = 3
REPEAT_STOP = 4
# Loading a large model and thinking before the first token can take minutes on a modest GPU.
TIMEOUT = httpx.Timeout(600.0, connect=5.0)
LIST_TIMEOUT = 3.0
HISTORY_VERSION = 1

# For models that can't use tools.
CHAT_PROMPT = (
    "You are {model}, a local model running inside Multi-Agent Hub on the user's Windows PC. "
    "The session's folder is {cwd}. This model can't use tools, so you can't read or change files or "
    "run commands: answer from the conversation, and say so plainly when a request needs those."
)
# Short and direct, written for small models.
TOOLS_PROMPT = """\
You are {model}, a coding agent running on the user's Windows PC inside Multi-Agent Hub.
Your working folder is {cwd}. Paths are relative to it. You can't use files outside it.

How to work:
- Look at files with read_file, list_files and search before changing them. Don't guess what a file contains.
- Change files only with write_file (new file or whole file) and edit_file (replace exact text). Never write files with shell commands.
- run_command runs Windows PowerShell 5.1 in the working folder. Use PowerShell syntax: separate commands with `;` (not `&&`). Commands can't ask for input.
- For a task with several steps, call update_plan with all the steps first, then again each time you finish one.
- If you can't continue without an answer from the user, call ask_user.
- When the task is done, answer with a short summary of what you did and no tool call.
- Questions that need no files or commands: just answer them.
"""


class OllamaUnavailable(RuntimeError):
    """Ollama isn't running, or the model asked for isn't installed."""


class OllamaError(RuntimeError):
    """A request to Ollama failed during a turn."""


class _Stopped(Exception):
    def __init__(self, partial: str = "") -> None:
        super().__init__("stopped")
        # The reply's text received before the stop.
        self.partial = partial


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
        tools: bool = True,
        runner: Runner | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        self.cwd = Path(cwd)
        self.model = model
        self.history_dir = Path(history_dir)
        self.base_url = base_url
        self.num_ctx = num_ctx
        self.think = think
        # False for a model without the tools capability: plain chat.
        self.use_tools = tools
        self.max_rounds = max_rounds
        self.tools = LocalTools(self.cwd, runner, command_timeout)
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
        # The loop checks the flag between steps; a request or a command in progress is ended now.
        self._stopping = True
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
        await self.tools.kill()

    def _system_prompt(self) -> str:
        return (TOOLS_PROMPT if self.use_tools else CHAT_PROMPT).format(model=self.model, cwd=self.cwd)

    async def _turn(self, text: str, new: bool) -> AsyncGenerator[AgentEvent]:
        """One turn: ask, run the tools the reply calls, ask again, until a reply calls none."""
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

        self._stopping = False
        yield AgentEvent(EventType.STARTED, {"session_id": self._conversation_id, "model": self.model,
                                             "cwd": str(self.cwd)})
        system = {"role": "system", "content": self._system_prompt()}
        # This turn's messages, saved after the history when the turn ends.
        added: list[dict[str, Any]] = [{"role": "user", "content": text}]
        usage = _Usage()
        turn = uuid.uuid4().hex[:8]
        calls_made = 0
        budget = prompt_budget(self.num_ctx, TOOL_DEFINITIONS if self.use_tools else None)
        context_noted = False
        last_call, repeats = "", 0
        # Asked once per turn for an answer after the model went quiet; sent, not saved.
        nudge: dict[str, Any] | None = None
        nudged = False

        def save() -> None:
            save_history(path, self.model, [*history, *added])

        def end_early(message: str, reason: str) -> list[AgentEvent]:
            """The turn stops before the model finished, waiting for the user to say how to go on."""
            save()
            return [
                usage.event(),
                AgentEvent(EventType.NOTICE, {"message": message, "reason": reason}),
                AgentEvent(EventType.QUESTION, {"text": message, "reason": reason}),
            ]

        for round_number in range(1, self.max_rounds + 1):
            fitted = fit([system, *history, *added], budget)
            if fitted.changed and not context_noted:
                context_noted = True
                yield AgentEvent(EventType.NOTICE, {"message": _context_note(fitted, self.num_ctx), "reason": "context",
                                                    "shortened": fitted.shortened, "dropped": fitted.dropped,
                                                    "cut_prompt": fitted.cut_prompt})
            try:
                reply, tool_calls, final = await self._ask([*fitted.messages, *([nudge] if nudge else [])])
            except _Stopped as stopped:
                if stopped.partial:
                    # Keep what the user saw, so the next message follows on from it.
                    added.append({"role": "assistant", "content": stopped.partial})
                save()
                yield usage.event()
                yield AgentEvent(EventType.ERROR, {"message": "Stopped by user", "stopped": True})
                return
            except OllamaError as exc:
                if round_number > 1:
                    save()  # the tools already ran, so their results stay in the conversation
                # Otherwise not saved: sending the message again shouldn't put it in the conversation twice.
                yield usage.event()
                yield AgentEvent(EventType.ERROR, {"message": str(exc)})
                return
            usage.add(final)
            if not reply and not tool_calls and round_number > 1 and not nudged and not self._stopping:
                # Small models often answer in their (dropped) thinking and then say nothing. Ask once more.
                nudge, nudged = {"role": "user", "content": QUIET_NUDGE}, True
                continue
            nudge = None

            assistant: dict[str, Any] = {"role": "assistant", "content": reply}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            added.append(assistant)
            if reply:
                yield AgentEvent(EventType.MESSAGE, {"text": reply, "parent_tool_use_id": None})

            if not tool_calls:
                save()
                yield usage.event()
                if reply or round_number > 1:
                    # Small models often finish their tool work with no closing words; the work still counts.
                    yield AgentEvent(EventType.COMPLETED, {"text": reply, "done_reason": final.get("done_reason")})
                else:
                    yield AgentEvent(EventType.ERROR, {"message": "The local model returned an empty answer."})
                return

            question: str | None = None
            looping = False
            for call in tool_calls:
                function = call["function"]
                name, args = function["name"], function["arguments"]
                if self._stopping or question is not None or looping:
                    # Every call gets an answer, or the next request confuses the model.
                    added.append(_tool_message(call, "Not run: the turn ended first."))
                    continue
                if isinstance(args, str):
                    added.append(_tool_message(call, f"Error: the arguments for {name} weren't valid JSON. "
                                                     "Call it again with a JSON object."))
                    continue
                if name == "ask_user":
                    question = str(args.get("question") or "").strip() or "The model asked for your input."
                    added.append(_tool_message(call, "The question was shown to the user. "
                                                     "Their answer is the next message."))
                    continue
                if name == "update_plan":
                    try:
                        items = plan_from_args(args)
                    except ToolError as exc:
                        added.append(_tool_message(call, f"Error: {exc}"))
                        continue
                    yield AgentEvent(EventType.PLAN, {"items": items})
                    added.append(_tool_message(call, "Plan updated."))
                    continue

                signature = f"{name} {json.dumps(args, sort_keys=True)}"
                repeats = repeats + 1 if signature == last_call else 1
                last_call = signature
                if repeats >= REPEAT_STOP:
                    looping = True
                    added.append(_tool_message(call, "Not run: the same call again."))
                    continue
                if repeats == REPEAT_NUDGE:
                    added.append(_tool_message(call, "Not run: you already made this exact call twice in a row and "
                                                     "got the result above. Use that result, try something else, "
                                                     "or give your answer."))
                    continue

                calls_made += 1
                tool_use_id = f"{turn}-{calls_made}"
                shown = self.tools.display(name, args)
                yield AgentEvent(EventType.TOOL_USE, {"tool_use_id": tool_use_id, "name": shown.name,
                                                      "input": shown.input, "parent_tool_use_id": None})
                result = await self.tools.run(name, args)
                yield AgentEvent(EventType.TOOL_RESULT, {"tool_use_id": tool_use_id, "is_error": result.is_error,
                                                         "content": result.content, "parent_tool_use_id": None,
                                                         **result.extra})
                added.append(_tool_message(call, result.content))

            if self._stopping:
                save()
                yield usage.event()
                yield AgentEvent(EventType.ERROR, {"message": "Stopped by user", "stopped": True})
                return
            if question is not None:
                save()
                yield AgentEvent(EventType.MESSAGE, {"text": question, "parent_tool_use_id": None})
                yield usage.event()
                yield AgentEvent(EventType.QUESTION, {"text": question})
                return
            if looping:
                for event in end_early(
                    "The local model kept repeating the same step, so the hub stopped the turn. Send a message "
                    "to steer it (for example, what to try instead), or hand the session off.", "loop"):
                    yield event
                return

        for event in end_early(
            f"The local model took {self.max_rounds} steps without finishing, so the hub paused the turn. "
            "Send a message (such as \"continue\") to let it go on, or hand the session off.", "round_cap"):
            yield event

    async def _ask(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        """One request: the reply's text, its tool calls (normalized) and the final chunk.

        Raises OllamaError, or _Stopped carrying the text received so far.
        """
        parts: list[str] = []
        calls: list[dict[str, Any]] = []
        final: dict[str, Any] = {}
        try:
            async for chunk in self._chat(messages, tools=self.use_tools):
                message = chunk.get("message") or {}
                parts.append(message.get("content") or "")
                calls += [c for c in (_normalize_call(raw) for raw in message.get("tool_calls") or []) if c]
                if chunk.get("done"):
                    final = chunk
        except _Stopped:
            raise _Stopped("".join(parts).strip()) from None
        return "".join(parts).strip(), calls, final

    async def _one_off(self, instructions: str, prompt: str) -> AsyncGenerator[AgentEvent]:
        """A call outside the conversation, saving nothing, with no tools."""
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}]
        self._stopping = False
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

    async def _chat(self, messages: list[dict[str, Any]], tools: bool = False) -> AsyncGenerator[dict[str, Any]]:
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
        if tools:
            payload["tools"] = TOOL_DEFINITIONS
        queue: asyncio.Queue[Any] = asyncio.Queue()
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


def _context_note(fitted: Any, num_ctx: int) -> str:
    parts = []
    if fitted.shortened:
        parts.append(f"shortened {fitted.shortened} old tool output{'s' if fitted.shortened != 1 else ''}")
    if fitted.dropped:
        parts.append(f"left out {fitted.dropped} earlier message{'s' if fitted.dropped != 1 else ''}")
    if fitted.cut_prompt:
        parts.append("cut the middle of the latest message")
    return (f"The conversation is longer than the local model's context ({num_ctx:,} tokens), so the hub "
            f"{', '.join(parts[:-1]) + ' and ' + parts[-1] if len(parts) > 1 else parts[0]} for this turn. "
            "The model may have forgotten details from earlier; the saved conversation is complete.")


def _normalize_call(raw: Any) -> dict[str, Any] | None:
    """A tool call as `{function: {name, arguments}}`, arguments parsed from JSON text if needed.

    Arguments that aren't valid JSON stay a string, so the loop can tell the model.
    """
    function = (raw or {}).get("function") if isinstance(raw, dict) else None
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        return None
    args = function.get("arguments")
    if isinstance(args, str):
        try:
            parsed = json.loads(args) if args.strip() else {}
            args = parsed if isinstance(parsed, dict) else args
        except json.JSONDecodeError:
            pass
    elif not isinstance(args, dict):
        args = {}
    call: dict[str, Any] = {"function": {"name": function["name"], "arguments": args}}
    if isinstance(raw.get("id"), str):
        call["id"] = raw["id"]
    return call


def _tool_message(call: dict[str, Any], content: str) -> dict[str, Any]:
    message = {"role": "tool", "content": content, "tool_name": call["function"]["name"]}
    if "id" in call:
        message["tool_call_id"] = call["id"]
    return message


class _Usage:
    """A turn's tokens and time, added up over its requests."""

    def __init__(self) -> None:
        self.input = self.output = self.requests = 0
        self.duration_ns = 0

    def add(self, final: dict[str, Any]) -> None:
        self.requests += 1
        self.input += final.get("prompt_eval_count") or 0
        self.output += final.get("eval_count") or 0
        duration = final.get("total_duration")
        self.duration_ns += duration if isinstance(duration, int | float) else 0

    def event(self) -> AgentEvent:
        return AgentEvent(EventType.USAGE, {
            "usage": {"input_tokens": self.input, "output_tokens": self.output},
            "duration_ms": round(self.duration_ns / 1_000_000) if self.duration_ns else None,
            "num_turns": self.requests,
        })


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
