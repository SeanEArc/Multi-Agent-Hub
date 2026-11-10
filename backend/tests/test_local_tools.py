"""The local model's tools, run directly (the loop that calls them is in test_ollama_adapter.py)."""

import asyncio
from pathlib import Path

import pytest

from app.adapters.local_tools import TOOL_NAMES, LocalTools, ToolError, cap, plan_from_args


def run(tools: LocalTools, name: str, **args):
    return asyncio.run(tools.run(name, args))


@pytest.fixture
def tools(tmp_path: Path) -> LocalTools:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    print('hi')\n\nmain()\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\nSay hi.\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("hi", encoding="utf-8")
    return LocalTools(tmp_path, command_timeout=30)


def test_the_definitions_cover_every_tool() -> None:
    assert TOOL_NAMES == ["read_file", "list_files", "search", "write_file", "edit_file", "run_command",
                          "update_plan", "ask_user"]


# --- Paths ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["../outside.txt", "src/../../outside.txt", "C:\\Windows\\win.ini", ""])
def test_paths_outside_the_folder_are_refused(tools: LocalTools, path: str) -> None:
    result = run(tools, "read_file", path=path)
    assert result.is_error
    assert "outside the working folder" in result.content or "path is required" in result.content


def test_absolute_paths_inside_the_folder_work(tools: LocalTools, tmp_path: Path) -> None:
    assert not run(tools, "read_file", path=str(tmp_path / "README.md")).is_error


def test_git_folder_is_read_only(tools: LocalTools) -> None:
    result = run(tools, "write_file", path=".git/config", content="x")
    assert result.is_error and ".git" in result.content


# --- Reading --------------------------------------------------------------------------------------


def test_read_file_numbers_lines_and_pages(tools: LocalTools, tmp_path: Path) -> None:
    assert run(tools, "read_file", path="README.md").content == "1: # Demo\n2: Say hi."
    (tmp_path / "long.txt").write_text("\n".join(f"line {n}" for n in range(1, 1001)), encoding="utf-8")
    page = run(tools, "read_file", path="long.txt", start_line=500).content
    assert page.startswith("500: line 500")
    assert "(lines 500-899 of 1000; use start_line to read more)" in page
    assert run(tools, "read_file", path="missing.txt").content == "Error: missing.txt doesn't exist."
    assert "is a folder" in run(tools, "read_file", path="src").content


def test_binary_files_are_refused(tools: LocalTools, tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    assert "binary" in run(tools, "read_file", path="blob.bin").content


def test_list_files(tools: LocalTools) -> None:
    assert run(tools, "list_files").content == "node_modules/\nsrc/\nREADME.md"
    # Recursive listing skips folders like node_modules.
    assert run(tools, "list_files", recursive=True).content == "README.md\nsrc/app.py"


def test_search(tools: LocalTools) -> None:
    assert run(tools, "search", text="HI").content == "README.md:2: Say hi.\nsrc/app.py:2: print('hi')"
    assert run(tools, "search", text="hi", file_pattern="*.py").content == "src/app.py:2: print('hi')"
    assert run(tools, "search", text=r"def \w+", regex=True).content == "src/app.py:1: def main():"
    assert run(tools, "search", text="nothing like this").content == "No matches."
    assert "valid regular expression" in run(tools, "search", text="(", regex=True).content


# --- Writing --------------------------------------------------------------------------------------


def test_write_file_creates_and_reports_a_diff(tools: LocalTools, tmp_path: Path) -> None:
    result = run(tools, "write_file", path="new/notes.txt", content="one\ntwo\n")
    assert result.content == "Created new/notes.txt (2 lines)."
    assert (tmp_path / "new" / "notes.txt").read_bytes() == b"one\ntwo\n"  # no \r\n added
    [diff] = result.extra["diffs"]
    assert (diff["path"], diff["kind"]) == (str(tmp_path / "new" / "notes.txt"), "add")
    assert diff["diff"].splitlines()[1:] == ["+one", "+two"]

    replaced = run(tools, "write_file", path="new/notes.txt", content="one\nthree\n")
    assert replaced.content.startswith("Replaced")
    assert replaced.extra["diffs"][0]["kind"] == "update"
    assert "-two" in replaced.extra["diffs"][0]["diff"] and "+three" in replaced.extra["diffs"][0]["diff"]


def test_edit_file(tools: LocalTools, tmp_path: Path) -> None:
    result = run(tools, "edit_file", path="src/app.py", old_text="print('hi')", new_text="print('hello')")
    assert result.content == "Edited src/app.py."
    assert "print('hello')" in (tmp_path / "src" / "app.py").read_text(encoding="utf-8")
    assert result.extra["diffs"][0]["diff"].count("\n-") == 1

    assert "wasn't found" in run(tools, "edit_file", path="src/app.py", old_text="nope", new_text="x").content
    (tmp_path / "twice.txt").write_text("a\na\n", encoding="utf-8")
    assert "appears 2 times" in run(tools, "edit_file", path="twice.txt", old_text="a", new_text="b").content
    assert "write_file" in run(tools, "edit_file", path="none.txt", old_text="a", new_text="b").content


def test_edit_file_matches_windows_line_endings(tools: LocalTools, tmp_path: Path) -> None:
    (tmp_path / "crlf.txt").write_bytes(b"first\r\nsecond\r\n")
    result = run(tools, "edit_file", path="crlf.txt", old_text="first\nsecond", new_text="1\n2")
    assert not result.is_error
    assert (tmp_path / "crlf.txt").read_bytes() == b"1\r\n2\r\n"


# --- Commands -------------------------------------------------------------------------------------


def test_run_command_returns_output_and_exit_code(tools: LocalTools) -> None:
    ok = run(tools, "run_command", command="Write-Output 'héllo'; Get-Location | Split-Path -Leaf")
    assert not ok.is_error
    assert ok.extra == {"exit_code": 0}
    assert ok.content.splitlines()[0] == "héllo"

    # A program's own exit code comes through, not just PowerShell's 0 or 1.
    failed = run(tools, "run_command", command="cmd /c exit 7")
    assert failed.is_error and failed.extra == {"exit_code": 7}
    assert failed.content.startswith("Exit code 7")

    error = run(tools, "run_command", command="Get-Item does-not-exist")
    assert error.is_error and error.extra["exit_code"] == 1
    assert "does-not-exist" in error.content


def test_run_command_times_out(tmp_path: Path) -> None:
    tools = LocalTools(tmp_path, command_timeout=1)
    result = run(tools, "run_command", command="Start-Sleep -Seconds 30")
    assert result.is_error and result.extra == {"exit_code": None}
    assert "stopped after 1 seconds" in result.content


def test_kill_stops_a_running_command(tmp_path: Path) -> None:
    tools = LocalTools(tmp_path, command_timeout=60)

    async def go():
        task = asyncio.create_task(tools.run("run_command", {"command": "Start-Sleep -Seconds 30"}))
        await asyncio.sleep(1.5)
        await tools.kill()
        return await asyncio.wait_for(task, 10)

    assert asyncio.run(go()).is_error


# --- Other ----------------------------------------------------------------------------------------


def test_unknown_tool_lists_the_real_ones(tools: LocalTools) -> None:
    result = run(tools, "delete_everything")
    assert result.is_error and "read_file" in result.content


def test_display_matches_the_ui_tool_names(tools: LocalTools, tmp_path: Path) -> None:
    assert tools.display("write_file", {"path": "a.txt", "content": "x"}).input == {"file_path": str(tmp_path / "a.txt")}
    assert tools.display("run_command", {"command": "dir"}).name == "PowerShell"
    assert tools.display("edit_file", {"path": "a.txt", "old_text": "x", "new_text": "y"}).name == "Edit"


def test_plan_from_args() -> None:
    assert plan_from_args({"steps": [{"text": "a", "done": True}, {"text": "b", "done": False}, "c"]}) == [
        {"text": "a", "status": "completed"},
        {"text": "b", "status": "in_progress"},
        {"text": "c", "status": "pending"},
    ]
    with pytest.raises(ToolError):
        plan_from_args({"steps": []})


def test_cap_keeps_start_and_end() -> None:
    text = "a" * 100 + "b" * 100
    capped = cap(text, 60)
    assert capped.startswith("a" * 20) and capped.endswith("b" * 40) and "140 characters cut" in capped
