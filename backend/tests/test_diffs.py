import subprocess
from pathlib import Path

from app.adapters.diffs import MAX_DIFF_CHARS, FileSnapshots, file_diff, from_structured_patch, hunks


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "app.py").write_text("one\ntwo\nthree\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_hunks_have_no_file_header() -> None:
    assert hunks("a\nb\n", "a\nc\n") == "@@ -1,2 +1,2 @@\n a\n-b\n+c"
    assert hunks("", "x\n") == "@@ -0,0 +1 @@\n+x"


def test_structured_patch() -> None:
    patch = [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 2, "lines": ["-a", "+a", "+b"]}]
    assert from_structured_patch(patch) == "@@ -1,1 +1,2 @@\n-a\n+a\n+b"


def test_long_diff_is_truncated() -> None:
    entry = file_diff("x", "update", "+" * (MAX_DIFF_CHARS + 10))
    assert entry["truncated"] is True
    assert len(entry["diff"]) == MAX_DIFF_CHARS


def test_first_edit_is_against_the_commit_then_against_the_previous_edit(tmp_path: Path) -> None:
    root = repo(tmp_path)
    snapshots = FileSnapshots(root)
    path = str(root / "app.py")

    (root / "app.py").write_text("one\nTWO\nthree\n")
    [first] = snapshots.capture([{"path": path, "kind": "update"}])
    assert first["base"] == "commit"
    assert first["diff"] == "@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three"

    (root / "app.py").write_text("one\nTWO\nthree\nfour\n")
    [second] = snapshots.capture([{"path": path, "kind": "update"}])
    assert second["base"] == "previous_edit"
    assert second["diff"] == "@@ -1,3 +1,4 @@\n one\n TWO\n three\n+four"


def test_subfolder_session_and_deleted_file(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "sub").mkdir()
    (root / "app.py").unlink()
    [deleted] = FileSnapshots(root / "sub").capture([{"path": str(root / "app.py"), "kind": "delete"}])
    assert deleted["diff"] == "@@ -1,3 +0,0 @@\n-one\n-two\n-three"


def test_new_file_and_relative_path(tmp_path: Path) -> None:
    (tmp_path / "new.txt").write_text("hi\n")
    [added] = FileSnapshots(tmp_path).capture([{"path": "new.txt", "kind": "add"}])
    assert added["diff"] == "@@ -0,0 +1 @@\n+hi"


def test_unknown_before_or_binary_has_no_diff(tmp_path: Path) -> None:
    # Outside a repository, the first update of a file has nothing to compare with.
    (tmp_path / "notes.txt").write_text("changed\n")
    (tmp_path / "image.bin").write_bytes(b"\x00\x01")
    snapshots = FileSnapshots(tmp_path)
    [update] = snapshots.capture([{"path": str(tmp_path / "notes.txt"), "kind": "update"}])
    [binary] = snapshots.capture([{"path": str(tmp_path / "image.bin"), "kind": "add"}])
    assert update["diff"] is None
    assert binary["diff"] is None
    # The next edit is against the copy the hub saw.
    (tmp_path / "notes.txt").write_text("changed again\n")
    [next_update] = snapshots.capture([{"path": str(tmp_path / "notes.txt"), "kind": "update"}])
    assert next_update["diff"] == "@@ -1 +1 @@\n-changed\n+changed again"
