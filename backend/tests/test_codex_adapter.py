import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.adapters.base import AgentEvent, EventType
from app.adapters.codex import ROLLOUT_TAIL, CodexAdapter, CodexCliNotFound, TurnParser, find_codex, read_model, read_rate_limits

FAKE_CODEX = Path(__file__).parent / "fake_codex.py"
THREAD = "01a17487-9cbe-79c3-97d4-1c2c350d55a1"
FAKE_THREAD = "01a17487-0000-7000-8000-000000000001"  # the id fake_codex.py reports
# Recorded from a codex-cli 0.164.0 session file (Plus plan).
RATE_LIMITS = {"limit_id": "codex", "primary": {"used_percent": 1.0, "window_minutes": 300, "resets_at": 1792968943},
               "secondary": {"used_percent": 0.0, "window_minutes": 10080, "resets_at": 1793555743},
               "plan_type": "plus", "rate_limit_reached_type": None}


def collect(events: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    async def run() -> list[AgentEvent]:
        return [event async for event in events]

    return asyncio.run(run())


def fake_adapter(mode: str, tmp_path: Path, **kwargs) -> CodexAdapter:
    kwargs.setdefault("codex_home", tmp_path / "codex-home")
    return CodexAdapter(tmp_path, command=[sys.executable, str(FAKE_CODEX), mode], **kwargs)


def write_rollout(codex_home: Path, thread_id: str, *payloads: dict) -> None:
    folder = codex_home / "sessions" / "2026" / "10" / "25"
    folder.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"timestamp": "t", "type": "event_msg", "payload": p}) for p in payloads]
    (folder / f"rollout-2026-10-25T14-26-47-{thread_id}.jsonl").write_text("\n".join(lines) + "\n")


def parse(*raw_events: dict, parser: TurnParser | None = None) -> list[AgentEvent]:
    parser = parser or TurnParser()
    return [event for raw in raw_events for event in parser.feed(json.dumps(raw).encode())]


def option(args: list[str], key: str) -> str:
    """The value of a `-c key=value` override."""
    return next(a.split("=", 1)[1] for a in args if a.startswith(f"{key}="))


# --- Parser (event shapes recorded from codex-cli 0.164.0) --------------------

POWERSHELL = '"C:\\\\WINDOWS\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" -Command '


def test_parses_a_command_turn() -> None:
    command = {"id": "item_1", "type": "command_execution",
               "command": POWERSHELL + "\"Set-Content -LiteralPath hello.txt -Value 'hi from codex'\ngit status --short\"",
               "aggregated_output": "", "exit_code": None, "status": "in_progress"}
    events = parse(
        {"type": "thread.started", "thread_id": THREAD},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "I'll create `hello.txt`.\n"}},
        {"type": "item.started", "item": command},
        {"type": "item.completed", "item": {**command, "aggregated_output": "?? hello.txt\n", "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "Created `hello.txt`."}},
        {"type": "turn.completed", "usage": {"input_tokens": 34472, "cached_input_tokens": 30464, "output_tokens": 117}},
    )
    assert [e.type for e in events] == [
        EventType.STARTED,
        EventType.MESSAGE,
        EventType.TOOL_USE,
        EventType.TOOL_RESULT,
        EventType.MESSAGE,
        EventType.USAGE,
        EventType.COMPLETED,
    ]
    assert events[0].data["session_id"] == THREAD
    assert events[2].data["name"] == "Shell"
    assert events[2].data["input"]["command"] == "Set-Content -LiteralPath hello.txt -Value 'hi from codex'\ngit status --short"
    assert events[3].data["tool_use_id"] == events[2].data["tool_use_id"]
    assert events[3].data == {**events[3].data, "is_error": False, "content": "?? hello.txt\n"}
    assert events[5].data["usage"]["output_tokens"] == 117
    assert events[5].data["cumulative"] is True
    assert events[-1].data["text"] == "Created `hello.txt`."
    assert events[0].raw is not None


def test_blocked_command_is_a_failed_tool_result() -> None:
    command = {"id": "item_1", "type": "command_execution", "command": "Set-Content ../outside/escape2.txt",
               "aggregated_output": "Access to the path ... is denied.", "exit_code": 1, "status": "failed"}
    (use, result) = parse({"type": "item.completed", "item": command})
    assert use.type is EventType.TOOL_USE
    assert result.type is EventType.TOOL_RESULT
    assert result.data["is_error"] is True


def test_file_change() -> None:
    change = {"id": "item_1", "type": "file_change", "status": "in_progress",
              "changes": [{"path": "C:\\repo\\hello.txt", "kind": "update"}]}
    events = parse(
        {"type": "item.started", "item": change},
        {"type": "item.completed", "item": {**change, "status": "completed"}},
    )
    assert [e.type for e in events] == [EventType.TOOL_USE, EventType.TOOL_RESULT]
    assert events[0].data["name"] == "Edit"
    assert events[0].data["input"]["path"] == "C:\\repo\\hello.txt"
    assert events[1].data["content"] == "update: C:\\repo\\hello.txt"


def test_tool_ids_differ_between_turns() -> None:
    item = {"type": "item.completed", "item": {"id": "item_1", "type": "web_search", "query": "x"}}
    first, second = parse(item)[0], parse(item)[0]
    assert first.data["tool_use_id"] != second.data["tool_use_id"]


def test_marker_turns_completion_into_question_and_is_hidden() -> None:
    events = parse(
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
                                            "text": "What name would you like for `hello.txt`?\n[[NEEDS_INPUT]]"}},
        {"type": "turn.completed", "usage": {}},
    )
    assert events[0].data["text"] == "What name would you like for `hello.txt`?"
    assert events[-1].type is EventType.QUESTION
    assert events[-1].data["text"] == "What name would you like for `hello.txt`?"


def test_limit_reported_once() -> None:
    text = "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), or try again at 3:00 PM."
    parser = TurnParser()
    events = parse({"type": "error", "message": text}, {"type": "turn.failed", "error": {"message": text}}, parser=parser)
    assert [e.type for e in events] == [EventType.LIMIT_REACHED]
    assert parser.finished


def test_other_failures_are_errors() -> None:
    events = parse({"type": "turn.failed", "error": {"message": "stream disconnected"}})
    assert [e.type for e in events] == [EventType.ERROR]


def test_reasoning_and_updates_are_ignored() -> None:
    assert parse(
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "thinking"}},
        {"type": "item.updated", "item": {"id": "item_1", "type": "command_execution", "command": "ls"}},
    ) == []


def test_todo_list_becomes_plan_events_when_it_changes() -> None:
    def todo(event: str, *done: bool) -> dict:
        items = [{"text": f"step {i + 1}", "completed": d} for i, d in enumerate(done)]
        return {"type": event, "item": {"id": "item_1", "type": "todo_list", "items": items}}

    events = parse(
        todo("item.started", False, False, False),
        todo("item.updated", True, False, False),
        todo("item.updated", True, False, False),  # unchanged: no new event
        todo("item.completed", True, True, True),
    )
    assert [e.type for e in events] == [EventType.PLAN] * 3
    assert [i["status"] for i in events[0].data["items"]] == ["in_progress", "pending", "pending"]
    assert [i["status"] for i in events[1].data["items"]] == ["completed", "in_progress", "pending"]
    assert events[1].data["items"][1]["text"] == "step 2"
    assert [i["status"] for i in events[2].data["items"]] == ["completed"] * 3


# --- Usage windows from the session file -------------------------------------


def test_reads_latest_rate_limits(tmp_path: Path) -> None:
    newer = {**RATE_LIMITS, "primary": {**RATE_LIMITS["primary"], "used_percent": 7.0}}
    write_rollout(tmp_path, THREAD,
                  {"type": "token_count", "info": None, "rate_limits": RATE_LIMITS},
                  {"type": "token_count", "info": None, "rate_limits": newer},
                  {"type": "agent_message", "message": "done"})
    assert read_rate_limits(tmp_path, THREAD)["primary"]["used_percent"] == 7.0


def turn_context(model: str) -> str:
    return json.dumps({"timestamp": "t", "type": "turn_context", "payload": {"turn_id": "x", "model": model}})


def prepend(rollout: Path, *lines: str) -> None:
    rollout.write_text("".join(f"{line}\n" for line in lines) + rollout.read_text())


def test_reads_model_of_latest_turn(tmp_path: Path) -> None:
    write_rollout(tmp_path, THREAD, {"type": "agent_message", "message": "hi"})
    rollout = next((tmp_path / "sessions").rglob("*.jsonl"))
    assert read_model(tmp_path, THREAD) is None
    prepend(rollout, turn_context("gpt-6-sol"), turn_context("gpt-6.1-sol"))
    assert read_model(tmp_path, THREAD) == "gpt-6.1-sol"


def test_reads_model_beyond_the_tail(tmp_path: Path) -> None:
    """A turn with large outputs pushes its turn_context out of the tail that holds the usage windows."""
    write_rollout(tmp_path, THREAD, {"type": "agent_message", "message": "x" * (ROLLOUT_TAIL + 10)},
                  {"type": "token_count", "rate_limits": RATE_LIMITS})
    rollout = next((tmp_path / "sessions").rglob("*.jsonl"))
    prepend(rollout, turn_context("gpt-6-luna"))
    assert read_model(tmp_path, THREAD) == "gpt-6-luna"
    assert read_rate_limits(tmp_path, THREAD) == RATE_LIMITS


def test_turn_reports_model_from_session_file(tmp_path: Path) -> None:
    write_rollout(tmp_path / "codex-home", FAKE_THREAD, {"type": "token_count", "rate_limits": RATE_LIMITS})
    rollout = next((tmp_path / "codex-home" / "sessions").rglob("*.jsonl"))
    prepend(rollout, turn_context("gpt-6.1-sol"))
    events = collect(fake_adapter("ok", tmp_path).start_task("hi"))
    assert [e.type for e in events[-3:]] == [EventType.COMPLETED, EventType.MODEL, EventType.RATE_LIMIT]
    assert events[-2].data == {"model": "gpt-6.1-sol"}


def test_missing_session_file_or_limits(tmp_path: Path) -> None:
    assert read_rate_limits(tmp_path, THREAD) is None
    write_rollout(tmp_path, THREAD, {"type": "token_count", "info": None, "rate_limits": None})
    assert read_rate_limits(tmp_path, THREAD) is None


def test_turn_ends_with_usage_windows(tmp_path: Path) -> None:
    write_rollout(tmp_path / "codex-home", FAKE_THREAD, {"type": "token_count", "rate_limits": RATE_LIMITS})
    events = collect(fake_adapter("ok", tmp_path).start_task("hi"))
    assert [e.type for e in events[-2:]] == [EventType.COMPLETED, EventType.RATE_LIMIT]
    assert events[-1].data["status"] == "allowed"
    assert events[-1].data["windows"] == {
        "five_hour": {"utilization": 0.01, "resets_at": 1792968943},
        "seven_day": {"utilization": 0.0, "resets_at": 1793555743},
    }


def test_reached_limit_window_is_rejected(tmp_path: Path) -> None:
    write_rollout(tmp_path / "codex-home", FAKE_THREAD,
                  {"type": "token_count", "rate_limits": {**RATE_LIMITS, "rate_limit_reached_type": "primary"}})
    events = collect(fake_adapter("limit", tmp_path).start_task("hi"))
    assert [e.type for e in events] == [EventType.STARTED, EventType.LIMIT_REACHED, EventType.RATE_LIMIT]
    assert events[-1].data["status"] == "rejected"


# --- Finding the binary ------------------------------------------------------


def test_find_codex_uses_native_binary_inside_npm_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = tmp_path / "codex.cmd"
    launcher.write_text("")
    native = tmp_path / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    native.parent.mkdir(parents=True)
    native.write_text("")
    monkeypatch.setattr("shutil.which", lambda name: str(launcher))
    assert find_codex() == str(native)


def test_find_codex_refuses_launcher_without_native_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = tmp_path / "codex.cmd"
    launcher.write_text("")
    monkeypatch.setattr("shutil.which", lambda name: str(launcher))
    with pytest.raises(CodexCliNotFound):
        find_codex()


# --- Process handling (fake CLI) ---------------------------------------------


def test_start_then_reply_resumes_reported_thread(tmp_path: Path) -> None:
    adapter = fake_adapter("ok", tmp_path, model="gpt-6-sol", sandbox="read-only")
    assert adapter.native_session_id is None

    first = collect(adapter.start_task("hello there"))
    assert first[-1].type is EventType.COMPLETED
    sent = json.loads(first[-1].data["text"])
    assert sent["prompt"] == "hello there"
    assert sent["args"][:2] == ["exec", "--json"] and sent["args"][-1] == "-"
    assert option(sent["args"], "sandbox_mode") == '"read-only"'
    assert option(sent["args"], "approval_policy") == '"never"'
    assert "[[NEEDS_INPUT]]" in option(sent["args"], "developer_instructions")
    assert sent["args"][sent["args"].index("--model") + 1] == "gpt-6-sol"
    assert adapter.native_session_id == first[0].data["session_id"]

    second = collect(adapter.send_reply('blue & "quotes"'))
    sent = json.loads(second[-1].data["text"])
    assert sent["prompt"] == 'blue & "quotes"'
    assert sent["args"][:3] == ["exec", "resume", "--json"]
    assert sent["args"][-2:] == [adapter.native_session_id, "-"]


def test_question_turn(tmp_path: Path) -> None:
    events = collect(fake_adapter("question", tmp_path).start_task("hi"))
    assert events[-1].type is EventType.QUESTION


def test_tool_turn(tmp_path: Path) -> None:
    types = [e.type for e in collect(fake_adapter("tools", tmp_path).start_task("hi"))]
    assert types.count(EventType.TOOL_USE) == 2
    assert types.count(EventType.TOOL_RESULT) == 2


def test_limit_turn_has_no_extra_exit_error(tmp_path: Path) -> None:
    events = collect(fake_adapter("limit", tmp_path).start_task("hi"))
    assert [e.type for e in events] == [EventType.STARTED, EventType.LIMIT_REACHED]


def test_crash_without_result_is_an_error(tmp_path: Path) -> None:
    events = collect(fake_adapter("crash", tmp_path).start_task("hi"))
    assert events[-1].type is EventType.ERROR
    assert "codex exited with code 2" in events[-1].data["message"]
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
