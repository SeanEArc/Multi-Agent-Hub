import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.handoff import git_state

from test_orchestrator import create, event_types, wait_until_idle


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit and a subfolder."""
    path = tmp_path / "project"
    (path / "src").mkdir(parents=True)
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("hello\n")
    (path / "src" / "app.py").write_text("print('hi')\n")
    git(path, "add", "--all")
    git(path, "commit", "--quiet", "--message", "first")
    return path


def worktree_of(client: TestClient, session_id: str) -> dict | None:
    return client.get(f"/api/sessions/{session_id}").json()["worktree"]


def agent_cwd(client: TestClient, session_id: str) -> Path:
    """The folder the fake Claude ran in, from its echoed result."""
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "where are you"})
    wait_until_idle(client, session_id)
    result = client.get(f"/api/sessions/{session_id}/events").json()[-1]["payload"]["text"]
    return Path(json.loads(result)["cwd"]).resolve()


def branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)").splitlines()


def test_session_in_a_repo_runs_in_its_own_worktree(client: TestClient, repo: Path) -> None:
    session_id = create(client, repo, "ok")
    worktree = worktree_of(client, session_id)

    assert worktree["status"] == "active"
    assert worktree["branch"].startswith("hub/ok-")
    assert worktree["base_commit"] == git(repo, "rev-parse", "HEAD")
    assert Path(worktree["repo_path"]) == repo.resolve()
    assert agent_cwd(client, session_id) == Path(worktree["path"]).resolve()
    assert worktree["branch"] in branches(repo)
    assert git(repo, "branch", "--show-current") == "main"  # the user's checkout is untouched


def test_two_sessions_on_one_repo_get_separate_worktrees(client: TestClient, repo: Path) -> None:
    first = worktree_of(client, create(client, repo, "ok"))
    second = worktree_of(client, create(client, repo, "ok"))

    assert first["path"] != second["path"]
    assert first["branch"] != second["branch"]
    (Path(first["path"]) / "README.md").write_text("changed by the first session\n")
    assert (Path(second["path"]) / "README.md").read_text() == "hello\n"
    assert (repo / "README.md").read_text() == "hello\n"


def test_project_subfolder_maps_into_the_worktree(client: TestClient, repo: Path) -> None:
    session_id = create(client, repo / "src", "ok")
    worktree = worktree_of(client, session_id)

    assert worktree["subdir"] == "src"
    assert agent_cwd(client, session_id) == (Path(worktree["path"]) / "src").resolve()


def test_opting_out_or_a_plain_folder_runs_in_place(client: TestClient, repo: Path, tmp_path: Path) -> None:
    opted_out = create(client, repo, "ok", use_worktree=False)
    plain = tmp_path / "plain"
    plain.mkdir()
    not_a_repo = create(client, plain, "ok")

    assert worktree_of(client, opted_out) is None
    assert agent_cwd(client, opted_out) == repo.resolve()
    assert worktree_of(client, not_a_repo) is None
    assert agent_cwd(client, not_a_repo) == plain.resolve()


def test_repo_without_commits_is_refused(client: TestClient, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "--quiet")

    response = client.post("/api/sessions", json={"title": "ok", "project_path": str(empty)})

    assert response.status_code == 400
    assert "no commits" in response.json()["detail"]


def test_removing_a_clean_unused_worktree_also_drops_its_empty_branch(client: TestClient, repo: Path) -> None:
    session_id = create(client, repo, "ok")
    worktree = worktree_of(client, session_id)

    response = client.post(f"/api/sessions/{session_id}/worktree/remove", json={})

    assert response.status_code == 200
    assert response.json()["worktree"]["status"] == "removed"
    assert not Path(worktree["path"]).exists()
    assert worktree["branch"] not in branches(repo)


def test_uncommitted_changes_block_removal_until_committed(client: TestClient, repo: Path) -> None:
    session_id = create(client, repo, "ok")
    worktree = worktree_of(client, session_id)
    (Path(worktree["path"]) / "new_file.py").write_text("x = 1\n")

    refused = client.post(f"/api/sessions/{session_id}/worktree/remove", json={})
    assert refused.status_code == 409
    assert refused.json()["detail"]["changes"] == ["?? new_file.py"]
    assert Path(worktree["path"]).exists()

    committed = client.post(f"/api/sessions/{session_id}/worktree/remove", json={"commit": True})
    assert committed.status_code == 200
    assert not Path(worktree["path"]).exists()
    assert git(repo, "show", "--name-only", "--format=%s", worktree["branch"]).splitlines() == ["ok", "", "new_file.py"]


def test_discard_throws_changes_away(client: TestClient, repo: Path) -> None:
    session_id = create(client, repo, "ok")
    worktree = worktree_of(client, session_id)
    (Path(worktree["path"]) / "README.md").write_text("edited\n")

    response = client.post(f"/api/sessions/{session_id}/worktree/remove", json={"discard": True})

    assert response.status_code == 200
    assert not Path(worktree["path"]).exists()
    assert worktree["branch"] not in branches(repo)


def test_worktree_folder_deleted_by_hand_is_pruned(client: TestClient, repo: Path) -> None:
    session_id = create(client, repo, "ok")
    worktree = worktree_of(client, session_id)
    git(repo, "worktree", "remove", "--force", worktree["path"])

    response = client.post(f"/api/sessions/{session_id}/worktree/remove", json={})

    assert response.status_code == 200
    assert worktree["path"] not in git(repo, "worktree", "list")


def test_message_after_removal_explains_where_the_work_is(client: TestClient, repo: Path) -> None:
    session_id = create(client, repo, "ok")
    worktree = worktree_of(client, session_id)
    (Path(worktree["path"]) / "new_file.py").write_text("x = 1\n")
    client.post(f"/api/sessions/{session_id}/worktree/remove", json={"commit": True})

    response = client.post(f"/api/sessions/{session_id}/messages", json={"text": "carry on"})

    assert response.status_code == 400
    assert worktree["branch"] in response.json()["detail"]


def test_deleting_a_session_removes_its_clean_worktree(client: TestClient, repo: Path) -> None:
    session_id = create(client, repo, "ok")
    worktree = worktree_of(client, session_id)

    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert not Path(worktree["path"]).exists()


def test_deleting_a_session_with_uncommitted_work_is_refused(client: TestClient, repo: Path) -> None:
    session_id = create(client, repo, "ok")
    worktree = worktree_of(client, session_id)
    (Path(worktree["path"]) / "new_file.py").write_text("x = 1\n")

    assert client.delete(f"/api/sessions/{session_id}").status_code == 409
    assert client.get(f"/api/sessions/{session_id}").status_code == 200
    assert Path(worktree["path"]).exists()


def test_handoff_brief_includes_work_committed_on_the_branch(client: TestClient, repo: Path) -> None:
    session_id = create(client, repo, "ok")
    worktree = worktree_of(client, session_id)
    client.post(f"/api/sessions/{session_id}/messages", json={"text": "start"})
    wait_until_idle(client, session_id)
    path = Path(worktree["path"])
    (path / "committed.py").write_text("done = True\n")
    git(path, "add", "--all")
    git(path, "commit", "--quiet", "--message", "agent commit")
    (path / "README.md").write_text("uncommitted edit\n")

    client.post(f"/api/sessions/{session_id}/handoff", json={"agent": "codex"})
    body = wait_until_idle(client, session_id)

    stint_id = body["stints"][-1]["id"]
    brief = client.get(f"/api/sessions/{session_id}/stints/{stint_id}").json()["brief"]
    assert "committed.py" in brief
    assert "README.md" in brief
    assert "handoff" in event_types(client, session_id)


def test_git_state_diffs_against_the_base_commit(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD")
    (repo / "later.py").write_text("x = 2\n")
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--message", "later")

    assert "later.py" not in asyncio.run(git_state(repo)).diff_stat
    assert "later.py" in asyncio.run(git_state(repo, base=base)).diff_stat


def test_folder_git_info(client: TestClient, repo: Path, tmp_path: Path) -> None:
    (repo / "README.md").write_text("dirty\n")
    info = client.get("/api/folders/git", params={"path": str(repo / "src")}).json()
    assert info["is_repo"] is True
    assert Path(info["root"]) == repo.resolve()
    assert info["branch"] == "main"
    assert info["has_commits"] is True
    assert info["changes"] == [" M README.md"]

    plain = tmp_path / "plain"
    plain.mkdir()
    assert client.get("/api/folders/git", params={"path": str(plain)}).json()["is_repo"] is False
