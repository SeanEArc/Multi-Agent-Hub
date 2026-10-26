import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.adapters.base import EventType
from app.handoff import MAX_BRIEF, GitState, build_brief, git_state, next_in_chain
from app.models import AgentKind, ChainEntry, SessionEvent, StintReason

from test_orchestrator import create, event_types, wait_until_idle

CODEX_THEN_CLAUDE = [{"agent": "codex"}, {"agent": "claude"}]


def events_of(client: TestClient, session_id: str) -> list[dict]:
    return client.get(f"/api/sessions/{session_id}/events").json()


def prompts_sent(client: TestClient, session_id: str, agent: str) -> list[str]:
    """What the fake CLIs received over stdin, from the text they echo back."""
    prompts = []
    for e in events_of(client, session_id):
        if e["type"] != "message" or e["agent"] != agent:
            continue
        text = e["payload"]["text"]
        prompts.append(text.removeprefix("echo: ") if agent == "claude" else json.loads(text)["prompt"])
    return prompts


# --- Automatic handoff --------------------------------------------------------


def test_claude_limit_hands_off_to_codex_with_brief(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "claude:limit codex:ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "build the parser"})
    session = wait_until_idle(client, session_id)

    assert session["status"] == "completed"
    assert session["agent"] == "codex"
    assert [(s["agent"], s["reason"]) for s in session["stints"]] == [("claude", "new"), ("codex", "limit")]
    assert session["stints"][0]["ended_at"] is not None
    types = event_types(client, session_id)
    assert types[types.index("limit_reached"):].count("handoff") == 1
    handoff = next(e for e in events_of(client, session_id) if e["type"] == "handoff")
    assert handoff["payload"] == {
        "from_agent": "claude", "to_agent": "codex", "model": None, "reason": "limit",
        "from_stint_id": session["stints"][0]["id"],
    }
    assert handoff["stint_id"] == session["stints"][1]["id"]

    [brief] = prompts_sent(client, session_id, "codex")
    assert brief.startswith("# Handoff from Claude Code")
    assert "build the parser" in brief
    assert "Unfinished turn" in brief  # the limit cut Claude's turn off
    stint = client.get(f"/api/sessions/{session_id}/stints/{session['stints'][1]['id']}").json()
    assert stint["brief"] == brief


def test_codex_limit_hands_off_along_session_chain(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "codex:limit claude:ok", agent="codex", chain=CODEX_THEN_CLAUDE)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    session = wait_until_idle(client, session_id)
    assert (session["status"], session["agent"]) == ("completed", "claude")


def test_ask_policy_offers_handoff_and_waits(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "claude:limit codex:ok", limit_policy="ask")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    session = wait_until_idle(client, session_id)

    assert (session["status"], session["agent"]) == ("waiting_for_input", "claude")
    offer = events_of(client, session_id)[-1]
    assert (offer["type"], offer["payload"]) == ("handoff_offer", {"agent": "codex", "model": None})

    response = client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "codex"})
    assert response.status_code == 202
    session = wait_until_idle(client, session_id)
    assert (session["status"], session["agent"]) == ("completed", "codex")
    assert session["stints"][1]["reason"] == "manual"


def test_global_settings_apply_to_sessions_without_their_own(client: TestClient, tmp_path: Path) -> None:
    assert client.get("/api/settings").json() == {
        "default_chain": [{"agent": "claude", "model": None}, {"agent": "codex", "model": None},
                          {"agent": "ollama", "model": None}],
        "limit_policy": "auto",
        "progress_layout": "panel",
    }
    saved = client.put("/api/settings", json={"default_chain": CODEX_THEN_CLAUDE, "limit_policy": "ask"})
    assert saved.json()["limit_policy"] == "ask"
    assert client.put("/api/settings", json={"default_chain": []}).status_code == 422

    session_id = create(client, tmp_path, "codex:limit", agent="codex")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    assert wait_until_idle(client, session_id)["status"] == "waiting_for_input"
    assert events_of(client, session_id)[-1]["payload"]["agent"] == "claude"


def test_limit_reported_after_turn_hands_off_before_next_turn(client: TestClient, engine, tmp_path: Path) -> None:
    """Codex reports a reached limit only after a turn, so the next message goes straight to the next agent."""
    session_id = create(client, tmp_path, "ok", agent="codex", chain=CODEX_THEN_CLAUDE)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "first"})
    session = wait_until_idle(client, session_id)
    with Session(engine) as db:
        db.add(SessionEvent(session_id=session_id, stint_id=session["stints"][0]["id"], agent=AgentKind.CODEX,
                            type=EventType.RATE_LIMIT, payload={"status": "rejected", "resets_at": 4102444800}))
        db.commit()

    client.post(f"/api/sessions/{session_id}/messages", json={"text": "second"})
    session = wait_until_idle(client, session_id)
    assert (session["status"], session["agent"]) == ("completed", "claude")
    assert len(prompts_sent(client, session_id, "codex")) == 1  # "second" never reached Codex
    [brief] = prompts_sent(client, session_id, "claude")
    assert "cut off before finishing its reply to:\n\nsecond" in brief


def test_expired_limit_report_does_not_hand_off(client: TestClient, engine, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok", agent="codex", chain=CODEX_THEN_CLAUDE)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "first"})
    session = wait_until_idle(client, session_id)
    with Session(engine) as db:
        db.add(SessionEvent(session_id=session_id, stint_id=session["stints"][0]["id"], agent=AgentKind.CODEX,
                            type=EventType.RATE_LIMIT, payload={"status": "rejected", "resets_at": 1}))
        db.commit()
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "second"})
    assert wait_until_idle(client, session_id)["agent"] == "codex"


# --- Simulated limit (HUB_SIMULATE_LIMIT) and loop guard ----------------------


def simulate(client: TestClient, *agents: str) -> None:
    client.app.state.orchestrator.simulate_limit = {AgentKind(a) for a in agents}


def test_simulated_limit_runs_turn_then_hands_off(client: TestClient, tmp_path: Path) -> None:
    simulate(client, "claude")
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "build it"})
    session = wait_until_idle(client, session_id)

    assert (session["status"], session["agent"]) == ("completed", "codex")
    events = events_of(client, session_id)
    limit = next(e for e in events if e["type"] == "limit_reached")
    assert (limit["agent"], limit["payload"]["simulated"]) == ("claude", True)
    assert prompts_sent(client, session_id, "claude") == ["build it"]  # Claude's turn really ran
    assert "Last messages from Claude Code" in prompts_sent(client, session_id, "codex")[0]


def test_simulated_limit_with_ask_policy_offers_handoff(client: TestClient, tmp_path: Path) -> None:
    simulate(client, "claude")
    session_id = create(client, tmp_path, "question", limit_policy="ask")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    assert wait_until_idle(client, session_id)["status"] == "waiting_for_input"
    assert event_types(client, session_id)[-2:] == ["limit_reached", "handoff_offer"]


def test_agents_out_of_usage_are_not_tried_again(client: TestClient, tmp_path: Path) -> None:
    """With a chain that returns to Claude, two limited agents must not hand off to each other forever."""
    chain = [{"agent": "claude"}, {"agent": "codex"}, {"agent": "claude"}]
    simulate(client, "claude", "codex")
    session_id = create(client, tmp_path, "ok", chain=chain)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    session = wait_until_idle(client, session_id)

    assert [s["agent"] for s in session["stints"]] == ["claude", "codex"]
    assert session["status"] == "failed"
    assert "no other agent" in events_of(client, session_id)[-1]["payload"]["message"]


def test_agent_that_made_progress_can_take_over_again(client: TestClient, tmp_path: Path) -> None:
    """Once Codex finished a turn, Claude's earlier limit may have reset, so Claude is tried again."""
    chain = [{"agent": "claude"}, {"agent": "codex"}, {"agent": "claude"}]
    session_id = create(client, tmp_path, "claude:limit codex:ok", chain=chain)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    assert wait_until_idle(client, session_id)["agent"] == "codex"

    simulate(client, "codex")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "more"})
    session = wait_until_idle(client, session_id)
    # Claude takes over, hits its (fake) limit again, and then Codex is still out of usage.
    assert [s["agent"] for s in session["stints"]] == ["claude", "codex", "claude"]
    assert session["status"] == "failed"


# --- Manual handoff -----------------------------------------------------------


def test_manual_handoff_with_note_then_reply_resumes_new_agent(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "claude:ok codex:question")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "write the parser"})
    wait_until_idle(client, session_id)

    response = client.post(f"/api/sessions/{session_id}/handoff",
                           json={"agent": "codex", "model": "gpt-6-sol", "note": "now add tests"})
    assert response.status_code == 202
    session = wait_until_idle(client, session_id)
    assert session["status"] == "waiting_for_input"
    assert (session["agent"], session["model"], session["native_session_id"]) == (
        "codex", "gpt-6-sol", session["stints"][1]["native_session_id"])
    user_messages = [e["payload"]["text"] for e in events_of(client, session_id) if e["type"] == "user_message"]
    assert user_messages == ["write the parser", "now add tests"]
    [brief] = prompts_sent(client, session_id, "codex")
    assert "the user handed the session over to you" in brief
    assert brief.endswith("## What to do now\n\nnow add tests")
    assert "Unfinished turn" not in brief

    client.post(f"/api/sessions/{session_id}/messages", json={"text": "blue"})
    wait_until_idle(client, session_id)
    assert prompts_sent(client, session_id, "codex")[-1] == "blue"


def test_session_reports_current_agents_model(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    assert client.get(f"/api/sessions/{session_id}").json()["reported_model"] is None
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    assert wait_until_idle(client, session_id)["reported_model"] == "fake"  # from Claude's init event

    client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "codex"})
    session = wait_until_idle(client, session_id)
    # The fake Codex writes no session file, so its default model stays unnamed; Claude's no longer applies.
    assert (session["agent"], session["model"], session["reported_model"]) == ("codex", None, None)


def test_handoff_back_starts_a_fresh_session(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    for agent in ("claude", "codex", "claude"):
        if agent != "claude" or event_types(client, session_id):
            client.post(f"/api/sessions/{session_id}/handoff", json={"agent": agent})
        else:
            client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
        wait_until_idle(client, session_id)
    session = client.get(f"/api/sessions/{session_id}").json()
    stints = session["stints"]
    assert [s["agent"] for s in stints] == ["claude", "codex", "claude"]
    assert stints[0]["native_session_id"] != stints[2]["native_session_id"]


def test_handoff_rejected_while_running_or_unavailable(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "hang")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    assert client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "codex"}).status_code == 409
    client.post(f"/api/sessions/{session_id}/stop")

    response = client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "ollama"})
    assert response.status_code == 400
    assert len(client.get(f"/api/sessions/{session_id}").json()["stints"]) == 1
    assert client.post("/api/sessions/missing/handoff", json={"agent": "codex"}).status_code == 404
    assert client.get(f"/api/sessions/{session_id}/stints/999").status_code == 404


# --- Brief and chain ----------------------------------------------------------


def event(stint_id: int, type: str, **payload) -> SessionEvent:
    return SessionEvent(session_id="s", stint_id=stint_id, agent=AgentKind.CLAUDE, type=type, payload=payload)


def test_brief_lists_task_messages_files_and_commands() -> None:
    events = [
        event(1, "user_message", text="the task"),
        event(1, "message", text="old reply"),
        event(1, "tool_use", name="Edit", input={"file_path": "a.py"}),
        event(1, "tool_use", name="Bash", input={"command": "pytest"}),
        event(1, "completed"),
        event(1, "user_message", text="also do b"),
        event(2, "tool_use", name="Edit", input={"changes": [{"path": "b.py"}, {"path": "a.py"}]}),
        event(2, "tool_use", name="Shell", input={"command": "git status"}),
        event(2, "message", text="m1"),
        event(2, "message", text="m2"),
        event(2, "message", text="m3"),
        event(2, "message", text="m4"),
    ]
    git = GitState(status=" M a.py\n", diff_stat=" a.py | 2 +-\n", diff="diff --git a/a.py b/a.py\n")
    brief = build_brief(events, 2, AgentKind.CODEX, AgentKind.CLAUDE, StintReason.MANUAL, git)

    assert "## The task\n\nthe task" in brief
    assert "- also do b" in brief
    assert "m2" in brief and "m4" in brief and "m1" not in brief and "old reply" not in brief
    assert "- a.py\n- b.py" in brief
    assert "- `pytest`\n- `git status`" in brief
    assert " M a.py" in brief and "diff --git a/a.py" in brief
    assert brief.endswith("Continue the task from where the previous agent stopped.")


def test_brief_cuts_a_huge_diff_to_fit() -> None:
    git = GitState(status="", diff_stat="", diff="+" * 200_000)
    brief = build_brief([event(1, "user_message", text="t")], 1, AgentKind.CLAUDE, AgentKind.CODEX,
                        StintReason.LIMIT, git, note="go on")
    assert len(brief) <= MAX_BRIEF
    assert "diff cut off; 200,000 characters" in brief
    assert brief.endswith("go on")


def test_git_state_reads_repo_and_handles_plain_folder(tmp_path: Path) -> None:
    assert asyncio.run(git_state(tmp_path)) is None

    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, check=True,
                       capture_output=True)

    git("init")
    (tmp_path / "a.txt").write_text("one\n")
    assert asyncio.run(git_state(tmp_path)).status.strip() == "?? a.txt"  # no commits yet
    git("add", ".")
    git("commit", "-m", "first")
    (tmp_path / "a.txt").write_text("two\n")
    state = asyncio.run(git_state(tmp_path))
    assert state.status.strip() == "M a.txt"
    assert "-one\n+two" in state.diff
    assert "a.txt" in state.diff_stat


@pytest.mark.parametrize(
    ("current", "expected"),
    [(AgentKind.CLAUDE, ["codex", "ollama"]), (AgentKind.CODEX, ["ollama"]), (AgentKind.OLLAMA, [])],
)
def test_next_in_chain(current: AgentKind, expected: list[str]) -> None:
    chain = [ChainEntry(agent=a) for a in ("claude", "codex", "ollama")]
    assert [e.agent for e in next_in_chain(chain, current)] == expected


def test_next_in_chain_for_agent_outside_chain() -> None:
    chain = [ChainEntry(agent=AgentKind.CLAUDE), ChainEntry(agent=AgentKind.OLLAMA)]
    assert [e.agent for e in next_in_chain(chain, AgentKind.CODEX)] == ["claude", "ollama"]
