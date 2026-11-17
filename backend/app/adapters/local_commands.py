"""The local model's commands (revision 4): `/compact`, `/context` and `/usage`, all the hub's own, since a model
served by Ollama has none. Each streams the same `AgentEvent`s as a turn, ending in `COMPLETED`. (`/code-review` is a
normal turn with the session's diff, built by the orchestrator.)

- `/compact [focus]`: the local model writes a summary of its saved conversation in a one-off request (no tools,
  free since it runs on this PC), and the saved conversation is replaced by that summary, so every later request
  starts from it. If the model fails, the hub's own brief (`OllamaAdapter.compact_fallback`, set by the
  orchestrator) is used instead.
- `/context`: the hub's estimate of what the next request sends (system prompt, tool definitions, conversation)
  against `num_ctx`, with the same character count `local_context.fit` uses.
- `/usage`: the local model has no plan and no limits.
"""

import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from app.adapters.base import AgentEvent, EventType
from app.adapters.local_context import (
    CHARS_PER_TOKEN,
    MIN_REPLY_TOKENS,
    REPLY_SHARE,
    _keep_ends,
    estimate_tokens,
    prompt_budget,
)
from app.adapters.local_tools import TOOL_DEFINITIONS

if TYPE_CHECKING:
    from app.adapters.ollama import OllamaAdapter

COMMANDS = {"compact", "context", "usage"}
COMPACT_SYSTEM = (
    "You summarize a coding session between a user and an AI coding agent, so the agent can keep working after the "
    "conversation is replaced by your summary. The agent will see only the summary, so include everything it needs "
    "and nothing else. Use these sections:\n\n"
    "## Task\nThe user's goal and requirements, with their exact wording where it matters.\n\n"
    "## Done so far\nFiles created or changed and how, commands run and their results.\n\n"
    "## Current state\nWhat works, what doesn't, errors still open.\n\n"
    "## Next steps\nWhat was about to happen next.\n\n"
    "## Details to keep\nExact file paths, names, values, decisions and the user's preferences.\n\n"
    "Be specific and brief. Keep paths and names exactly as written. Write the summary only, with no introduction."
)
COMPACT_REQUEST = "Summarize the conversation above, using the sections from your instructions."
SUMMARY_HEADER = (
    "# Compacted conversation\n\n"
    "The user compacted this conversation to free up context. You can't see the earlier messages any more; this "
    "summary of them, which you wrote, is what you keep. Check the current files before changing them, don't redo "
    "finished work, and continue from where you stopped. The user's next message follows."
)
USER_CLIP = 6_000  # one user message in the summary request (a handoff brief can be 40k)
CALL_CLIP = 300
RESULT_CLIP = 600


async def run(adapter: "OllamaAdapter", name: str, rest: str) -> AsyncGenerator[AgentEvent]:
    """Run `/name rest` for the adapter's conversation and stream its events."""
    if name not in COMMANDS:
        raise ValueError(f"The local model has no /{name} the hub can run")
    adapter._stopping = False
    async for event in _COMMANDS[name](adapter, rest):
        yield event


# --- The commands --------------------------------------------------------------------------------


async def _compact(adapter: "OllamaAdapter", rest: str) -> AsyncGenerator[AgentEvent]:
    from app.adapters.ollama import OllamaError, _Stopped, history_path, load_history, save_history

    path = history_path(adapter.history_dir, adapter.native_session_id)
    try:
        history = load_history(path)
    except (OSError, ValueError):
        yield AgentEvent(EventType.ERROR, {"message": f"This conversation's history ({path}) is missing or "
                                                      "unreadable, so there is nothing to compact."})
        return
    if not any(m.get("role") == "assistant" for m in history):
        for event in _report("Not enough messages to compact."):
            yield event
        return

    before = context_tokens(adapter, history)
    summary, problem, by = "", "", "model"
    try:
        summary, usage = await _write_summary(adapter, history, rest)
        yield usage
    except _Stopped:
        yield AgentEvent(EventType.ERROR, {"message": "Stopped by user", "stopped": True})
        return
    except OllamaError as exc:
        problem = str(exc)
    if not summary:
        problem = problem or "it returned an empty summary"
        if adapter.compact_fallback is None:
            yield AgentEvent(EventType.ERROR, {"message": f"The local model couldn't write the summary ({problem}), "
                                                          "so the conversation wasn't compacted."})
            return
        # The hub's brief is a complete first message of its own (it ends with the focus text).
        try:
            summary, by = await adapter.compact_fallback(), "hub"
        except Exception as exc:  # noqa: BLE001 - reported in the chat instead of failing the turn
            yield AgentEvent(EventType.ERROR, {"message": f"The local model couldn't write the summary ({problem}), "
                                                          f"and neither could the hub ({exc})."})
            return
        content = summary
    else:
        content = SUMMARY_HEADER + "\n\n" + summary + (f"\n\n## Keep in focus\n\n{rest}" if rest else "")

    compacted = [{"role": "user", "content": content}]
    save_history(path, adapter.model, compacted)
    after = context_tokens(adapter, compacted)
    message = f"Conversation compacted · {_tokens(before)} → {_tokens(after)} tokens"
    if by == "hub":
        message += f" (the local model couldn't write the summary: {problem}; the hub's summary was used)"
    yield AgentEvent(EventType.NOTICE, {"message": message, "reason": "compacted", "trigger": "manual",
                                        "pre_tokens": before, "post_tokens": after, "summary": summary, "by": by})
    yield AgentEvent(EventType.COMPLETED, {"text": ""})


async def _context(adapter: "OllamaAdapter", rest: str) -> AsyncGenerator[AgentEvent]:
    history = _saved_history(adapter)
    system = {"role": "system", "content": adapter._system_prompt()}
    tools = TOOL_DEFINITIONS if adapter.use_tools else None
    system_tokens = estimate_tokens(system)
    tool_tokens = estimate_tokens(tools) if tools else 0
    conversation = sum(estimate_tokens(m) for m in history)
    used = system_tokens + tool_tokens + conversation
    reserve = max(MIN_REPLY_TOKENS, int(adapter.num_ctx * REPLY_SHARE))
    budget = prompt_budget(adapter.num_ctx, tools)
    share = round(100 * used / adapter.num_ctx) if adapter.num_ctx else 0

    rows = [
        "## Local model context\n",
        f"**About {_tokens(used)} of {_tokens(adapter.num_ctx)} tokens ({share}%)** · {adapter.model}\n",
        "| Part | Tokens |",
        "|---|---|",
        f"| System prompt | {_tokens(system_tokens)} |",
    ]
    if tools:
        rows.append(f"| Tool definitions | {_tokens(tool_tokens)} |")
    rows += [
        f"| Conversation ({len(history)} message{'s' if len(history) != 1 else ''}) | {_tokens(conversation)} |",
        f"| Kept free for the reply | {_tokens(reserve)} |",
    ]
    text = "\n".join(rows) + "\n\n"
    if system_tokens + conversation > budget:
        text += ("The conversation no longer fits, so the hub shortens what it sends (old tool outputs first, then "
                 "earlier turns). Use /compact to have the model summarize it instead.\n\n")
    elif system_tokens + conversation > 0.8 * budget:
        text += "The context is nearly full. Use /compact to have the model summarize the conversation.\n\n"
    text += (f"Estimated from the text (about {CHARS_PER_TOKEN:g} characters per token), so the real count is "
             "usually a little lower. The context size is set by `HUB_OLLAMA_NUM_CTX`.")
    for event in _report(text):
        yield event


async def _usage(adapter: "OllamaAdapter", rest: str) -> AsyncGenerator[AgentEvent]:
    text = (f"## Local model usage\n\nThe local model ({adapter.model}) runs on this PC through Ollama, so it has no "
            "usage limits and costs nothing per message. How fast it answers depends on your hardware.")
    for event in _report(text):
        yield event


_COMMANDS = {"compact": _compact, "context": _context, "usage": _usage}


# --- Helpers -------------------------------------------------------------------------------------


async def _write_summary(adapter: "OllamaAdapter", history: list[dict[str, Any]], rest: str
                         ) -> tuple[str, AgentEvent]:
    """The model's summary of `history` and the request's usage. Raises OllamaError or _Stopped."""
    from app.adapters.ollama import _usage

    request = COMPACT_REQUEST + (f"\n\nGive special attention to: {rest}" if rest else "")
    room = int(prompt_budget(adapter.num_ctx, None) * CHARS_PER_TOKEN) - len(COMPACT_SYSTEM) - len(request) - 200
    transcript = render(history)
    if len(transcript) > room:
        transcript = _keep_ends(transcript, max(2000, room))
    messages = [{"role": "system", "content": COMPACT_SYSTEM},
                {"role": "user", "content": f"{transcript}\n\n---\n\n{request}"}]
    parts: list[str] = []
    final: dict[str, Any] = {}
    async for chunk in adapter._chat(messages):
        parts.append((chunk.get("message") or {}).get("content") or "")
        if chunk.get("done"):
            final = chunk
    return "".join(parts).strip(), _usage(final)


def render(history: list[dict[str, Any]]) -> str:
    """The saved conversation as plain text for the summary request (no tools are sent with it, so tool calls and
    results are written out, clipped)."""
    parts = []
    for message in history:
        role, content = message.get("role"), (message.get("content") or "").strip()
        if role == "user":
            parts.append("### User\n" + (_keep_ends(content, USER_CLIP) if len(content) > USER_CLIP else content))
        elif role == "assistant":
            lines = [content] if content else []
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                args = json.dumps(function.get("arguments"), ensure_ascii=False)
                lines.append(f"[called {function.get('name')} {_clip(args, CALL_CLIP)}]")
            if lines:
                parts.append("### Assistant\n" + "\n".join(lines))
        elif role == "tool":
            parts.append(f"[result of {message.get('tool_name') or 'tool'}: {_clip(content, RESULT_CLIP)}]")
    return "\n\n".join(parts)


def context_tokens(adapter: "OllamaAdapter", history: list[dict[str, Any]]) -> int:
    """The estimated size of a request with `history`: system prompt, tool definitions and messages."""
    system = estimate_tokens({"role": "system", "content": adapter._system_prompt()})
    tools = estimate_tokens(TOOL_DEFINITIONS) if adapter.use_tools else 0
    return system + tools + sum(estimate_tokens(m) for m in history)


def _saved_history(adapter: "OllamaAdapter") -> list[dict[str, Any]]:
    from app.adapters.ollama import history_path, load_history

    if adapter.native_session_id is None:
        return []
    try:
        return load_history(history_path(adapter.history_dir, adapter.native_session_id))
    except (OSError, ValueError):
        return []


def _report(text: str) -> list[AgentEvent]:
    """A command's answer: a message row, then the end of the turn."""
    return [AgentEvent(EventType.MESSAGE, {"text": text, "parent_tool_use_id": None}),
            AgentEvent(EventType.COMPLETED, {"text": text})]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _tokens(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)
