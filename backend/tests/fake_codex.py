"""Stand-in for the native `codex` CLI. Usage: python fake_codex.py <mode> [codex args...]

Modes: ok, question, tools, limit, crash, hang. Echoes its args and stdin prompt back
in the final agent message so tests can check what the adapter sent. Event shapes
follow codex-cli 0.164.0.
"""

import json
import sys
import time


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def message(item_id: str, text: str) -> None:
    emit({"type": "item.completed", "item": {"id": item_id, "type": "agent_message", "text": text}})


mode, args = sys.argv[1], sys.argv[2:]
prompt = sys.stdin.read()
# A reply resumes the thread named just before the trailing "-"; a new task gets a new id.
thread_id = args[-2] if args[:2] == ["exec", "resume"] else "01a17487-0000-7000-8000-000000000001"

emit({"type": "thread.started", "thread_id": thread_id})
emit({"type": "turn.started"})
if mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(2)
if mode == "hang":
    time.sleep(60)
if mode == "limit":
    text = "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), or try again at 3:00 PM."
    emit({"type": "error", "message": text})
    emit({"type": "turn.failed", "error": {"message": text}})
    sys.exit(1)
if mode == "tools":
    command = {"id": "item_1", "type": "command_execution",
               "command": '"C:\\\\WINDOWS\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" -Command "git status --short"',
               "aggregated_output": "", "exit_code": None, "status": "in_progress"}
    emit({"type": "item.started", "item": command})
    emit({"type": "item.completed", "item": {**command, "aggregated_output": "?? hello.txt\n", "exit_code": 0, "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "item_2", "type": "file_change", "status": "completed",
                                             "changes": [{"path": "hello.txt", "kind": "update"}]}})

text = json.dumps({"args": args, "prompt": prompt})
if mode == "question":
    text += "\n[[NEEDS_INPUT]]"
message("item_9", text)
emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}})
