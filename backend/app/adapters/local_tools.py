"""The local model's tools: what the hub's own agent loop lets the model do, and how each is run.

Claude and Codex bring their own tools; a local model gets this small set, written for small
models: plain JSON arguments, exact-text edits, and short error messages that say how to fix
the call. Every path is checked to stay inside the session's working folder. Commands run in
Windows PowerShell through the session's Runner, so phase 9's Docker runner applies to them too.
This is not a sandbox: a command can still reach anything the user's account can.

The events use the same tool names and shapes the UI already renders for Claude (`Read`,
`Write`, `Edit`, `PowerShell` with `file_path` or `command`), with `diffs` on edits and
`exit_code` on commands.
"""

import asyncio
import fnmatch
import inspect
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.adapters.cli import HEADLESS_GIT_ENV, plan_items
from app.adapters.diffs import MAX_FILE_BYTES, file_diff, hunks
from app.adapters.runner import HostRunner, RunningProcess, Runner

DEFAULT_COMMAND_TIMEOUT = 300
# What goes back to the model from one tool call. The context is small, so long output is cut.
MAX_RESULT_CHARS = 12_000
READ_MAX_LINES = 400
LIST_MAX_ENTRIES = 300
SEARCH_MAX_MATCHES = 100
# Folders that are rarely what the model is looking for and can be huge.
SKIPPED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
                ".ruff_cache", "dist", "build", "target", ".next", ".turbo"}

# Run before the model's command: UTF-8 output (PowerShell 5.1 uses the OEM code page in a pipe)
# and no progress bars, which would otherwise arrive on stderr.
PS_PREFIX = (
    "$ProgressPreference = 'SilentlyContinue'\n"
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
    "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
)
# Run after it: PowerShell's own exit code is only 0 or 1, so pass on a program's real exit code.
PS_SUFFIX = (
    "\n$__hubOk = $?; $__hubCode = $LASTEXITCODE\n"
    "if (-not $__hubOk) { if ($__hubCode) { exit $__hubCode } else { exit 1 } }\n"
)


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


TOOL_DEFINITIONS = [
    _fn("read_file", "Read a text file. Shows line numbers, which are not part of the file.",
        {"path": {"type": "string", "description": "File path, relative to the working folder"},
         "start_line": {"type": "integer", "description": "First line to show (default 1)"},
         "end_line": {"type": "integer", "description": "Last line to show (default: 400 lines on)"}},
        ["path"]),
    _fn("list_files", "List the files and folders in a folder.",
        {"path": {"type": "string", "description": "Folder path (default: the working folder)"},
         "recursive": {"type": "boolean", "description": "Also list everything in subfolders"}},
        []),
    _fn("search", "Find lines containing some text in the project's files.",
        {"text": {"type": "string", "description": "Text to look for (not case-sensitive)"},
         "path": {"type": "string", "description": "Folder or file to search (default: the working folder)"},
         "file_pattern": {"type": "string", "description": "Only files whose name matches, like *.py"},
         "regex": {"type": "boolean", "description": "Treat text as a regular expression"}},
        ["text"]),
    _fn("write_file", "Create a file, or replace a whole file, with the given content.",
        {"path": {"type": "string", "description": "File path, relative to the working folder"},
         "content": {"type": "string", "description": "The complete file content"}},
        ["path", "content"]),
    _fn("edit_file", "Replace one exact piece of text in a file. old_text must appear exactly once; "
                     "include a few surrounding lines to make it unique.",
        {"path": {"type": "string", "description": "File path, relative to the working folder"},
         "old_text": {"type": "string", "description": "The exact text to replace, copied from the file"},
         "new_text": {"type": "string", "description": "The text to put in its place"}},
        ["path", "old_text", "new_text"]),
    _fn("run_command", "Run a Windows PowerShell command in the working folder and get its output and exit code.",
        {"command": {"type": "string", "description": "The PowerShell command"}},
        ["command"]),
    _fn("update_plan", "Show your plan for the task as a checklist. Call it with all steps at the start, "
                       "and again whenever you finish a step.",
        {"steps": {"type": "array", "description": "Every step, in order", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "done": {"type": "boolean"}},
            "required": ["text", "done"]}}},
        ["steps"]),
    _fn("ask_user", "Ask the user a question and wait for the answer. Use it only when you can't continue without it.",
        {"question": {"type": "string", "description": "The question"}},
        ["question"]),
]
TOOL_NAMES = [t["function"]["name"] for t in TOOL_DEFINITIONS]


class ToolError(Exception):
    """A tool call that couldn't run; the message goes back to the model so it can fix the call."""


@dataclass
class ToolResult:
    # What the model gets back.
    content: str
    is_error: bool = False
    # Added to the TOOL_RESULT event (diffs, exit_code).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolDisplay:
    """How a call shows in the transcript: the name and input of its TOOL_USE event."""
    name: str
    input: dict[str, Any]


def cap(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """Long output keeps its start and its end, where errors and summaries usually are."""
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return f"{text[:head]}\n… [{len(text) - limit} characters cut] …\n{text[-tail:]}"


class LocalTools:
    """Runs the local model's tool calls in one working folder."""

    def __init__(self, cwd: Path, runner: Runner | None = None,
                 command_timeout: float = DEFAULT_COMMAND_TIMEOUT) -> None:
        self.cwd = Path(cwd)
        self.root = self.cwd.resolve()
        self.runner = runner or HostRunner()
        self.command_timeout = command_timeout
        self._process: RunningProcess | None = None
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "read_file": self._read_file,
            "list_files": self._list_files,
            "search": self._search,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "run_command": self._run_command,
        }

    # --- Shown in the transcript ------------------------------------------------------------------

    def display(self, name: str, args: dict[str, Any]) -> ToolDisplay:
        def path(key: str = "path", default: str | None = None) -> str:
            raw = args.get(key, default)
            if not isinstance(raw, str) or not raw:
                return str(raw)
            # Absolute, like Claude's file_path, so the UI shows it relative to the project.
            return str(self.cwd / raw) if not Path(raw).is_absolute() else raw

        match name:
            case "read_file":
                extra = {k: args[k] for k in ("start_line", "end_line") if k in args}
                return ToolDisplay("Read", {"file_path": path(), **extra})
            case "list_files":
                return ToolDisplay("List", {"path": path(default="."), "recursive": bool(args.get("recursive"))})
            case "search":
                return ToolDisplay("Search", {"pattern": args.get("text"), "in": args.get("path") or ".",
                                              **({"file_pattern": args["file_pattern"]} if args.get("file_pattern") else {})})
            case "write_file":
                return ToolDisplay("Write", {"file_path": path()})
            case "edit_file":
                return ToolDisplay("Edit", {"file_path": path(), "old_string": args.get("old_text"),
                                            "new_string": args.get("new_text")})
            case "run_command":
                return ToolDisplay("PowerShell", {"command": args.get("command")})
        return ToolDisplay(name, args)

    # --- Running ----------------------------------------------------------------------------------

    async def run(self, name: str, args: dict[str, Any]) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(f"Error: there is no tool named {name}. The tools are: {', '.join(TOOL_NAMES)}.", True)
        try:
            if inspect.iscoroutinefunction(handler):
                return await handler(args)
            return await asyncio.to_thread(handler, args)
        except ToolError as exc:
            return ToolResult(f"Error: {exc}", True)
        except OSError as exc:
            return ToolResult(f"Error: {exc.strerror or exc}", True)

    async def kill(self) -> None:
        """Stop a running command (the user pressed Stop)."""
        if self._process is not None:
            await self._process.kill()

    def resolve(self, raw: Any, *, writing: bool = False) -> Path:
        """The path inside the working folder, or a ToolError for anything outside it."""
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("a path is required.")
        path = Path(raw.strip())
        resolved = (path if path.is_absolute() else self.root / path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ToolError(f"{raw} is outside the working folder ({self.root}). Use a path inside it.")
        if writing and ".git" in resolved.relative_to(self.root).parts:
            raise ToolError("files inside .git can't be changed with file tools.")
        return resolved

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def _read_file(self, args: dict[str, Any]) -> ToolResult:
        path = self.resolve(_required(args, "path"))
        text = _read_text(path)
        lines = text.splitlines()
        start = max(1, _int(args.get("start_line"), 1))
        end = min(len(lines), _int(args.get("end_line"), start + READ_MAX_LINES - 1), start + READ_MAX_LINES - 1)
        if not lines:
            return ToolResult("(the file is empty)")
        if start > len(lines):
            raise ToolError(f"the file has only {len(lines)} lines.")
        width = len(str(end))
        shown = "\n".join(f"{n:>{width}}: {lines[n - 1]}" for n in range(start, end + 1))
        note = "" if (start, end) == (1, len(lines)) else \
            f"\n(lines {start}-{end} of {len(lines)}; use start_line to read more)"
        return ToolResult(cap(shown) + note)

    def _list_files(self, args: dict[str, Any]) -> ToolResult:
        folder = self.resolve(args.get("path") or ".")
        if not folder.is_dir():
            raise ToolError(f"{args.get('path')} is not a folder.")
        entries: list[str] = []
        if args.get("recursive"):
            for dirpath, dirnames, filenames in os.walk(folder):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
                base = Path(dirpath)
                entries += [self.relative(base / f) for f in sorted(filenames)]
                if len(entries) > LIST_MAX_ENTRIES:
                    break
        else:
            children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            entries = [p.name + ("/" if p.is_dir() else "") for p in children]
        if not entries:
            return ToolResult("(the folder is empty)")
        more = len(entries) - LIST_MAX_ENTRIES
        listed = "\n".join(entries[:LIST_MAX_ENTRIES])
        return ToolResult(listed + (f"\n… and more (list a subfolder to see the rest)" if more > 0 else ""))

    def _search(self, args: dict[str, Any]) -> ToolResult:
        text = _required(args, "text")
        start = self.resolve(args.get("path") or ".")
        try:
            pattern = re.compile(text if args.get("regex") else re.escape(text), re.IGNORECASE)
        except re.error as exc:
            raise ToolError(f"that isn't a valid regular expression ({exc}).") from None
        name_filter = args.get("file_pattern") or None
        files = [start] if start.is_file() else _walk_files(start)
        matches: list[str] = []
        for file in files:
            if name_filter and not fnmatch.fnmatch(file.name, name_filter):
                continue
            try:
                body = _read_text(file)
            except ToolError:
                continue
            for number, line in enumerate(body.splitlines(), 1):
                if pattern.search(line):
                    matches.append(f"{self.relative(file)}:{number}: {line.strip()[:200]}")
                    if len(matches) >= SEARCH_MAX_MATCHES:
                        return ToolResult("\n".join(matches) + f"\n(stopped at {SEARCH_MAX_MATCHES} matches)")
        return ToolResult("\n".join(matches) if matches else "No matches.")

    def _write_file(self, args: dict[str, Any]) -> ToolResult:
        path = self.resolve(_required(args, "path"), writing=True)
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content is required (the whole file, as text).")
        if path.is_dir():
            raise ToolError(f"{args['path']} is a folder.")
        existed = path.exists()
        try:
            before = _read_text(path, missing_ok=True) if existed else ""
        except ToolError:
            before = None  # binary or too large: replaced, but no diff to show
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text(path, content)
        diff = file_diff(str(path), "update" if existed else "add", hunks(before, content) if before is not None else None)
        verb = "Replaced" if existed else "Created"
        return ToolResult(f"{verb} {self.relative(path)} ({len(content.splitlines())} lines).", extra={"diffs": [diff]})

    def _edit_file(self, args: dict[str, Any]) -> ToolResult:
        path = self.resolve(_required(args, "path"), writing=True)
        old, new = args.get("old_text"), args.get("new_text")
        if not isinstance(old, str) or not old:
            raise ToolError("old_text is required: the exact text to replace, copied from the file.")
        if not isinstance(new, str):
            raise ToolError("new_text is required (it can be empty to delete the text).")
        if not path.is_file():
            raise ToolError(f"{args['path']} doesn't exist. Use write_file to create it.")
        before = _read_text(path)
        count = before.count(old)
        if count == 0 and "\r\n" in before and "\r\n" not in old:
            # The model writes \n; the file may use Windows line endings.
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            count = before.count(old)
        if count == 0:
            raise ToolError("old_text wasn't found in the file. Read the file and copy the text exactly, "
                            "including spaces and indentation.")
        if count > 1:
            raise ToolError(f"old_text appears {count} times. Include more surrounding lines so it matches once.")
        after = before.replace(old, new, 1)
        _write_text(path, after)
        diff = file_diff(str(path), "update", hunks(before, after))
        return ToolResult(f"Edited {self.relative(path)}.", extra={"diffs": [diff]})

    async def _run_command(self, args: dict[str, Any]) -> ToolResult:
        command = _required(args, "command")
        # A script file instead of -Command: no Windows quoting, no command-line length limit. PowerShell
        # 5.1 reads a .ps1 as UTF-8 only with a byte-order mark.
        fd, script = tempfile.mkstemp(prefix="hub-cmd-", suffix=".ps1")
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(PS_PREFIX + command + PS_SUFFIX)
        try:
            proc = await self.runner.start(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script],
                self.cwd, HEADLESS_GIT_ENV,
            )
            self._process = proc
            try:
                await proc.send_input(b"")  # nothing can wait for input
                output, timed_out = await self._collect(proc)
                code = None if timed_out else await proc.wait()
            finally:
                self._process = None
                await proc.kill()
        finally:
            Path(script).unlink(missing_ok=True)
        output = cap(output.replace("\r\n", "\n").rstrip())
        if timed_out:
            note = f"The command was stopped after {self.command_timeout:g} seconds."
            return ToolResult(f"{output}\n{note}".strip(), True, {"exit_code": None})
        shown = output or "(no output)"
        return ToolResult(f"Exit code {code}\n{shown}" if code else shown, code != 0, {"exit_code": code})

    async def _collect(self, proc: RunningProcess) -> tuple[str, bool]:
        """stdout then stderr, and whether the timeout ran out."""
        stderr = asyncio.create_task(proc.read_stderr())
        lines: list[bytes] = []
        size = 0

        async def read() -> None:
            nonlocal size
            async for line in proc.lines():
                # Keep reading so the process never blocks on a full pipe, but stop storing.
                if size < MAX_RESULT_CHARS * 4:
                    lines.append(line)
                    size += len(line)

        try:
            await asyncio.wait_for(read(), self.command_timeout)
            timed_out = False
        except TimeoutError:
            timed_out = True
            await proc.kill()
        try:
            err = await asyncio.wait_for(stderr, 5)
        except TimeoutError:
            err = b""
        out = b"".join(lines).decode("utf-8", "replace")
        err_text = err.decode("utf-8", "replace").strip()
        return (out + ("\n" + err_text if err_text else "")), timed_out


def plan_from_args(args: dict[str, Any]) -> list[dict[str, str]]:
    """update_plan's steps as plan items. Raises ToolError for a malformed list."""
    steps = args.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ToolError("steps is required: a list of {text, done}.")
    parsed = []
    for step in steps:
        if isinstance(step, str):
            parsed.append((step, False))
        elif isinstance(step, dict) and isinstance(step.get("text"), str):
            parsed.append((step["text"], bool(step.get("done"))))
        else:
            raise ToolError("each step needs a text and done (true or false).")
    return plan_items(parsed)


def _required(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ToolError(f"{key} is required.")
    return value


def _int(value: Any, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _read_text(path: Path, missing_ok: bool = False) -> str | None:
    """A text file's contents. Binary and very large files are refused."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ToolError(f"{path.name} is too large to read ({path.stat().st_size // 1024} KB).")
        data = path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ToolError(f"{path.name} doesn't exist.") from None
    except IsADirectoryError:
        raise ToolError(f"{path.name} is a folder; use list_files.") from None
    except PermissionError:
        if path.is_dir():
            raise ToolError(f"{path.name} is a folder; use list_files.") from None
        raise
    if b"\0" in data[:8192]:
        raise ToolError(f"{path.name} is a binary file.")
    return data.decode("utf-8-sig", "replace")


def _write_text(path: Path, content: str) -> None:
    # newline="" writes the text as given: no \n to \r\n translation behind the model's back.
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(content)


def _walk_files(folder: Path):
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name
