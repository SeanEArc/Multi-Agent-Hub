"""Stand-in for the `claude` CLI. Usage: python fake_claude.py <mode> [claude args...]

Modes: ok, question, limit, badmodel, crash, hang. Echoes its args, stdin prompt and working folder back in the
result so tests can check what the adapter sent.

Like the real CLI with `--input-format stream-json`, stdin is one JSON line per turn, `{"type": "user", "message":
{"content": [blocks]}}`; `prompt` is its text blocks joined, and `blocks` lists every block's type (and an image's
or document's media type and size), so tests can check what was attached.
"""

import base64
import json
import os
import sys
import time


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


mode, args = sys.argv[1], sys.argv[2:]
stdin = sys.stdin.read()
blocks: list[dict] = []
if "--input-format" in args and args[args.index("--input-format") + 1] == "stream-json":
    message = json.loads(stdin.splitlines()[0])
    content = message["message"]["content"]
    content = [{"type": "text", "text": content}] if isinstance(content, str) else content
    prompt = "\n".join(b["text"] for b in content if b["type"] == "text")
    for b in content:
        source = b.get("source") or {}
        blocks.append({"type": b["type"], "media_type": source.get("media_type"),
                       "bytes": len(base64.b64decode(source["data"])) if source.get("data") else None})
else:  # the hub always sends stream-json now; a plain prompt means an adapter path was missed
    print("fake claude: expected --input-format stream-json", file=sys.stderr)
    sys.exit(4)
if "--session-id" in args:
    session_id = args[args.index("--session-id") + 1]
elif "--resume" in args:
    session_id = args[args.index("--resume") + 1]
else:  # a one-off call that isn't saved (--no-session-persistence)
    session_id = "00000000-0000-4000-8000-000000000000"

emit({"type": "system", "subtype": "init", "session_id": session_id, "model": "fake", "permissionMode": "acceptEdits",
      "skills": ["fake-skill"], "slash_commands": ["fake-skill", "compact", "context", "usage", "code-review", "clear", "model", "doctor",
                         "__internal"],
      "terminal_slash_commands": ["doctor"], "mcp_servers": [{"name": "fake-mcp", "status": "connected"}],
      "tools": ["Read", "Skill"]})
if mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(2)
if mode == "hang":
    time.sleep(60)
if mode == "badmodel":  # the plan can't use the model asked for, as Claude Code words it
    emit({"type": "result", "subtype": "success", "is_error": True, "num_turns": 0,
          "result": "There's an issue with the selected model (x). It may not exist or you may not have access to it. "
                    "Run --model to pick a different model."})
    sys.exit(1)
if mode == "limit":
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 4102444800}})
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your limit", "num_turns": 1})
    sys.exit(1)

if prompt.startswith("/compact"):  # as Claude Code 2.1.323 reports a compaction
    emit({"type": "system", "subtype": "status", "status": "compacting", "session_id": session_id})
    emit({"type": "system", "subtype": "compact_boundary", "session_id": session_id,
          "compact_metadata": {"trigger": "manual", "pre_tokens": 28812, "post_tokens": 7124}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "", "num_turns": 0})
    sys.exit(0)

emit({"type": "assistant", "message": {"content": [{"type": "text", "text": f"echo: {prompt}"}]}, "parent_tool_use_id": None})
text = json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd(), "blocks": blocks})
if mode == "question":
    text += "\n[[NEEDS_INPUT]]"
emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": 0.0, "num_turns": 1})
