"""Agent commands (revision 4): routing `/name` messages to each agent's own commands."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.codex import CodexAdapter
from app.adapters.codex_server import AppServer
from app.adapters.runner import HostRunner

from app.adapters.base import EventType
from app.adapters.claude import TurnParser
from app.commands import route
from app.models import AgentKind

from conftest import FAKE_CODEX
from test_handoff import events_of, prompts_sent
from test_orchestrator import create, wait_until_idle
from test_worktrees import git, repo  # noqa: F401 (fixture)

CAPABILITIES = {
    "skills": ["fake-skill"],
    "slash_commands": ["fake-skill", "compact", "context", "usage", "code-review", "clear", "model", "doctor",
                       "__internal", "hub:greet"],
    "terminal_slash_commands": ["doctor"],
}


def claude_args(client: TestClient, session_id: str) -> list[list[str]]:
    """The arguments of each fake Claude run that echoed them (not compactions, which reply with no text)."""
    return [json.loads(e["payload"]["text"])["args"] for e in events_of(client, session_id)
            if e["type"] == "completed" and e["agent"] == "claude" and e["payload"]["text"]]


def notices(client: TestClient, session_id: str) -> list[dict]:
    return [e["payload"] for e in events_of(client, session_id) if e["type"] == "notice"]


def test_route_follows_claudes_own_list() -> None:
    def action(text: str, caps: dict | None = CAPABILITIES) -> tuple[str, bool] | None:
        r = route(AgentKind.CLAUDE, text, caps)
        return (r.action, r.local) if r else None

    assert action("/compact keep the tests") == ("agent", False)
    assert action("/context") == ("agent", True) and action("/usage") == ("agent", True)
    assert action("/code-review") == ("agent", False) and action("/fake-skill x") == ("agent", False)
    for hidden in ("/clear", "/model opus", "/doctor"):
        assert action(hidden) == ("notice", False)
    assert action("/__internal") is None and action("/hub:greet") is None  # internals and the hub's own plugin
    assert action("/greet") is None and action("fix /compact") is None  # a library skill, plain text
    assert action("/code-review", None) == ("agent", False)  # nothing recorded yet: the commands every agent has


def test_claude_compacts_its_own_conversation(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "build the thing"})
    wait_until_idle(client, session_id)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/compact keep the tests"})
    session = wait_until_idle(client, session_id)

    assert session["status"] == "completed" and len(session["stints"]) == 1  # no handoff: Claude's own
    [notice] = notices(client, session_id)
    assert notice["reason"] == "compacted" and notice["trigger"] == "manual"
    assert notice["message"] == "Conversation compacted · 28.8k → 7.1k tokens"
    typed = [e["payload"]["text"] for e in events_of(client, session_id) if e["type"] == "user_message"]
    assert typed == ["build the thing", "/compact keep the tests"]

    client.post(f"/api/sessions/{session_id}/messages", json={"text": "next step"})
    wait_until_idle(client, session_id)
    first, last = claude_args(client, session_id)
    assert "--resume" in last and last[last.index("--resume") + 1] == first[first.index("--session-id") + 1]


def test_compact_before_the_conversation_starts_sends_nothing(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/compact"})
    assert "Nothing to compact" in notices(client, session_id)[0]["message"]
    assert not any(e["type"] in ("user_message", "started") for e in events_of(client, session_id))


def test_local_command_before_the_conversation_is_a_one_off(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/context"})
    session = wait_until_idle(client, session_id)
    assert session["native_session_id"] is None  # nothing saved to resume
    [args] = claude_args(client, session_id)
    assert "--no-session-persistence" in args and "--session-id" not in args
    assert prompts_sent(client, session_id, "claude") == ["/context"]

    client.post(f"/api/sessions/{session_id}/messages", json={"text": "hello"})
    session = wait_until_idle(client, session_id)
    assert "--session-id" in claude_args(client, session_id)[-1] and session["native_session_id"]


def test_hub_controlled_commands_answer_without_sending(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi"})
    wait_until_idle(client, session_id)  # Claude's list is recorded from this turn
    for text in ("/clear", "/model opus", "/doctor"):
        client.post(f"/api/sessions/{session_id}/messages", json={"text": text})
    messages = [n["message"] for n in notices(client, session_id)]
    assert "new chat" in messages[0] and "model picker" in messages[1] and "terminal" in messages[2]
    assert prompts_sent(client, session_id, "claude") == ["hi"]

    body = client.get("/api/skills/commands", params={"agent": "claude", "session_id": session_id}).json()
    names = [c["name"] for c in body["commands"]]
    assert names[:4] == ["compact", "context", "usage", "code-review"]
    assert not {"clear", "model", "doctor", "__internal"} & set(names)


def test_command_after_a_waiting_handoff_keeps_the_brief_behind_it(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok", agent="codex")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "build the thing"})
    wait_until_idle(client, session_id)
    response = client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "claude", "wait": True})
    assert response.status_code == 202, response.text
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/code-review the parser"})
    wait_until_idle(client, session_id)
    [prompt] = prompts_sent(client, session_id, "claude")
    assert prompt.startswith("/code-review the parser\n\n") and "build the thing" in prompt


def test_parser_reports_claudes_own_compaction() -> None:
    parser = TurnParser()
    [event] = parser.parse({"type": "system", "subtype": "compact_boundary",
                            "compact_metadata": {"trigger": "auto", "pre_tokens": 190000, "post_tokens": 900}})
    assert event.type is EventType.NOTICE
    assert event.data["message"] == "Claude compacted the conversation on its own · 190.0k → 900 tokens"
    assert parser.parse({"type": "system", "subtype": "status", "status": "compacting"}) == []


# --- Codex (through its app server) ---


def codex_session(client: TestClient, path: Path, mode: str = "ok", **extra) -> str:
    """A Codex session that has had one turn, so it has a thread."""
    session_id = create(client, path, mode, agent="codex", **extra)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "build the thing"})
    wait_until_idle(client, session_id)
    return session_id


def send(client: TestClient, session_id: str, text: str) -> dict:
    response = client.post(f"/api/sessions/{session_id}/messages", json={"text": text})
    assert response.status_code in (200, 202), response.text
    return wait_until_idle(client, session_id)


def last_message(client: TestClient, session_id: str) -> str:
    return [e["payload"]["text"] for e in events_of(client, session_id) if e["type"] == "message"][-1]


def test_codex_routes() -> None:
    def action(text: str):
        r = route(AgentKind.CODEX, text)
        return r and (r.action, r.adapter, r.local)

    assert action("/compact") == ("agent", True, False) and action("/code-review x") == ("agent", True, False)
    assert action("/usage") == ("agent", True, True) and action("/status") == ("agent", True, True)
    assert action("/diff") == ("hub", False, False)
    assert action("/model") == ("notice", False, False) and action("/clear")[0] == "notice"
    init = route(AgentKind.CODEX, "/init keep it short")
    assert init.action == "agent" and "AGENTS.md" in init.prompt and init.prompt.endswith("keep it short")
    assert action("/imagegen a cat") is None  # a Codex skill: sent as $imagegen by expand_command


def test_codex_compacts_through_its_app_server(client: TestClient, tmp_path: Path) -> None:
    session_id = codex_session(client, tmp_path)
    session = send(client, session_id, "/compact")
    assert session["status"] == "completed" and len(session["stints"]) == 1  # Codex's own, no handoff
    [notice] = notices(client, session_id)
    assert notice["reason"] == "compacted" and notice["message"] == "Conversation compacted · was 16.8k tokens"
    assert [e["payload"]["text"] for e in events_of(client, session_id) if e["type"] == "user_message"] == [
        "build the thing", "/compact"]


def test_codex_reports_context_usage_status_and_mcp(client: TestClient, tmp_path: Path) -> None:
    session_id = codex_session(client, tmp_path)
    send(client, session_id, "/context")
    assert "16.8k of 258.4k tokens used (7%)" in last_message(client, session_id)
    send(client, session_id, "/usage")
    usage = last_message(client, session_id)
    assert "5-hour window:** 25% used" in usage and "Weekly window:** 12% used" in usage
    assert "codex" in json.dumps(client.get("/api/usage").json())
    send(client, session_id, "/status")
    status = last_message(client, session_id)
    assert "**Sandbox:** workspace-write" in status and "01a17487" in status and "25% used" in status
    send(client, session_id, "/mcp")
    assert "| docs | 2 | not needed |" in last_message(client, session_id)
    assert "acct-must-not-be-kept" not in json.dumps(events_of(client, session_id))


def test_codex_review_runs_inline_and_can_be_focused(client: TestClient, tmp_path: Path) -> None:
    session_id = codex_session(client, tmp_path)
    send(client, session_id, "/code-review")
    findings = last_message(client, session_id)
    assert findings.startswith("- [P1] Restore addition") and '"uncommittedChanges"' in findings
    shell = [e for e in events_of(client, session_id) if e["type"] == "tool_use"][-1]
    assert shell["payload"]["name"] == "Shell" and shell["payload"]["input"]["command"] == "git diff"
    send(client, session_id, "/review the parser only")
    assert '"custom"' in last_message(client, session_id) and "the parser only" in last_message(client, session_id)
    assert client.get(f"/api/sessions/{session_id}").json()["status"] == "completed"


def test_codex_review_before_a_thread_is_a_one_off(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok", agent="codex")
    session = send(client, session_id, "/code-review")
    assert session["native_session_id"] is None  # the throwaway thread isn't kept
    assert "Restore addition" in last_message(client, session_id)


def test_codex_review_in_a_worktree_covers_the_branch(client: TestClient, repo: Path) -> None:  # noqa: F811
    session_id = codex_session(client, repo)
    send(client, session_id, "/code-review")
    base = git(repo, "rev-parse", "HEAD").strip()
    assert f'{{"type": "baseBranch", "branch": "{base}"}}' in last_message(client, session_id)


def test_codex_diff_is_answered_by_the_hub(client: TestClient, repo: Path) -> None:  # noqa: F811
    session_id = codex_session(client, repo, use_worktree=False)
    (repo / "README.md").write_text("hello\nworld\n")
    before = client.get(f"/api/sessions/{session_id}").json()["status"]
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/diff"})
    reply = last_message(client, session_id)
    assert "## Changes since the last commit" in reply and "+world" in reply and "M README.md" in reply
    assert client.get(f"/api/sessions/{session_id}").json()["status"] == before  # no turn ran


def test_codex_init_sends_the_hubs_request(client: TestClient, tmp_path: Path) -> None:
    session_id = codex_session(client, tmp_path)
    send(client, session_id, "/init")
    assert "Create an AGENTS.md file" in prompts_sent(client, session_id, "codex")[-1]


def test_codex_menu_lists_its_commands(client: TestClient, tmp_path: Path) -> None:
    session_id = codex_session(client, tmp_path)
    body = client.get("/api/skills/commands", params={"agent": "codex", "session_id": session_id}).json()
    names = [c["name"] for c in body["commands"]]
    assert names[:4] == ["compact", "context", "usage", "code-review"]
    assert {"review", "status", "mcp", "diff", "init"} <= set(names) and "model" not in names
    fresh = client.get("/api/skills/commands", params={"agent": "codex"}).json()
    assert not {"compact", "diff"} & {c["name"] for c in fresh["commands"]}


def test_codex_limit_during_a_command(tmp_path: Path) -> None:
    adapter = CodexAdapter(tmp_path, native_session_id="t1", command=[sys.executable, str(FAKE_CODEX), "limit"],
                           codex_home=tmp_path / "codex-home")

    async def run():
        return [e async for e in adapter.run_command("/compact")]

    assert [e.type for e in asyncio.run(run())] == [EventType.LIMIT_REACHED]


def test_app_server_never_asks_for_the_account(tmp_path: Path) -> None:
    async def run():
        async with AppServer([sys.executable, str(FAKE_CODEX), "ok"], tmp_path, HostRunner()) as server:
            with pytest.raises(ValueError):
                await server.request("account/read")
            return await server.request("account/rateLimits/read")

    assert asyncio.run(run())["rateLimits"]["planType"] == "plus"


# --- The local model (the hub's own commands) -----------------------------------------------------


def local_session(client: TestClient, path: Path, **extra) -> str:
    """A local-model session that has had one turn, so it has a saved conversation."""
    session_id = create(client, path, "ollama:ok", agent="ollama", **extra)
    send(client, session_id, "build the thing")
    return session_id


def saved_conversation(path: Path, session: dict) -> list[dict]:
    file = path / "local-history" / f"{session['native_session_id']}.json"
    return json.loads(file.read_text(encoding="utf-8"))["messages"]


def test_local_routes() -> None:
    def action(text: str):
        r = route(AgentKind.OLLAMA, text)
        return r and (r.action, r.adapter, r.local, r.review, r.rest)

    assert action("/compact the parser") == ("agent", True, False, False, "the parser")
    assert action("/context") == ("agent", True, True, False, "")
    assert action("/usage") == ("agent", True, True, False, "")
    assert action("/code-review the tests") == ("agent", False, False, True, "the tests")
    assert action("/clear") is None and action("/greet") is None  # not offered; a library skill


def test_local_compact_replaces_the_saved_conversation(client: TestClient, tmp_path: Path) -> None:
    session_id = local_session(client, tmp_path)
    session = send(client, session_id, "/compact the parser")
    assert session["status"] == "completed" and len(session["stints"]) == 1  # no handoff any more
    [notice] = notices(client, session_id)
    assert notice["reason"] == "compacted" and notice["by"] == "model"
    assert notice["message"].startswith("Conversation compacted · ") and notice["post_tokens"] > 0
    # The fake echoes the request: the transcript of the conversation and the focus text.
    assert notice["summary"].startswith("Echo: ### User\nbuild the thing") and "the parser" in notice["summary"]
    [compacted] = saved_conversation(tmp_path, session)
    assert compacted["role"] == "user" and compacted["content"].startswith("# Compacted conversation")
    assert compacted["content"].endswith("## Keep in focus\n\nthe parser")

    send(client, session_id, "what next?")
    assert last_message(client, session_id) == "Echo: what next? (3 messages)"  # system, summary, the new message


def test_local_compact_falls_back_to_the_hubs_brief(tmp_path: Path) -> None:
    from app.adapters.ollama import OllamaAdapter, save_history

    import fake_ollama

    conversation = "6f1c1d1e-0000-4000-8000-000000000001"
    history = tmp_path / "history"
    save_history(history / f"{conversation}.json", "m", [{"role": "user", "content": "task"},
                                                          {"role": "assistant", "content": "done"}])

    def adapter(fallback):
        a = OllamaAdapter(tmp_path, "m", conversation, history_dir=history,
                          transport=fake_ollama.FakeOllama("error").transport)
        a.compact_fallback = fallback
        return a

    async def run(a):
        return [e async for e in a.run_command("/compact")]

    [error] = asyncio.run(run(adapter(None)))
    assert error.type is EventType.ERROR and "wasn't compacted" in error.data["message"]

    async def brief() -> str:
        return "# Compacted conversation\n\nthe hub's brief"

    events = asyncio.run(run(adapter(brief)))
    notice = next(e for e in events if e.type is EventType.NOTICE)
    assert notice.data["by"] == "hub" and "the hub's summary was used" in notice.data["message"]
    assert events[-1].type is EventType.COMPLETED
    saved = json.loads((history / f"{conversation}.json").read_text(encoding="utf-8"))["messages"]
    assert saved == [{"role": "user", "content": "# Compacted conversation\n\nthe hub's brief"}]


def test_local_compact_brief_comes_from_the_session(client: TestClient, tmp_path: Path) -> None:
    session_id = local_session(client, tmp_path)
    brief = asyncio.run(client.app.state.orchestrator._compact_brief(session_id, "the parser"))
    assert brief.startswith("# Compacted conversation") and "build the thing" in brief
    assert "## Keep in focus\n\nthe parser" in brief


def test_local_context_and_usage(client: TestClient, tmp_path: Path) -> None:
    fresh = create(client, tmp_path, "ollama:ok", agent="ollama")
    session = send(client, fresh, "/context")
    assert "## Local model context" in last_message(client, fresh) and "Conversation (0 messages)" in last_message(
        client, fresh)
    assert session["native_session_id"] is None  # nothing was started

    (tmp_path / "second").mkdir()
    session_id = local_session(client, tmp_path / "second")
    send(client, session_id, "/context")
    context = last_message(client, session_id)
    assert "of 32.8k tokens" in context and "| Tool definitions |" in context and "Conversation (2 messages)" in context
    send(client, session_id, "/usage")
    assert "no usage limits" in last_message(client, session_id)
    assert len(saved_conversation(tmp_path / "second", client.get(f"/api/sessions/{session_id}").json())) == 2


def test_local_code_review_sends_the_diff(client: TestClient, repo: Path) -> None:  # noqa: F811
    (repo / ".git" / "info" / "exclude").write_text("local-history/\n")  # where the test factory saves conversations
    session_id = local_session(client, repo, use_worktree=False)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/code-review"})
    assert notices(client, session_id)[-1]["message"] == "No changes to review: the working tree is clean."

    (repo / "README.md").write_text("hello\nworld\n")
    (repo / "new.py").write_text("x = 1\n")
    send(client, session_id, "/code-review the readme")
    request = last_message(client, session_id)
    assert request.startswith("Echo: Review the code changes below for bugs.") and "Focus on: the readme" in request
    assert "+world" in request and "New files, not in the diff (read them): new.py" in request
    assert [e["payload"]["text"] for e in events_of(client, session_id) if e["type"] == "user_message"][-1] == (
        "/code-review the readme")  # the transcript keeps what was typed


def test_local_code_review_outside_git(client: TestClient, tmp_path: Path) -> None:
    session_id = local_session(client, tmp_path)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/code-review"})
    assert "isn't a git repository" in notices(client, session_id)[-1]["message"]


def test_local_menu_lists_its_commands(client: TestClient, tmp_path: Path) -> None:
    session_id = local_session(client, tmp_path)
    body = client.get("/api/skills/commands", params={"agent": "ollama", "session_id": session_id}).json()
    assert [(c["name"], c["kind"]) for c in body["commands"]][:4] == [
        ("compact", "hub"), ("context", "hub"), ("usage", "hub"), ("code-review", "hub")]
    fresh = client.get("/api/skills/commands", params={"agent": "ollama"}).json()
    assert [c["name"] for c in fresh["commands"]] == ["context", "usage", "code-review"]
