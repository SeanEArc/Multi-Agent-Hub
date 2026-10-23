import os
import string
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from sqlmodel import SQLModel

router = APIRouter(prefix="/folders", tags=["folders"])


class FolderEntry(SQLModel):
    name: str
    path: str


class FolderListing(SQLModel):
    # None for the top level (home folder and drives).
    path: str | None
    parent: str | None
    folders: list[FolderEntry]


def _roots() -> list[FolderEntry]:
    home = Path.home()
    roots = [FolderEntry(name=f"Home ({home.name})", path=str(home))]
    if os.name == "nt":
        drives = (f"{letter}:\\" for letter in string.ascii_uppercase)
        roots += [FolderEntry(name=drive, path=drive) for drive in drives if os.path.isdir(drive)]
    else:
        roots.append(FolderEntry(name="/", path="/"))
    return roots


@router.get("", response_model=FolderListing)
def list_folders(path: str | None = None, show_hidden: bool = False) -> FolderListing:
    """Subfolders of `path` (files are never listed), for the UI's folder picker.

    With no `path`, returns the home folder and the drives as starting points.
    """
    if not path:
        return FolderListing(path=None, parent=None, folders=_roots())

    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
    folder = folder.resolve()

    try:
        children = [
            entry
            for entry in os.scandir(folder)
            if _is_dir(entry) and (show_hidden or not _is_hidden(entry))
        ]
    except PermissionError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Can't open this folder") from None

    children.sort(key=lambda entry: entry.name.lower())
    # A drive root's parent is itself; go back to the top level instead.
    parent = str(folder.parent) if folder.parent != folder else None
    return FolderListing(
        path=str(folder),
        parent=parent,
        folders=[FolderEntry(name=entry.name, path=str(folder / entry.name)) for entry in children],
    )


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith(".") or entry.name.startswith("$"):
        return True
    if os.name == "nt":
        try:
            attributes = entry.stat(follow_symlinks=False).st_file_attributes
        except OSError:
            return True
        return bool(attributes & (0x2 | 0x4))  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
    return False
