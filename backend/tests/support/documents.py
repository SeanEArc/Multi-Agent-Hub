"""hub-doc run as a program (`HubDoc`), a session's files as attachments PR 1 stores them, and Windows file helpers
(junctions, files held open by another program)."""

import ctypes
import hashlib
import json
import os
import stat
import struct
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.attachments import make_read_only, mark_from_elsewhere


TIMEOUT = 180


@dataclass
class Reply:
    code: int
    data: dict
    stdout: str
    stderr: str

    def __repr__(self) -> str:  # shown when an assertion fails
        return f"Reply(code={self.code}, data={json.dumps(self.data)[:1500]}, stderr={self.stderr[-1500:]!r})"


class HubDoc:
    """Runs hub-doc as an agent would: by its name's launcher, with the session's outputs folder in its env."""

    def __init__(self, exe: str, outputs: Path | None) -> None:
        self.exe = exe
        self.outputs = outputs

    def run(self, *args: object, env: dict[str, str] | None = None) -> Reply:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("HUB_DOC_")}
        if self.outputs is not None:
            environment["HUB_DOC_OUTPUTS"] = str(self.outputs)
        environment.update(env or {})
        done = subprocess.run([self.exe, *map(str, args)], capture_output=True, env=environment, timeout=TIMEOUT,
                              stdin=subprocess.DEVNULL)
        stdout = done.stdout.decode("utf-8", "replace")
        stderr = done.stderr.decode("utf-8", "replace")
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            pytest.fail(f"hub-doc {' '.join(map(str, args))} didn't reply with one JSON object (exit {done.returncode})"
                        f"\nstdout: {stdout[-2000:]!r}\nstderr: {stderr[-2000:]!r}")
        assert isinstance(data, dict) and isinstance(data.get("ok"), bool), stdout[-2000:]
        # Exit code and reply agree.
        assert (done.returncode == 0) == data["ok"], f"exit {done.returncode} with ok={data['ok']}: {stdout[-1000:]}"
        if not data["ok"]:
            assert isinstance(data.get("error"), str) and data["error"].strip(), stdout[-1000:]
            assert "Traceback (most recent call last)" not in data["error"]
        return Reply(done.returncode, data, stdout, stderr)

    # Excel
    def xlsx_check(self, file: Path, **env: str) -> Reply:
        return self.run("xlsx", "check", file, env=env)

    def xlsx_read(self, file: Path, sheet: str | None = None, cells: str | None = None) -> Reply:
        return self.run("xlsx", "read", file, *(["--sheet", sheet] if sheet else []), *(["--range", cells] if cells else []))

    def xlsx_edit(self, file: Path, out: object, cells: dict[str, str]) -> Reply:
        sets = [arg for address, value in cells.items() for arg in ("--set", f"{address}={value}")]
        return self.run("xlsx", "edit", file, "--out", out, *sets)

    def xlsx_render(self, file: Path, out: object, sheet: str, cells: str | None = None) -> Reply:
        return self.run("xlsx", "render", file, "--sheet", sheet, *(["--range", cells] if cells else []), "--out", out)

    def xlsx_csv(self, file: Path, out: object, sheet: str) -> Reply:
        return self.run("xlsx", "csv", file, "--sheet", sheet, "--out", out)

    # Word
    def docx_report(self, file: Path) -> Reply:
        return self.run("docx", "report", file)

    def docx_read(self, file: Path) -> Reply:
        return self.run("docx", "read", file, "--view", "markup")

    def docx_replace(self, file: Path, out: object, old: str, new: str) -> Reply:
        return self.run("docx", "edit", file, "--out", out, "--replace", old, new)

    def docx_diff(self, original: Path, edited: Path) -> Reply:
        return self.run("docx", "diff", original, edited)


@dataclass
class SessionFiles:
    root: Path  # <attachments>/<session id>
    outputs: Path  # <attachments>/<session id>/outputs

    def original(self, name: str, data: bytes) -> Path:
        """An attachment as PR 1 leaves it: in its own folder, read-only, with the Mark of the Web."""
        path = self.root / uuid.uuid4().hex[:12] / name
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        mark_from_elsewhere(path)
        make_read_only(path)
        return path


def make_session(attachments: Path) -> SessionFiles:
    root = attachments / str(uuid.uuid4())
    outputs = root / "outputs"
    outputs.mkdir(parents=True)
    return SessionFiles(root, outputs)


def fingerprint(path: Path) -> dict:
    """Everything about an original that must never change."""
    info = path.stat()
    zone = None
    if os.name == "nt":
        try:
            zone = Path(f"{path}:Zone.Identifier").read_text(encoding="ascii")
        except OSError:
            zone = None
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": info.st_size,
            "mtime": info.st_mtime_ns, "read_only": not (info.st_mode & stat.S_IWRITE), "zone": zone}


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", data[16:24])


def same_file(a: object, b: object) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def junction(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=True)


# Windows CreateFileW, to hold a file open the way another program does.
GENERIC_READ = 0x80000000


FILE_SHARE_READ = 0x1


OPEN_EXISTING = 3


class Held:
    """A file held open by "another program": share mode 0 (nobody else may open it) or FILE_SHARE_READ (Excel
    holding a workbook: others may read, nobody may write, rename over or delete it)."""

    def __init__(self, path: Path, share: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32 = kernel32
        self.handle = kernel32.CreateFileW(str(path), GENERIC_READ, share, None, OPEN_EXISTING, 0, None)
        assert self.handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()

    def __enter__(self) -> "Held":
        return self

    def __exit__(self, *exc: object) -> None:
        self._kernel32.CloseHandle(self.handle)
