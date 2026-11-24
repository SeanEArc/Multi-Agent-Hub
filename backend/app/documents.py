"""What each agent gets for working on documents (revision 4, attachments PR 2): the session's outputs folder and
the `hub-doc` toolkit (`app.doctool`).

- **Outputs folder** `<attachments>/<session id>/outputs` (`AttachmentStore.outputs_dir`), made before every turn.
  Claude writes there through its existing `--add-dir` (the session's attachments folder); Codex gets it as its
  sandbox's only extra writable root (never the whole session folder: the sandbox could then clear an original's
  read-only flag, probe 2026-11-21); the local model gets it as a `LocalTools` write root.
- **hub-doc on PATH**: from source, `uv sync` makes a launcher in the venv's Scripts folder, next to `python.exe`;
  the hub copies it into a bin folder of its own (`HUB_TOOLS_BIN`) so the venv's Python never shadows the user's.
  In the bundle, `hub-doc.exe` sits beside `hub-backend.exe` and needs its `_internal` folder, so that folder is
  used as is (it holds no `python.exe`).
- **Claude** gets `hub-doc` pre-approved with `--allowedTools "Bash(hub-doc *)" "PowerShell(hub-doc *)"` (the first
  tool the hub pre-approves, decided with the user): under the hub's settings Claude's shell otherwise runs no
  program, and the rules match only the bare name, so the agents are told to call it that way.
- Every agent's environment carries `HUB_DOC_OUTPUTS` and `HUB_DOC_READ_ROOTS` (hub-doc reads only the session's
  attachments, outputs and working folder: a ruling, so the pre-approved command can't read the user's other
  documents), and its instructions name the folder and the commands.

Not a sandbox: an agent with a shell can change these variables. `hub-doc` refusing writes outside the folder is
what keeps an allowed command from writing anywhere, and the originals are also read-only.
"""

import filecmp
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from app.doctool import office_registry
from app.doctool.contract import OUTPUTS_ENV, READ_ROOTS_ENV
from app.doctool.office_registry import REGISTRY_ENV

log = logging.getLogger(__name__)

TOOL = "hub-doc"
LAUNCHER = "hub-doc.exe" if os.name == "nt" else "hub-doc"
CLAUDE_RULES = ("Bash(hub-doc *)", "PowerShell(hub-doc *)")


@dataclass(frozen=True)
class DocumentAccess:
    """One turn's outputs folder and toolkit, given to the agent's adapter by the orchestrator."""

    outputs: Path
    bin_dir: Path | None = None  # the folder holding hub-doc; None when it isn't installed
    office: bool = False  # Word passed the hub's test conversion: page images and Compare work
    # What hub-doc may read: the session's attachments folder, its outputs folder and its working folder.
    read_roots: tuple[Path, ...] = ()

    @property
    def toolkit(self) -> bool:
        return self.bin_dir is not None

    def env(self) -> dict[str, str]:
        """Added to the agent's environment: hub-doc first on PATH, and the outputs folder."""
        env = {OUTPUTS_ENV: str(self.outputs)}
        # The hub's own Office lock folder, resolved here: hub-doc in every agent then shares the hub's lock whatever
        # the agent's TEMP is (Codex's sandbox may point it elsewhere). If an agent can't write there, its Word
        # actions fail closed with a visible "lock folder can't be used", never a silent overlap.
        env[REGISTRY_ENV] = os.path.realpath(office_registry.folder())
        if self.read_roots:
            env[READ_ROOTS_ENV] = os.pathsep.join(str(root) for root in self.read_roots)
        if self.bin_dir is not None:
            env["PATH"] = str(self.bin_dir) + os.pathsep + os.environ.get("PATH", "")
        return env

    def instructions(self, author: str) -> str:
        """The agent's instructions: where to write, and how to call hub-doc."""
        lines = [
            "## Documents and the outputs folder",
            "",
            f"Write every new file for the user (edited copies, exports, pictures) to the session's outputs folder: "
            f"{self.outputs}. Never change attached files; save a changed copy under a new name, such as "
            "\"report (edited).docx\". Writing a name that is already in the outputs folder replaces your earlier "
            "file there.",
        ]
        if not self.toolkit:
            return "\n".join(lines)
        lines += [
            "",
            f"For Word and Excel files use the hub's toolkit, the command `{TOOL}`. Call it by that bare name "
            "(never by a path), one command per call, not chained with other commands. Each call prints one JSON "
            "object: `ok`, and `error` when it fails (exit code 0 ok, 1 refused, 2 failed). `--out` takes a file "
            "name, which goes in the outputs folder. It reads only the attached files, the outputs folder and your working "
            "folder. Run `hub-doc help` for every option.",
            "- `hub-doc xlsx check <file>`: recalculates every formula; lists the formula errors and charts.",
            "- `hub-doc xlsx read <file> [--sheet S] [--range A1:F20]`: the computed values as a table.",
            "- `hub-doc xlsx edit <file> --out \"<name>.xlsx\" --set \"Sheet!E3==IF(B3=0,0,D3/B3)\" [--set ...]`: a "
            "recalculated copy that stores the new results; the reply lists changed cells and the errors left "
            "(fix until `errors` is empty). A formula keeps its own `=` after the cell's `=`.",
            "- `hub-doc xlsx render <file> [--sheet S] [--range A1:F20] --out \"<name>.png\"`: a picture of a sheet.",
            "- `hub-doc xlsx csv <file> [--sheet S] --out \"<name>.csv\"`: computed values as CSV for your own "
            "analysis.",
            "- `hub-doc docx read <file>`: the text as Markdown, with tracked changes and their authors.",
            "- `hub-doc docx report <file>`: the formatting report (styles, fonts, sizes, spacing, lists, tracked "
            "changes, inconsistencies). Use it to review a document's formatting.",
            "- `hub-doc docx edit <file> --out \"<name>.docx\" --replace \"old text\" \"new text\" [--replace ...]`: "
            "replaces text and keeps its formatting; it refuses paragraphs with links, fields or tracked changes.",
            "- `hub-doc docx diff <original> <edited>`: the paragraphs that changed.",
        ]
        if self.office:
            lines += [
                "- `hub-doc docx pages <file>`: Microsoft Word saves a PDF and PNG page images, to look at the "
                "layout.",
                f"- `hub-doc docx compare <original> <edited> --author {author} --out \"<name> (changes).docx\"`: a "
                "copy showing your edits as tracked changes.",
                "  These two start Microsoft Word, wait for any other document Word is busy with, and can take up to "
                "3 minutes: give the shell call a time limit of at least 4 minutes (for example a Bash timeout of "
                "240000 ms).",
            ]
        lines.append("- `hub-doc pdf pages <file.pdf>`: PNG page images of a PDF.")
        return "\n".join(lines)


class DocumentToolkit:
    """Finds hub-doc, puts its launcher in the bin folder agents get on their PATH, and hands out each turn's
    `DocumentAccess`."""

    def __init__(self, tools_bin: Path, office=None) -> None:
        self.tools_bin = Path(tools_bin)
        self.office = office  # app.office.OfficeDetector, or None
        self.bin_dir: Path | None = None

    def install(self) -> Path | None:
        """Make hub-doc available; returns the folder to put on agents' PATH (None, with a warning, if it can't)."""
        self.bin_dir = prepare_bin(self.tools_bin)
        return self.bin_dir

    def access(self, outputs: Path, read_roots: tuple[Path, ...] = ()) -> DocumentAccess:
        office = bool(self.office is not None and self.office.found)
        return DocumentAccess(outputs=outputs, bin_dir=self.bin_dir, office=office, read_roots=read_roots)


def prepare_bin(tools_bin: Path) -> Path | None:
    if getattr(sys, "frozen", False):
        # The bundle's hub-doc.exe can't be copied: it needs the _internal folder beside it.
        folder = Path(sys.executable).parent
        if (folder / LAUNCHER).is_file():
            return folder
        log.warning("hub-doc.exe isn't next to %s; agents won't have the document toolkit", sys.executable)
        return None
    source = Path(sys.executable).parent / LAUNCHER
    if not source.is_file():
        log.warning("%s isn't installed (run `uv sync` in backend/); agents won't have the document toolkit", source)
        return None
    target = tools_bin / LAUNCHER
    try:
        tools_bin.mkdir(parents=True, exist_ok=True)
        if not (target.is_file() and filecmp.cmp(source, target, shallow=False)):
            partial = target.with_name(target.name + ".new")
            shutil.copyfile(source, partial)
            os.replace(partial, target)
    except OSError as exc:
        # An agent may be running the old copy right now; it still works.
        if not target.is_file():
            log.warning("Couldn't put hub-doc in %s (%s); agents won't have the document toolkit", tools_bin, exc)
            return None
        log.info("Kept the existing hub-doc in %s (%s)", tools_bin, exc)
    return tools_bin
