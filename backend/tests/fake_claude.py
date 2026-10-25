"""Stand-in for the `claude` CLI. Usage: python fake_claude.py <mode> [claude args...]

Modes: ok, question, limit, crash, hang. Echoes its args and stdin prompt back in the
result so tests can check what the adapter sent.
"""

import json
import sys
import time


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


mode, args = sys.argv[1], sys.argv[2:]
prompt = sys.stdin.read()
session_id = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]

emit({"type": "system", "subtype": "init", "session_id": session_id, "model": "fake", "permissionMode": "acceptEdits"})
if mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(2)
if mode == "hang":
    time.sleep(60)
if mode == "limit":
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 4102444800}})
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your limit", "num_turns": 1})
    sys.exit(1)

emit({"type": "assistant", "message": {"content": [{"type": "text", "text": f"echo: {prompt}"}]}, "parent_tool_use_id": None})
text = json.dumps({"args": args, "prompt": prompt})
if mode == "question":
    text += "\n[[NEEDS_INPUT]]"
emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": 0.0, "num_turns": 1})
