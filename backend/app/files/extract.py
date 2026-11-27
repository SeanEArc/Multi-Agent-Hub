"""Text copies of attached documents, made in a separate, capped process.

PDF, Word, Excel and CSV parsers run on files from anywhere, and a broken or hostile file (a zip bomb inside a
.docx, a PDF that sends the parser into a loop) must not take the backend down. So each text copy is made by
`app.files.extract_worker` in its own process:

- **Memory:** the worker caps itself (`extract_worker.cap_memory`): after reading its request from stdin and
  before parsing anything, it puts its own process in a new Windows job object limited to `MEMORY_LIMIT` of
  committed memory and to one process (`RLIMIT_AS` elsewhere). If that fails it parses nothing (fail closed).
  The cap has to come from inside: in a virtual environment `python.exe` is a launcher that starts the real
  interpreter as its child, and that child can start before the hub could put the launcher in a job, so a job
  assigned from outside isn't guaranteed to contain the process that parses (QA reproduced exactly that).
- **Cleanup:** the hub still puts the worker in its own job (same memory limit, two processes for the launcher
  and its child, killed when the hub closes the handle), so a timed-out or stuck worker and its child are ended.
- **Time:** `TIMEOUT` seconds, then the worker is killed.
- **Load:** at most `CONCURRENT` text copies run at once, so several uploads can't each take `MEMORY_LIMIT`.
- **Size:** the uploaded file is already within its type's cap; the worker stops writing at `MAX_TEXT_CHARS`.
- **Isolation:** the worker runs with `python -I` (no user site-packages, no script folder or current folder on
  the import path) and imports nothing from the hub.

The worker writes to a temporary name; the hub renames it only on success, so a failure leaves no text copy.
"""

import asyncio
import json
import logging
import os
import sys
import weakref
from dataclasses import dataclass
from pathlib import Path

from app.files.extract_worker import WindowsJob

log = logging.getLogger(__name__)

TEXT_COPY_KINDS = {"pdf", "word", "excel", "csv"}
TIMEOUT = 60.0
MEMORY_LIMIT = 512 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
# The hub's job holds the venv's python.exe launcher and the interpreter it starts; the worker's own job allows one.
ACTIVE_PROCESSES = 2
CONCURRENT = 2
# One semaphore per event loop (a semaphore can't be shared between loops; tests run several).
_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def _gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _slots:
        _slots[loop] = asyncio.Semaphore(CONCURRENT)
    return _slots[loop]
WORKER = Path(__file__).with_name("extract_worker.py")
# The bundled backend (PyInstaller) has no Python to run the worker file with; hub_backend.py runs it instead.
WORKER_FLAG = "--extract-worker"


@dataclass
class TextCopy:
    path: Path | None = None
    pages: int | None = None
    error: str | None = None


def text_copy_path(original: Path) -> Path:
    return original.with_name(original.name + ".txt")


def worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_FLAG]
    return [sys.executable, "-I", str(WORKER)]


async def make_text_copy(kind: str, original: Path, *, timeout: float = TIMEOUT,
                         memory_limit: int = MEMORY_LIMIT, max_chars: int = MAX_TEXT_CHARS) -> TextCopy:
    """Write `<original>.txt` beside the file. Never raises: a failure is returned as `error`, with no file left."""
    final = text_copy_path(original)
    tmp = final.with_name(final.name + ".part")
    request = json.dumps({"kind": kind, "src": str(original), "dst": str(tmp), "max_chars": max_chars,
                          "memory_limit": memory_limit}).encode()
    try:
        async with _gate():
            result = await _run_worker(request, timeout, memory_limit)
        if not result.get("ok"):
            raise _Failed(str(result.get("error") or "the file couldn't be read"))
        if not tmp.is_file():
            raise _Failed("the reader wrote nothing")
        os.replace(tmp, final)
        pages = result.get("pages")
        return TextCopy(final, pages if isinstance(pages, int) else None)
    except _Failed as exc:
        await _discard(tmp)
        return TextCopy(error=str(exc), pages=None)
    except Exception as exc:  # a text copy is a convenience: never fail the upload over it
        log.exception("Text copy of %s failed", original)
        await _discard(tmp)
        return TextCopy(error=f"the reader failed ({exc.__class__.__name__})")


class _Failed(Exception):
    pass


async def _discard(tmp: Path) -> None:
    """Remove a partial text copy, never raising. A killed worker can hold the file open for a moment after the kill
    (in a virtual environment the interpreter is the launcher's child, ended by closing the job, which isn't
    instant), so a sharing violation is retried for up to two seconds."""
    for _ in range(20):
        try:
            tmp.unlink(missing_ok=True)
            return
        except PermissionError:
            await asyncio.sleep(0.1)
        except OSError:
            break
    log.warning("Couldn't remove the partial text copy %s", tmp)


async def _run_worker(request: bytes, timeout: float, memory_limit: int) -> dict:
    kwargs: dict = {}
    if os.name != "nt":
        kwargs["preexec_fn"] = _rlimit(memory_limit)
    proc = await asyncio.create_subprocess_exec(
        *worker_command(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    job = None
    try:
        if os.name == "nt":
            try:
                job = WindowsJob(memory_limit, active_processes=ACTIVE_PROCESSES)
                job.assign(proc.pid)
            except OSError as exc:
                raise _Failed(f"the reader couldn't be given a memory limit ({exc})") from None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(request), timeout)
        except TimeoutError:
            raise _Failed(f"reading the file took longer than {timeout:g} seconds") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if job is not None:
            job.close()
    try:
        return json.loads(stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        if b"MemoryError" in stderr or proc.returncode in (_STATUS_NO_MEMORY, -9):
            raise _Failed("the file needed more memory than allowed") from None
        raise _Failed(f"the reader stopped unexpectedly (exit code {proc.returncode})") from None


# 0xC0000017 STATUS_NO_MEMORY, as a signed exit code.
_STATUS_NO_MEMORY = -1073741801


def _rlimit(memory_limit: int):
    def apply() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))

    return apply
