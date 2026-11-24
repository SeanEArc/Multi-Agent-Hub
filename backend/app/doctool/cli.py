"""`hub-doc` entry point: parses the command, checks where it may write, runs the work in a capped child process
and prints one JSON object.

Why a child: BetterOffice is Rust, and running out of memory under a job-object cap aborts the whole process with
no Python exception (probe addendum, 2026-11-23), so the process doing the work can't be the one that answers.
The parent here never opens a document; it supervises the child (`app.doctool.worker`), which caps its own memory
before reading anything, and reports "too large" when the child dies. Word itself (page images, Compare) runs
from the parent through PowerShell COM scripts (`app.doctool.office`), because Office is a separate program.
"""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from app.doctool.contract import (
    CHILD_FLAG,
    OUTPUTS_ENV,
    FAILED,
    OK,
    WORK_TIMEOUT,
    Failed,
    HubDocError,
    Refused,
    memory_limit,
    read_roots,
    short,
)
from app.doctool.outputs import OutputsFolder, Staging
from app.doctool.paths import real, within_any

USAGE = """\
hub-doc <format> <action> <file> [options]   (one JSON object on stdout; exit 0 ok, 1 refused, 2 failed)

Excel (.xlsx, .xlsm; .xls is read through a fallback and saved as .xlsx):
  hub-doc xlsx check <file>                                  recalculate; list formula errors and charts
  hub-doc xlsx read <file> [--sheet S] [--range A1:F20]      computed values as a Markdown grid
  hub-doc xlsx edit <file> --set "Sheet!E3==IF(B3=0,0,D3/B3)" [--set ...] [--out NAME.xlsx]
                                                             recalculated copy; changed cells and errors left
  hub-doc xlsx render <file> [--sheet S] [--range A1:F20] [--out NAME.png]   picture of a sheet or range
  hub-doc xlsx csv <file> [--sheet S] [--range A1:F20] [--out NAME.csv]      computed values as CSV
Word (.docx):
  hub-doc docx read <file> [--view markup|accepted|original] the text as Markdown, tracked changes shown
  hub-doc docx report <file>                                 formatting report: styles, fonts, spacing, lists,
                                                             tracked changes, inconsistencies
  hub-doc docx edit <file> --replace OLD NEW [--replace ...] [--out NAME.docx]   keeps run formatting
  hub-doc docx diff <original> <edited>                      changed paragraphs
  hub-doc docx pages <file> [--max-pages N]                  (Office) PDF and PNG page images
  hub-doc docx compare <original> <edited> [--author NAME] [--out NAME.docx]   (Office) tracked-changes copy
PDF:
  hub-doc pdf pages <file> [--max-pages N]                   PNG page images
Files are written only to the session's outputs folder (HUB_DOC_OUTPUTS); --out is a name in it."""


@dataclass
class Command:
    format: str
    action: str
    files: list[str]
    out: str | None = None
    sheet: str | None = None
    range: str | None = None
    sets: list[str] = field(default_factory=list)
    replaces: list[list[str]] = field(default_factory=list)
    view: str = "markup"
    author: str | None = None
    max_pages: int = 20
    scale: float = 1.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Command":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Action:
    inputs: int  # how many files it reads
    out_suffixes: tuple[str, ...] = ()  # empty: writes nothing
    office: bool = False  # Word runs in the parent
    default_name: str = ""  # with {stem} and {sheet}


ACTIONS: dict[str, dict[str, Action]] = {
    "xlsx": {
        "check": Action(1),
        "read": Action(1),
        "edit": Action(1, (".xlsx", ".xlsm"), default_name="{stem} (edited){suffix}"),
        "render": Action(1, (".png",), default_name="{stem} ({sheet}).png"),
        "csv": Action(1, (".csv",), default_name="{stem} ({sheet}).csv"),
    },
    "docx": {
        "read": Action(1),
        "report": Action(1),
        "edit": Action(1, (".docx",), default_name="{stem} (edited).docx"),
        "diff": Action(2),
        "pages": Action(1, (".pdf",), office=True, default_name="{stem} (pages).pdf"),
        "compare": Action(2, (".docx",), office=True, default_name="{stem} (changes).docx"),
    },
    "pdf": {
        "pages": Action(1, (".png",), default_name="{stem} (page {page}).png"),
    },
}
SUFFIXES = {"xlsx": (".xlsx", ".xlsm", ".xls"), "docx": (".docx",), "pdf": (".pdf",)}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # never print usage and exit 2 on its own
        raise Failed(f"bad arguments: {message}. Run `hub-doc help` for the commands.")


def parse(argv: Sequence[str]) -> Command:
    parser = _Parser(prog="hub-doc", add_help=False, allow_abbrev=False)
    parser.add_argument("format")
    parser.add_argument("action")
    parser.add_argument("files", nargs="*")
    parser.add_argument("--out")
    parser.add_argument("--sheet")
    parser.add_argument("--range")
    parser.add_argument("--set", dest="sets", action="append", default=[])
    parser.add_argument("--replace", dest="replaces", nargs=2, action="append", default=[], metavar=("OLD", "NEW"))
    parser.add_argument("--view", default="markup", choices=["markup", "accepted", "original"])
    parser.add_argument("--author")
    parser.add_argument("--max-pages", dest="max_pages", type=int, default=20)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args(list(argv))
    return Command(**vars(args))


def check(command: Command) -> Action:
    """The action, once the command names a known one with the files it needs. Raises Failed."""
    action = ACTIONS.get(command.format, {}).get(command.action)
    if action is None:
        known = ", ".join(f"{fmt} {name}" for fmt, names in ACTIONS.items() for name in names)
        raise Failed(f"unknown command `{command.format} {command.action}`. Commands: {known}.")
    if len(command.files) != action.inputs:
        raise Failed(f"`{command.format} {command.action}` takes {action.inputs} file"
                     f"{'s' if action.inputs != 1 else ''}, got {len(command.files)}.")
    roots = read_roots()
    # Under the hub (an outputs folder is set) a missing read limit doesn't mean "any file": an agent can't widen
    # what it reads by unsetting HUB_DOC_READ_ROOTS. It then reads only the session's folder (the outputs folder's
    # parent: its attachments and outputs). Run by hand with neither variable set, hub-doc reads any file.
    fallback = session_folder() if roots is None else None
    for index, name in enumerate(list(command.files)):
        path = Path(name)
        # Before anything else, so a refused path doesn't even say whether it exists.
        if roots is not None and not within_any(path, roots):
            raise Refused(f"{name} is outside the folders hub-doc may read: the session's attached files, its "
                          "outputs folder and its working folder.")
        if not path.is_file():
            raise Failed(f"{name} doesn't exist (or isn't a file).")
        if fallback is not None and not within_any(path, [fallback]):
            raise Refused(f"{name} is outside the folders hub-doc may read: HUB_DOC_READ_ROOTS isn't set, so only "
                          "the session's own folder (attached files and outputs) can be read.")
        # From here on the file is used by its resolved name: an 8.3 short name cuts `.xlsx` to `.XLS`, and a
        # `\\?\` prefix or a junction would otherwise reach the libraries and Word as typed.
        path = real(path)
        command.files[index] = name = str(path)
        if path.suffix.lower() not in SUFFIXES[command.format]:
            raise Failed(f"{name} isn't a {command.format} file ({', '.join(SUFFIXES[command.format])}).")
    if command.format == "xlsx" and command.action == "edit" and not command.sets:
        raise Failed('`xlsx edit` needs at least one --set "Sheet!A1=value" (a formula starts with =).')
    if command.format == "docx" and command.action == "edit" and not command.replaces:
        raise Failed("`docx edit` needs at least one --replace OLD NEW.")
    if not 1 <= command.max_pages <= 200:
        raise Failed("--max-pages must be between 1 and 200.")
    if not 0.1 <= command.scale <= 4:
        raise Failed("--scale must be between 0.1 and 4.")
    return action


def session_folder() -> str | None:
    """The outputs folder's parent (`<attachments>/<session id>`) when an outputs folder is set, else None."""
    outputs = os.environ.get(OUTPUTS_ENV, "").strip()
    return os.path.dirname(os.path.realpath(outputs)) if outputs else None


def default_name(command: Command, action: Action, **extra: object) -> str:
    source = Path(command.files[0])
    suffix = source.suffix.lower() if source.suffix.lower() in action.out_suffixes else action.out_suffixes[0]
    sheet = command.sheet or "sheet"
    name = action.default_name.format(stem=source.stem, suffix=suffix, sheet=sheet, **extra)
    return "".join("_" if c in '<>:"/\\|?*' else c for c in name)


# --- The supervising parent ------------------------------------------------------------------------------------


def child_command() -> list[str]:
    if getattr(sys, "frozen", False):  # the bundled hub-doc.exe runs its own child mode
        return [sys.executable, CHILD_FLAG]
    # -I: no user site-packages, no script or current folder on the import path; the hub's package is installed.
    return [sys.executable, "-I", "-m", "app.doctool", CHILD_FLAG]


def run_child(request: dict[str, Any], limit: int, timeout: float = WORK_TIMEOUT) -> dict[str, Any]:
    """Run one job in the capped child and return its reply. Raises Failed when it dies or runs too long."""
    env = {**os.environ, "OPENBLAS_NUM_THREADS": "1", "PYTHONIOENCODING": "utf-8"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(child_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, creationflags=flags)
    job = None
    try:
        if os.name == "nt":
            # Kills the child (and, in a venv, the interpreter its launcher started) if this parent ends or gives
            # up. The child caps itself from inside too (`worker.main`), which is the cap that holds.
            from app.extract_worker import WindowsJob

            try:
                job = WindowsJob(limit, active_processes=2)
                job.assign(proc.pid)
            except OSError:
                job = None
        try:
            stdout, stderr = proc.communicate(json.dumps({**request, "memory_limit": limit}).encode(), timeout)
        except subprocess.TimeoutExpired:
            raise Failed(f"the work took longer than {timeout:g} seconds and was stopped.") from None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if job is not None:
            job.close()
    lines = stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        reply = json.loads(lines[-1]) if lines else None
    except ValueError:
        reply = None
    if isinstance(reply, dict) and isinstance(reply.get("ok"), bool):
        return reply
    text = stderr.decode("utf-8", "replace")
    code = (proc.returncode or 0) & 0xFFFFFFFF
    if "memory allocation" in text or "MemoryError" in text or code in OUT_OF_MEMORY_CODES:
        raise Failed(f"the file is too large: the work needed more memory than hub-doc allows "
                     f"({limit // (1024 * 1024)} MB), so it was stopped and nothing was written.")
    raise Failed(f"hub-doc's worker stopped unexpectedly (exit code {proc.returncode}): {short(text[-600:]) or 'no output'}")


# STATUS_NO_MEMORY, and the abort a Rust allocation failure ends in (STATUS_STACK_BUFFER_OVERRUN from abort()); 3 is
# the C runtime's abort(). Windows reports exit codes unsigned (3221225495), so they are compared as 32-bit values.
OUT_OF_MEMORY_CODES = {0xC0000017, 0xC0000409, 3}


def execute(command: Command) -> dict[str, Any]:
    """Run a checked command and return the reply data (without `ok`). Raises HubDocError."""
    action = check(command)
    if not action.out_suffixes:
        return _from_child(run_child({"command": command.to_json()}, memory_limit()))
    outputs = OutputsFolder.from_env()
    target = None
    if command.format != "pdf":
        target = outputs.target(command.out, default_name(command, action), command.files, action.out_suffixes)
    outputs.require()
    staging = Staging(outputs)
    try:
        if action.office:
            from app.doctool import office

            return office.run_action(command, target, outputs, staging)
        if command.format == "pdf":
            return render_pages(command, Path(command.files[0]), outputs, staging)
        staged = staging.file(target.suffix)
        reply = _from_child(run_child({"command": command.to_json(), "output": str(staged)}, memory_limit()))
        reply["output"] = str(staging.commit(staged, target))
        return reply
    finally:
        staging.remove()


def render_pages(command: Command, pdf: Path, outputs: OutputsFolder, staging: Staging) -> dict[str, Any]:
    """PNG page images of a PDF (pypdfium2, in the capped child), committed as `<first input's name> (page N).png`
    (for `docx pages` the PDF is Word's, staged, and the pages are named after the document)."""
    request = {"command": command.to_json(), "pdf": str(pdf), "staging": str(staging.path), "job": "pdf-pages"}
    reply = _from_child(run_child(request, memory_limit()))
    pages = []
    for number, staged in enumerate(reply.pop("staged", []), 1):
        name = default_name(command, ACTIONS["pdf"]["pages"], page=number)
        target = outputs.target(name, name, command.files, (".png",))
        pages.append(str(staging.commit(Path(staged), target)))
    reply["pages"] = pages
    return reply


def _from_child(reply: dict[str, Any]) -> dict[str, Any]:
    if reply.get("ok"):
        return {k: v for k, v in reply.items() if k != "ok"}
    error = str(reply.get("error") or "the work failed")
    raise (Refused if reply.get("refused") else Failed)(error)


def emit(data: dict[str, Any], code: int) -> NoReturn:
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def main() -> NoReturn:
    # Agents read the reply as UTF-8 whatever the console's code page.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    argv = sys.argv[1:]
    if argv[:1] == [CHILD_FLAG]:
        from app.doctool.worker import main as child

        child()
        sys.exit(0)
    if not argv or argv[0] in ("help", "--help", "-h", "/?"):
        emit({"ok": True, "usage": USAGE}, OK)
    try:
        command = parse(argv)
        data = execute(command)
    except HubDocError as exc:
        emit({"ok": False, "error": str(exc)}, exc.code)
    except Exception as exc:  # never a traceback on stdout: the agent reads JSON
        emit({"ok": False, "error": f"hub-doc failed: {exc.__class__.__name__}: {short(exc)}"}, FAILED)
    emit({"ok": True, **data}, OK)
