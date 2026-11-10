"""Fitting the local model's conversation into its context window.

A local model has a small context (32k tokens by default), and every tool result stays in the
conversation, so a long session outgrows it. Ollama doesn't fail when that happens: it silently
drops the start of the prompt, which is where the system prompt and the task are. So before each
request the hub estimates the size and, when it's too big, shortens what is sent:

1. Old tool outputs (and the file contents in old write/edit calls) are cut to a short stub,
   oldest first. The latest few messages are kept whole.
2. Earlier turns' tool steps are left out, oldest first, keeping each turn's request and final
   answer; then, if needed, whole earlier turns. The first message (the task) and the current
   turn are always kept. The system prompt says how many messages were left out.
3. If the current turn alone is still too big, its tool outputs are cut too, and then its
   message (a long handoff brief, for example) keeps only its start and end.

Only what is sent is shortened; the saved conversation stays complete. Sizes are estimated from
the characters (no tokenizer), on the safe side.
"""

import json
from dataclasses import dataclass
from typing import Any

# Code and JSON run about 3 to 4 characters per token; 3 overestimates, which is the safe side.
CHARS_PER_TOKEN = 3.0
# Room left for the model's reply (thinking included).
MIN_REPLY_TOKENS = 4096
REPLY_SHARE = 0.2
# Messages at the end that are never shortened in step 1.
KEEP_RECENT = 6
STUB_CHARS = 300
# Shorter than this, cutting saves too little to bother.
MIN_CUT = 600
# Arguments of old calls that hold file contents.
CONTENT_ARGS = ("content", "old_text", "new_text")


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return int(len(text) / CHARS_PER_TOKEN) + 1


def prompt_budget(num_ctx: int, tools: Any) -> int:
    """Tokens the messages may use: the context minus the reply's room and the tool definitions."""
    reserve = max(MIN_REPLY_TOKENS, int(num_ctx * REPLY_SHARE))
    return max(1024, num_ctx - reserve - (estimate_tokens(tools) if tools else 0))


@dataclass
class Fitted:
    messages: list[dict[str, Any]]
    tokens: int
    # Tool outputs and call arguments cut to a stub.
    shortened: int = 0
    # Messages of earlier turns left out.
    dropped: int = 0
    # The current turn's message was cut (keeping its start and end).
    cut_prompt: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.shortened or self.dropped or self.cut_prompt)


def fit(messages: list[dict[str, Any]], budget: int) -> Fitted:
    """`messages` (system prompt first) shortened to fit `budget` tokens. The input is not changed."""
    sizes = [estimate_tokens(m) for m in messages]
    if sum(sizes) <= budget:
        return Fitted(list(messages), sum(sizes))

    msgs = [dict(m) for m in messages]
    result = Fitted(msgs, sum(sizes))
    users = [i for i, m in enumerate(msgs) if m.get("role") == "user"]
    current = users[-1] if users else len(msgs)

    def total() -> int:
        return sum(sizes)

    def shorten(i: int) -> None:
        old = sizes[i]
        msgs[i] = _stubbed(msgs[i])
        sizes[i] = estimate_tokens(msgs[i])
        if sizes[i] < old:
            result.shortened += 1

    # 1. Old tool outputs and file contents, oldest first, never the latest few messages.
    for i in range(1, max(1, len(msgs) - KEEP_RECENT)):
        if total() <= budget:
            break
        if _stubbable(msgs[i]):
            shorten(i)

    # 2. Earlier turns, oldest first, keeping the task (the first user message) and the current turn:
    #    first their tool steps, keeping each turn's request and final answer (a short record of what
    #    was done), then whole turns.
    first = users[0] if users else current
    removable = [i for i in range(first + 1, current)]
    turns = list(_turns(removable, users, current))
    dropped: set[int] = set()

    def fits() -> bool:
        return total() - sum(sizes[i] for i in dropped) <= budget

    for turn_start, turn_end in turns:
        if fits():
            break
        dropped.update(i for i in range(turn_start, turn_end) if _is_tool_step(msgs[i]))
    for turn_start, turn_end in turns:
        if fits():
            break
        dropped.update(range(turn_start, turn_end))
    if dropped:
        result.dropped = len(dropped)
        kept = [m for i, m in enumerate(msgs) if i not in dropped]
        kept_sizes = [s for i, s in enumerate(sizes) if i not in dropped]
        current -= len([i for i in dropped if i < current])
        msgs[:], sizes[:] = kept, kept_sizes
        msgs[0] = {**msgs[0], "content": msgs[0]["content"] + (
            f"\n\nNote: {result.dropped} earlier messages of this conversation were left out to fit your "
            "context. The first user message is the original task.")}
        sizes[0] = estimate_tokens(msgs[0])

    # 3. The current turn: its tool outputs (all but the last), then its message.
    for i in range(current + 1, len(msgs) - 1):
        if total() <= budget:
            break
        if _stubbable(msgs[i]):
            shorten(i)
    if total() > budget and current < len(msgs):
        over = total() - budget
        prompt = msgs[current]
        content = prompt.get("content") or ""
        keep = max(2000, len(content) - int(over * CHARS_PER_TOKEN) - 200)
        if keep < len(content):
            msgs[current] = {**prompt, "content": _keep_ends(content, keep)}
            sizes[current] = estimate_tokens(msgs[current])
            result.cut_prompt = True

    result.messages = msgs
    result.tokens = total()
    return result


def _turns(removable: list[int], users: list[int], current: int):
    """(start, end) index ranges of the removable messages, one per turn, oldest first.

    The first turn's own user message (the task) isn't removable, so its work comes first as a
    range of its own; each later turn runs from its user message to the next one.
    """
    if not removable:
        return
    bounds = [i for i in users if removable[0] <= i < current]
    starts = [removable[0], *[b for b in bounds if b != removable[0]]]
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else current
        if end > start:
            yield start, end


def _is_tool_step(message: dict[str, Any]) -> bool:
    """A tool call or its result: dropped together, so every result left still follows its call."""
    return message.get("role") == "tool" or (message.get("role") == "assistant" and bool(message.get("tool_calls")))


def _stubbable(message: dict[str, Any]) -> bool:
    if message.get("role") == "tool":
        return len(message.get("content") or "") > MIN_CUT
    if message.get("role") == "assistant" and message.get("tool_calls"):
        return any(len(str(call["function"].get("arguments", {}).get(key, ""))) > MIN_CUT
                   for call in message["tool_calls"] if isinstance(call["function"].get("arguments"), dict)
                   for key in CONTENT_ARGS)
    return False


def _stubbed(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("role") == "tool":
        content = message.get("content") or ""
        return {**message, "content": content[:STUB_CHARS] +
                f"\n[… {len(content) - STUB_CHARS} more characters left out to save space; run the tool again if needed]"}
    calls = []
    for call in message["tool_calls"]:
        args = call["function"].get("arguments")
        if isinstance(args, dict):
            args = {key: (f"[{len(value)} characters, left out to save space]"
                          if key in CONTENT_ARGS and isinstance(value, str) and len(value) > MIN_CUT else value)
                    for key, value in args.items()}
        calls.append({**call, "function": {**call["function"], "arguments": args}})
    return {**message, "tool_calls": calls}


def _keep_ends(text: str, keep: int) -> str:
    head = keep // 2
    tail = keep - head
    return (f"{text[:head]}\n\n[… {len(text) - keep} characters left out here to fit the local model's "
            f"context …]\n\n{text[-tail:]}")
