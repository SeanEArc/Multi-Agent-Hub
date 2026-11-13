"""The skill library, its API, the `/` menu and how each agent gets library skills."""

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.claude import ClaudeAdapter
from app.adapters.codex import CodexAdapter
from app.adapters.local_tools import LocalTools, ToolError
from app.models import AgentKind
from app.skills import SkillError, SkillLibrary, expand_command, menu, read_skill

from conftest import FAKE_CLAUDE, FAKE_CODEX
from test_handoff import events_of, prompts_sent
from test_orchestrator import create, wait_until_idle

ALL_ON = {"claude": True, "codex": True, "ollama": True}


def make_skill(folder: Path, name: str = "greet", body: str = "Say hello warmly.", extra: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Greets the user. Use when asked to greet.\n{extra}---\n\n{body}\n",
        encoding="utf-8",
    )
    return folder


def library(tmp_path: Path) -> SkillLibrary:
    return SkillLibrary(tmp_path / "lib", claude_home=tmp_path / "claude-home", codex_home=tmp_path / "codex-home")


# --- Reading skills ---


def test_read_skill_suggests_switches_from_what_it_needs(tmp_path: Path) -> None:
    plain = read_skill(make_skill(tmp_path / "plain"))
    assert (plain.name, plain.description, plain.warnings) == ("greet", "Greets the user. Use when asked to greet.", [])
    assert plain.suggested == ALL_ON

    claude_only = read_skill(make_skill(tmp_path / "ask", body="Ask with the AskUserQuestion tool.",
                                        extra="allowed-tools: Read\n"))
    assert claude_only.suggested == {"claude": True, "codex": False, "ollama": False}
    assert any("AskUserQuestion" in w for w in claude_only.warnings)
    assert any("allowed-tools" in w for w in claude_only.warnings)

    scripted = make_skill(tmp_path / "scripted")
    (scripted / "scripts").mkdir()
    (scripted / "scripts" / "run.py").write_text("print(1)")
    info = read_skill(scripted)
    assert info.scripts == ["scripts/run.py"]
    assert info.suggested == {"claude": True, "codex": True, "ollama": False}


def test_a_skill_needs_a_description(tmp_path: Path) -> None:
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "SKILL.md").write_text("---\nname: bad\n---\nbody")
    with pytest.raises(SkillError, match="no description"):
        read_skill(folder)


# --- The library ---


def test_linked_skill_is_read_in_place_and_left_alone_on_removal(tmp_path: Path) -> None:
    lib = library(tmp_path)
    folder = make_skill(tmp_path / "mine" / "greet")
    lib.link(folder, ALL_ON)
    (folder / "SKILL.md").write_text((folder / "SKILL.md").read_text().replace("warmly", "loudly"))
    assert "loudly" in read_skill(lib.get("greet").path).content  # edits carry over

    view = lib.root / "views" / "claude" / "skills" / "greet"
    assert (view / "SKILL.md").is_file() and lib.claude_plugin_dir() is not None
    lib.remove("greet")
    assert not view.exists() and (folder / "SKILL.md").is_file()  # the user's own folder stays
    assert lib.claude_plugin_dir() is None


def test_copied_skill_is_deleted_on_removal_and_names_are_unique(tmp_path: Path) -> None:
    lib = library(tmp_path)
    skill = lib.copy_in(make_skill(tmp_path / "download" / "greet"), {"codex": True})
    assert skill.path.is_relative_to(lib.root) and skill.agents == {"claude": False, "codex": True, "ollama": False}
    with pytest.raises(SkillError, match="already"):
        lib.link(make_skill(tmp_path / "other" / "greet"), ALL_ON)
    lib.remove("greet")
    assert not skill.path.exists()


def test_a_missing_linked_folder_is_kept_but_not_given_to_agents(tmp_path: Path) -> None:
    lib = library(tmp_path)
    folder = make_skill(tmp_path / "mine" / "greet")
    lib.link(folder, ALL_ON)
    (folder / "SKILL.md").unlink()
    assert lib.get("greet").present is False
    assert lib.for_agent(AgentKind.CODEX) == []


def test_an_agent_is_not_given_its_own_skill_twice(tmp_path: Path) -> None:
    lib = library(tmp_path)
    lib.link(make_skill(tmp_path / "claude-home" / "skills" / "greet"), ALL_ON)
    assert lib.get("greet").origin == "claude"
    assert lib.for_agent(AgentKind.CLAUDE) == [] and lib.claude_plugin_dir() is None
    assert [s.name for s in lib.for_agent(AgentKind.CODEX)] == ["greet"]


def test_switches_decide_who_gets_a_skill(tmp_path: Path) -> None:
    lib = library(tmp_path)
    lib.link(make_skill(tmp_path / "mine" / "greet"), {"claude": True})
    assert lib.instructions(AgentKind.CODEX) is None
    lib.set_agents("greet", {"codex": True})
    assert lib.get("greet").agents == {"claude": True, "codex": True, "ollama": False}
    text = lib.instructions(AgentKind.CODEX)
    assert "- greet: Greets the user." in text and str(tmp_path / "mine" / "greet" / "SKILL.md") in text


# --- Using a skill from the `/` menu ---


def test_expand_command_per_agent(tmp_path: Path) -> None:
    lib = library(tmp_path)
    lib.link(make_skill(tmp_path / "mine" / "greet"), ALL_ON)
    assert expand_command(AgentKind.CLAUDE, "/greet Sam", lib) == "/hub:greet Sam"
    for agent in (AgentKind.CODEX, AgentKind.OLLAMA):
        prompt = expand_command(agent, "/greet Sam", lib)
        assert '<skill name="greet">\nSay hello warmly.\n</skill>' in prompt
        assert prompt.endswith("## The request\n\nSam") and "name: greet" not in prompt
    assert expand_command(AgentKind.CODEX, "/imagegen a cat", lib, codex_names=["imagegen"]) == "$imagegen a cat"
    assert expand_command(AgentKind.CODEX, "/unknown thing", lib) == "/unknown thing"
    assert expand_command(AgentKind.CODEX, "fix /greet in the docs", lib) == "fix /greet in the docs"


def test_menu_lists_only_what_the_agent_can_use(tmp_path: Path) -> None:
    lib = library(tmp_path)
    lib.link(make_skill(tmp_path / "mine" / "greet"), {"claude": True, "codex": True})
    claude = menu(AgentKind.CLAUDE, lib, [{"name": "review", "description": "Reviews"}, {"name": "hub:greet"},
                                          {"name": "compact"}])
    assert [(c.name, c.kind) for c in claude] == [("compact", "hub"), ("review", "skill"),
                                                  ("greet", "library"), ("context", "builtin"), ("usage", "builtin")]
    local = menu(AgentKind.OLLAMA, lib, None, hub=False)
    assert local == []  # the skill is off for the local model, and it has no skills of its own


# --- Delivery to each agent ---


def test_claude_gets_the_plugin_dir_and_codex_the_instructions(tmp_path: Path) -> None:
    import asyncio

    plugin = tmp_path / "plugin"

    async def dumped(adapter) -> str:
        return " ".join([json.dumps(event.data) async for event in adapter.start_task("hi")])

    claude = ClaudeAdapter(tmp_path, command=["python", str(FAKE_CLAUDE), "ok"], plugin_dirs=[plugin])
    sent = asyncio.run(dumped(claude))
    assert "--plugin-dir" in sent and "plugin" in sent

    codex = CodexAdapter(tmp_path, command=["python", str(FAKE_CODEX), "ok"], codex_home=tmp_path,
                         skill_instructions="## Skills from Multi-Agent Hub\n- greet")
    assert "Skills from Multi-Agent Hub" in asyncio.run(dumped(codex))


def test_local_model_may_read_but_not_change_skill_folders(tmp_path: Path) -> None:
    skill = make_skill(tmp_path / "skills" / "greet")
    work = tmp_path / "work"
    work.mkdir()
    tools = LocalTools(work, read_roots=[skill])
    assert tools.resolve(str(skill / "SKILL.md")) == (skill / "SKILL.md").resolve()
    with pytest.raises(ToolError):
        tools.resolve(str(skill / "SKILL.md"), writing=True)
    with pytest.raises(ToolError):
        tools.resolve(str(tmp_path / "elsewhere.txt"))


# --- API ---


def test_import_a_folder_then_switch_and_remove(client: TestClient, tmp_path: Path) -> None:
    source = tmp_path / "repo-like"
    make_skill(source / "skills" / "greet")
    make_skill(source / "skills" / "wave", name="wave")
    preview = client.post("/api/skills/preview", json={"source": str(source)}).json()
    assert preview["kind"] == "folder" and preview["staging_id"] is None
    assert sorted(s["name"] for s in preview["skills"]) == ["greet", "wave"]
    assert "Say hello warmly." in preview["skills"][0]["content"]

    pick = next(s for s in preview["skills"] if s["name"] == "greet")
    response = client.post("/api/skills", json={"source": str(source), "picks": [
        {"path": pick["path"], "agents": {"claude": False, "codex": True, "ollama": True}}]})
    assert response.status_code == 201, response.text
    [entry] = client.get("/api/skills").json()["skills"]
    assert (entry["name"], entry["source"], entry["present"]) == ("greet", "link", True)

    client.patch("/api/skills/greet", json={"agents": {"ollama": False}})
    assert client.get("/api/skills").json()["skills"][0]["agents"] == {"claude": False, "codex": True,
                                                                       "ollama": False}
    assert client.delete("/api/skills/greet").status_code == 204
    assert client.get("/api/skills").json()["skills"] == []
    assert (source / "skills" / "greet" / "SKILL.md").is_file()


def test_import_a_zip_copies_it_in(client: TestClient, tmp_path: Path) -> None:
    make_skill(tmp_path / "pack" / "greet")
    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.write(tmp_path / "pack" / "greet" / "SKILL.md", "greet/SKILL.md")
    preview = client.post("/api/skills/preview", json={"source": str(archive)}).json()
    assert preview["kind"] == "zip" and preview["staging_id"]
    response = client.post("/api/skills", json={"source": str(archive), "staging_id": preview["staging_id"],
                                                "picks": [{"path": "greet", "agents": ALL_ON}]})
    assert response.status_code == 201, response.text
    entry = client.get("/api/skills").json()["skills"][0]
    assert entry["source"] == "copy"
    assert Path(entry["path"]).is_relative_to(client.app.state.skills.root.resolve())


def test_a_zip_with_paths_outside_its_folder_is_refused(client: TestClient, tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../escape/SKILL.md", "---\nname: x\ndescription: y\n---\n")
    response = client.post("/api/skills/preview", json={"source": str(archive)})
    assert response.status_code == 400 and "outside" in response.json()["detail"]


def test_preview_explains_a_bad_source(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/api/skills/preview", json={"source": str(tmp_path / "nothing-here")})
    assert response.status_code == 400
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "No skills were found" in client.post("/api/skills/preview", json={"source": str(empty)}).json()["detail"]


def test_repository_skill_update_shows_changes_first(client: TestClient, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    import app.api.skills as routes

    upstream = tmp_path / "upstream"
    make_skill(upstream / "skills" / "greet")
    commits = iter(["c1", "c2"])

    async def fake_clone(url: str, into: Path) -> str:
        shutil.copytree(upstream, into)
        return next(commits)

    monkeypatch.setattr(routes, "clone", fake_clone)
    monkeypatch.setattr(routes, "is_git_url", lambda source: source.startswith("https://"))
    url = "https://example.com/skills.git"
    preview = client.post("/api/skills/preview", json={"source": url}).json()
    client.post("/api/skills", json={"source": url, "staging_id": preview["staging_id"],
                                     "picks": [{"path": "skills/greet", "agents": ALL_ON}]})
    entry = client.get("/api/skills").json()["skills"][0]
    assert (entry["repo"], entry["commit"], entry["subpath"]) == (url, "c1", "skills/greet")

    (upstream / "skills" / "greet" / "SKILL.md").write_text(
        (upstream / "skills" / "greet" / "SKILL.md").read_text().replace("warmly", "briefly"))
    check = client.post("/api/skills/greet/update").json()
    assert check["up_to_date"] is False and "+Say hello briefly." in check["skill_diff"]
    assert "warmly" in read_skill(Path(entry["path"])).content  # nothing changed yet
    applied = client.post("/api/skills/greet/update/apply", json={"staging_id": check["staging_id"]}).json()
    assert applied["commit"] == "c2" and "briefly" in read_skill(Path(entry["path"])).content


# --- The orchestrator ---


def test_slash_skill_reaches_codex_spelled_out(client: TestClient, tmp_path: Path) -> None:
    client.app.state.skills.link(make_skill(tmp_path / "mine" / "greet"), ALL_ON)
    session_id = create(client, tmp_path, "ok", agent="codex")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/greet Sam"})
    wait_until_idle(client, session_id)
    [prompt] = prompts_sent(client, session_id, "codex")
    assert '<skill name="greet">' in prompt and prompt.endswith("Sam")
    user = next(e for e in events_of(client, session_id) if e["type"] == "user_message")
    assert user["payload"]["text"] == "/greet Sam"  # the transcript shows what was typed


def test_claude_turns_teach_the_menu_its_skills(client: TestClient, tmp_path: Path) -> None:
    client.app.state.skills.link(make_skill(tmp_path / "mine" / "greet"), ALL_ON)
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/greet"})
    wait_until_idle(client, session_id)
    assert prompts_sent(client, session_id, "claude") == ["/hub:greet"]
    started = next(e for e in events_of(client, session_id) if e["type"] == "started")
    assert "capabilities" not in started["payload"]

    body = client.get("/api/skills/commands", params={"agent": "claude", "session_id": session_id}).json()
    assert [(c["name"], c["kind"]) for c in body["commands"]][1:3] == [("fake-skill", "skill"), ("greet", "library")]
    assert body["mcp_servers"] == [{"name": "fake-mcp", "status": "connected"}]


def test_compact_starts_the_same_agent_from_the_brief(client: TestClient, tmp_path: Path) -> None:
    session_id = create(client, tmp_path, "ok")
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/compact"})
    [notice] = [e for e in events_of(client, session_id) if e["type"] == "notice"]
    assert "Nothing to compact" in notice["payload"]["message"]  # nothing started yet

    client.post(f"/api/sessions/{session_id}/messages", json={"text": "build the thing"})
    wait_until_idle(client, session_id)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "/compact the parser"})
    session = client.get(f"/api/sessions/{session_id}").json()
    assert session["status"] == "ready" and len(session["stints"]) == 2
    assert session["agent"] == "claude"
    handoff = [e for e in events_of(client, session_id) if e["type"] == "handoff"][-1]
    assert handoff["payload"]["command"] == "compact" and handoff["payload"]["brief"] is True
    brief = client.get(f"/api/sessions/{session_id}/stints/{session['stints'][-1]['id']}").json()["brief"]
    assert brief.startswith("# Compacted conversation") and "## Keep in focus\n\nthe parser" in brief
    assert "build the thing" in brief

    client.post(f"/api/sessions/{session_id}/messages", json={"text": "next step"})
    wait_until_idle(client, session_id)
    first = prompts_sent(client, session_id, "claude")[-1]
    assert first.startswith("# Compacted conversation") and first.endswith("next step")

    assert not any(e["type"] == "user_message" and e["payload"]["text"].startswith("/")
                   for e in events_of(client, session_id))  # commands aren't saved as messages
    body = client.get("/api/skills/commands", params={"agent": "claude"}).json()
    assert "compact" not in [c["name"] for c in body["commands"]]  # a new chat has nothing to compact
