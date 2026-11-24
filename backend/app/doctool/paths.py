r"""Path checks shared by hub-doc's read gate (`HUB_DOC_READ_ROOTS`) and the hub's Save as… route.

"Is this path inside that folder?" has many spellings on Windows: `\\?\C:\...` and `\\?\UNC\...` prefixes, 8.3
short names (`MULTIA~1`), case, junctions and symbolic links, and network spellings of a local folder such as the
admin share `\\localhost\C$\...`. `realpath` handles links, short names and case for parts that exist; `plain`
removes the `\\?\` prefix realpath keeps for a path that doesn't exist yet; and `within` finally compares each
existing parent with the folder by file identity (`os.path.samefile`: volume serial and file index), which also
catches a share or other route to the same folder that no string comparison would.
"""

import os
from collections.abc import Iterable
from pathlib import Path


def plain(path: str) -> str:
    r"""A Windows path without the `\\?\` (or `\\?\UNC\`) prefix that only switches off path parsing."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\") and path[5:6] == ":":
        return path[4:]
    return path


def real(path: str | Path) -> Path:
    """The path with prefixes removed and links, short names and `..` resolved (as far as it exists)."""
    return Path(plain(os.path.realpath(plain(str(path)))))


def within(path: str | Path, root: str | Path) -> bool:
    """Whether `path` is `root` or inside it, by any spelling (see the module docstring)."""
    target, folder = real(path), real(root)
    if target == folder or target.is_relative_to(folder) or \
            os.path.normcase(str(target)).startswith(os.path.normcase(str(folder)).rstrip("\\/") + os.sep):
        return True
    if not folder.is_dir():
        return False
    for parent in (target, *target.parents):
        try:
            if parent.exists() and os.path.samefile(parent, folder):
                return True
        except OSError:
            continue
    return False


def within_any(path: str | Path, roots: Iterable[str | Path]) -> bool:
    return any(within(path, root) for root in roots)
