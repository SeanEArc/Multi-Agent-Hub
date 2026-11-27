from pathlib import Path

from fastapi.testclient import TestClient


def test_top_level_lists_home(client: TestClient) -> None:
    body = client.get("/api/folders").json()
    assert body["path"] is None
    assert body["parent"] is None
    assert str(Path.home()) in [f["path"] for f in body["folders"]]


def test_lists_subfolders_only_sorted(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "beta").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / "notes.txt").write_text("not a folder")

    body = client.get("/api/folders", params={"path": str(tmp_path)}).json()
    assert body["path"] == str(tmp_path.resolve())
    assert body["parent"] == str(tmp_path.resolve().parent)
    assert [f["name"] for f in body["folders"]] == ["Alpha", "beta"]
    assert body["folders"][0]["path"] == str(tmp_path.resolve() / "Alpha")


def test_show_hidden(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    body = client.get("/api/folders", params={"path": str(tmp_path), "show_hidden": True}).json()
    assert [f["name"] for f in body["folders"]] == [".git"]


def test_drive_root_has_no_parent(client: TestClient) -> None:
    root = Path.home().anchor
    body = client.get("/api/folders", params={"path": root}).json()
    assert body["parent"] is None


def test_missing_folder_is_404(client: TestClient, tmp_path: Path) -> None:
    assert client.get("/api/folders", params={"path": str(tmp_path / "nope")}).status_code == 404
    (tmp_path / "file.txt").write_text("x")
    assert client.get("/api/folders", params={"path": str(tmp_path / "file.txt")}).status_code == 404
