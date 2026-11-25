"""The session's outputs folder, `<attachments>/<session id>/outputs`: files agents write for the user (revision 4,
attachments PR 2). It lives beside the attachments, so it is outside any repository and deleted with the session.

The UI lists it and offers **Open** (the file's own app; scripts and code are only shown in Explorer, as PR 1 does
for attachments) and **Save as…** (a copy anywhere on the PC except the attachments folder, so a copy can never
land on an original). Every name is resolved and must stay inside the folder: `..`, absolute paths and junctions
an agent made that lead elsewhere are refused.
"""

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.attachments import AttachmentError, AttachmentStore, mark_from_elsewhere, openable_name
from app.doctool.contract import STAGING_PREFIX
from app.doctool.paths import real, within

OUTPUTS = "outputs"
LIST_LIMIT = 500


@dataclass(frozen=True)
class OutputFile:
    name: str  # relative to the outputs folder, with forward slashes
    size: int
    modified: float
    path: str


def changes(before: dict[str, OutputFile], after: dict[str, OutputFile]) -> list[dict[str, Any]]:
    """What an agent did between two looks at the folder, as saved on an event (`output_files`): files created or
    edited (a new size or time). Deleted files aren't listed."""
    return [
        {"name": name, "size": file.size, "modified": file.modified,
         "change": "edited" if name in before else "created"}
        for name, file in after.items()
        if name not in before or (file.size, file.modified) != (before[name].size, before[name].modified)
    ]


class OutputsStore:
    def __init__(self, attachments: AttachmentStore) -> None:
        self.attachments = attachments

    def folder(self, session_id: str, create: bool = False) -> Path:
        """The session's outputs folder (made, with plain mkdir so Codex's sandbox can use it, when `create`)."""
        folder = self.attachments.session_dir(session_id) / OUTPUTS
        if create:
            folder.mkdir(parents=True, exist_ok=True)
        return folder

    def list(self, session_id: str) -> list[OutputFile]:
        folder = self.folder(session_id)
        if not folder.is_dir():
            return []
        root = Path(os.path.realpath(folder))
        found: list[OutputFile] = []
        for dirpath, dirnames, filenames in os.walk(root):
            # Never follow a junction or link out of the folder, and skip hub-doc's staging folders.
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(STAGING_PREFIX)
                                 and Path(os.path.realpath(Path(dirpath) / d)).is_relative_to(root)
                                 and not (Path(dirpath) / d).is_junction() and not (Path(dirpath) / d).is_symlink())
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if name.startswith(STAGING_PREFIX) or path.is_symlink():
                    continue
                try:
                    info = path.stat()
                except OSError:
                    continue
                found.append(OutputFile(path.relative_to(root).as_posix(), info.st_size, info.st_mtime, str(path)))
                if len(found) >= LIST_LIMIT:
                    return found
        return found

    def file(self, session_id: str, name: str) -> Path:
        """An output file by its listed name. AttachmentError 400/404 for anything outside the folder or missing."""
        if not isinstance(name, str) or not name.strip():
            raise AttachmentError("Name a file in the outputs folder.", 400)
        root = Path(os.path.realpath(self.folder(session_id)))
        candidate = Path(name.strip())
        if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
            raise AttachmentError("That file isn't in the outputs folder.", 400)
        path = Path(os.path.realpath(root / candidate))
        if not path.is_relative_to(root) or path == root or any(p.startswith(STAGING_PREFIX) for p in candidate.parts):
            raise AttachmentError("That file isn't in the outputs folder.", 400)
        if not path.is_file():
            raise AttachmentError("File not found.", 404)
        return path

    def open_target(self, session_id: str, name: str) -> tuple[Path, bool]:
        """The file to hand to Windows and whether to only reveal it in Explorer (scripts and code are never run).
        The file gets the Mark of the Web first, so Office opens an agent's copy of a downloaded file in Protected
        View like the original."""
        path = self.file(session_id, name)
        reveal = not openable_name(path.name)
        if not reveal:
            mark_from_elsewhere(path)
        return path, reveal

    def save_copy(self, session_id: str, name: str, destination: str) -> Path:
        """Copy an output to `destination` (a full file path the user chose). Never into the attachments folder
        (where the originals are), never over a folder or a read-only file."""
        source = self.file(session_id, name)
        if not isinstance(destination, str) or not destination.strip():
            raise AttachmentError("Choose where to save the copy.", 400)
        target = Path(destination.strip())
        if not target.is_absolute():
            raise AttachmentError("The destination must be a full path, such as C:\\Users\\you\\Documents\\x.docx.", 400)
        if os.name == "nt" and (str(target).startswith(("\\\\.\\", "//./")) or "GLOBALROOT" in str(target).upper()
                                or os.path.isreserved(target)):
            # A device (\\.\, CON, NUL) or an alternate data stream (x.docx:name) is never a place for a copy.
            raise AttachmentError(f"{destination.strip()} isn't a file name Windows can save to; choose a normal "
                                  "file name.", 400)
        # `\\?\C:\...` is the same place as `C:\...`, but realpath keeps the prefix for a file that doesn't exist yet,
        # and the check below then misses the attachments folder (a copy landed among the originals).
        resolved = real(target)
        # By any spelling, and by file identity for each existing parent, so another route to the same folder (an
        # admin share such as \\localhost\C$\...) is refused too.
        if within(resolved, self.attachments.root) or within(target.parent, self.attachments.root):
            raise AttachmentError("Copies can't be saved inside the hub's attachments folder, where the originals "
                                  "are kept. Choose another folder.", 403)
        if not resolved.parent.is_dir():
            raise AttachmentError(f"The folder {resolved.parent} doesn't exist.", 400)
        if resolved.is_dir():
            raise AttachmentError(f"{resolved} is a folder; add a file name.", 400)
        if resolved.exists() and not resolved.stat().st_mode & stat.S_IWRITE:
            raise AttachmentError(f"{resolved.name} is read-only, so it wasn't replaced.", 409)
        try:
            shutil.copyfile(source, resolved)
        except OSError as exc:
            raise AttachmentError(f"The copy couldn't be saved: {exc.strerror or exc}", 409) from None
        mark_from_elsewhere(resolved)
        return resolved
