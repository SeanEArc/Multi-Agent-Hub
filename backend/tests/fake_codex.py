"""Stand-in for the native `codex` CLI. Usage: python fake_codex.py <mode> [codex args...]

Modes: ok, question, tools, limit, crash, hang. Echoes its args and stdin prompt back
in the final agent message so tests can check what the adapter sent. Event shapes
follow codex-cli 0.164.0. `tools` writes hello.txt in its cwd, like a real patch.

`app-server` answers the JSON-RPC requests the hub sends (revision 4), as codex-cli 0.164.0 does; in `limit` mode
compactions and reviews fail with the usage-limit message. `account/read` is never allowed: it exits with code 9.
"""

import json
import sys
import time
from pathlib import Path


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def message(item_id: str, text: str) -> None:
    emit({"type": "item.completed", "item": {"id": item_id, "type": "agent_message", "text": text}})


mode, args = sys.argv[1], sys.argv[2:]
LIMIT_TEXT = "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), or try again at 3:00 PM."


def app_server() -> None:
    def reply(request_id, result=None, error=None):
        emit({"id": request_id, **({"error": {"code": -1, "message": error}} if error else {"result": result or {}})})

    def notify(method, params):
        emit({"method": method, "params": params})

    def turn(thread, turn_id, items):
        notify("turn/started", {"threadId": thread, "turn": {"id": turn_id, "status": "inProgress"}})
        for item in items:
            notify("item/started", {"threadId": thread, "item": item})
            notify("item/completed", {"threadId": thread, "item": item})
        if mode == "limit":
            notify("error", {"threadId": thread, "error": {"message": LIMIT_TEXT}})
            notify("turn/completed", {"threadId": thread, "turn": {"id": turn_id, "status": "failed",
                                                                   "error": {"message": LIMIT_TEXT}}})
        else:
            notify("turn/completed", {"threadId": thread, "turn": {"id": turn_id, "status": "completed"}})

    for line in sys.stdin:
        message = json.loads(line)
        method, params, request_id = message.get("method"), message.get("params") or {}, message.get("id")
        if request_id is None:
            continue  # the `initialized` notification
        if method == "account/read":
            sys.exit(9)
        if method == "initialize":
            reply(request_id, {"userAgent": "fake"})
        elif method == "thread/resume":
            reply(request_id, {"thread": {"id": params["threadId"]}})
            notify("thread/tokenUsage/updated", {"threadId": params["threadId"], "turnId": "t0", "tokenUsage": {
                "total": {"totalTokens": 30000}, "last": {"totalTokens": 16844}, "modelContextWindow": 258400}})
        elif method == "thread/start":
            reply(request_id, {"thread": {"id": "ephemeral-thread"}})
        elif method == "thread/compact/start":
            reply(request_id, {})
            turn(params["threadId"], "compact-1", [{"type": "contextCompaction", "id": "c1"}])
        elif method == "review/start":
            reply(request_id, {"turn": {"id": "review-1"}, "reviewThreadId": params["threadId"]})
            findings = f"- [P1] Restore addition in add. target={json.dumps(params['target'])} args={json.dumps(args)}"
            turn(params["threadId"], "review-1", [
                {"type": "commandExecution", "id": "x1", "command": "git diff", "aggregatedOutput": "diff", "exitCode": 0},
                {"type": "exitedReviewMode", "id": "r1", "review": findings},
                {"type": "agentMessage", "id": "m1", "text": findings},
            ])
        elif method == "account/rateLimits/read":
            reply(request_id, {"accountId": "acct-must-not-be-kept", "rateLimits": {
                "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 4102444800},
                "secondary": {"usedPercent": 12, "windowDurationMins": 10080, "resetsAt": 4102444800},
                "planType": "plus", "rateLimitReachedType": None}})
        elif method == "mcpServerStatus/list":
            reply(request_id, {"data": [{"name": "docs", "tools": {"search": {}, "fetch": {}}, "authStatus": "unsupported"}]})
        else:
            reply(request_id, error=f"unknown method {method}")


if args[:1] == ["app-server"]:
    app_server()
    sys.exit(0)

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
    text = LIMIT_TEXT
    emit({"type": "error", "message": text})
    emit({"type": "turn.failed", "error": {"message": text}})
    sys.exit(1)
if mode == "tools":
    command = {"id": "item_1", "type": "command_execution",
               "command": '"C:\\\\WINDOWS\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" -Command "git status --short"',
               "aggregated_output": "", "exit_code": None, "status": "in_progress"}
    emit({"type": "item.started", "item": command})
    emit({"type": "item.completed", "item": {**command, "aggregated_output": "?? hello.txt\n", "exit_code": 0, "status": "completed"}})
    Path("hello.txt").write_text("hello\n")
    emit({"type": "item.completed", "item": {"id": "item_2", "type": "file_change", "status": "completed",
                                             "changes": [{"path": "hello.txt", "kind": "add"}]}})

text = json.dumps({"args": args, "prompt": prompt})
if mode == "question":
    text += "\n[[NEEDS_INPUT]]"
message("item_9", text)
emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}})
