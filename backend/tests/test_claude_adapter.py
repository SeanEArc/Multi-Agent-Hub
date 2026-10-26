import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.adapters.base import AgentEvent, EventType
from app.adapters.claude import ClaudeAdapter, TurnParser

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


def collect(events: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    async def run() -> list[AgentEvent]:
        return [event async for event in events]

    return asyncio.run(run())


def fake_adapter(mode: str, tmp_path: Path, **kwargs) -> ClaudeAdapter:
    return ClaudeAdapter(tmp_path, command=[sys.executable, str(FAKE_CLAUDE), mode], **kwargs)


def parse(*raw_events: dict) -> list[AgentEvent]:
    parser = TurnParser()
    return [event for raw in raw_events for event in parser.feed(json.dumps(raw).encode())]


# --- Parser (event shapes recorded from Claude Code 2.1.305) -----------------


def test_parses_a_tool_turn() -> None:
    events = parse(
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-haiku-5-5", "permissionMode": "acceptEdits"},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50},
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "thinking", "thinking": ""},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git --version"}},
        ]}},
        {"type": "system", "subtype": "permission_denied", "tool_name": "Bash", "tool_use_id": "t1",
         "decision_reason": "no approval surface in this session"},
        {"type": "user", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "Permission denied"},
        ]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "I couldn't run it."}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "I couldn't run it.",
         "total_cost_usd": 0.0027, "num_turns": 2, "permission_denials": [{"tool_name": "Bash"}]},
    )
    assert [e.type for e in events] == [
        EventType.STARTED,
        EventType.TOOL_USE,
        EventType.PERMISSION_DENIED,
        EventType.TOOL_RESULT,
        EventType.MESSAGE,
        EventType.USAGE,
        EventType.COMPLETED,
    ]
    assert events[0].data["session_id"] == "s1"
    assert events[1].data["input"] == {"command": "git --version"}
    assert events[3].data["is_error"] is True
    assert events[-1].data["permission_denials"] == 1
    assert events[0].raw is not None


def test_todo_write_becomes_a_plan_event() -> None:
    todos = [{"content": "Read the model", "status": "completed", "activeForm": "Reading the model"},
             {"content": "Write tests", "status": "in_progress", "activeForm": "Writing tests"}]
    events = parse(
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {"todos": todos}},
        ]}},
        # A subagent's own to-do list stays a tool call.
        {"type": "assistant", "parent_tool_use_id": "t0", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "TodoWrite", "input": {"todos": todos}},
        ]}},
    )
    assert [e.type for e in events] == [EventType.PLAN, EventType.TOOL_USE]
    assert events[0].data["items"] == [{"text": "Read the model", "status": "completed"},
                                       {"text": "Write tests", "status": "in_progress"}]


def test_marker_turns_result_into_question_and_is_hidden() -> None:
    text = "What is your favorite color?\n\n[[NEEDS_INPUT]]"
    events = parse(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": text},
    )
    assert events[0].data["text"] == "What is your favorite color?"
    assert events[-1].type is EventType.QUESTION
    assert events[-1].data["text"] == "What is your favorite color?"


def test_rate_limit_status() -> None:
    allowed = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1792558200,
        "unifiedWindows": {"five_hour": {"utilization": 0.07}}}}
    rejected = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}}
    error_result = {"type": "result", "subtype": "success", "is_error": True, "result": "limit hit"}

    (event,) = parse(allowed)
    assert event.type is EventType.RATE_LIMIT
    assert event.data["windows"]["five_hour"]["utilization"] == 0.07

    # A rejected window is reported once; the failing result after it adds no second error.
    assert [e.type for e in parse(rejected, error_result)] == [EventType.LIMIT_REACHED, EventType.USAGE]


def test_limit_detected_from_error_text() -> None:
    events = parse({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached"})
    assert events[-1].type is EventType.LIMIT_REACHED


def test_other_errors() -> None:
    events = parse({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})
    assert events[-1].type is EventType.ERROR
    assert events[-1].data["subtype"] == "error_max_turns"


def test_unparseable_line() -> None:
    (event,) = TurnParser().feed(b"not json\n")
    assert event.type is EventType.ERROR


# --- Process handling (fake CLI) ---------------------------------------------


def test_start_then_reply_uses_same_session(tmp_path: Path) -> None:
    adapter = fake_adapter("ok", tmp_path, model="haiku")
    first = collect(adapter.start_task("hello there"))
    assert first[-1].type is EventType.COMPLETED
    sent = json.loads(first[-1].data["text"])
    assert sent["prompt"] == "hello there"
    assert sent["args"][sent["args"].index("--session-id") + 1] == adapter.native_session_id
    assert sent["args"][sent["args"].index("--permission-prompts") + 1] == "none"
    assert sent["args"][sent["args"].index("--model") + 1] == "haiku"

    second = collect(adapter.send_reply('blue & "quotes"'))
    sent = json.loads(second[-1].data["text"])
    assert sent["prompt"] == 'blue & "quotes"'
    assert sent["args"][sent["args"].index("--resume") + 1] == adapter.native_session_id


def test_question_turn(tmp_path: Path) -> None:
    events = collect(fake_adapter("question", tmp_path).start_task("hi"))
    assert events[-1].type is EventType.QUESTION


def test_crash_without_result_is_an_error(tmp_path: Path) -> None:
    events = collect(fake_adapter("crash", tmp_path).start_task("hi"))
    assert events[-1].type is EventType.ERROR
    assert "code 2" in events[-1].data["message"]
    assert events[-1].data["stderr"] == "boom"


def test_stop_kills_a_running_turn(tmp_path: Path) -> None:
    adapter = fake_adapter("hang", tmp_path)

    async def run() -> list[AgentEvent]:
        events = []
        async for event in adapter.start_task("hi"):
            events.append(event)
            if event.type is EventType.STARTED:
                asyncio.get_running_loop().call_later(0.1, lambda: asyncio.ensure_future(adapter.stop()))
        return events

    events = asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert events[-1].type is EventType.ERROR
    assert events[-1].data.get("stopped") is True


def test_reply_before_start_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        fake_adapter("ok", tmp_path).send_reply("hi")
