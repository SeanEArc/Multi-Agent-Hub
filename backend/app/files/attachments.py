"""Files the user attaches to a message: which are allowed, where they are kept, and how agents are told about them.

Each attachment lives in its own folder, `<attachments dir>/<session id>/<attachment id>/`, holding the original
(never changed), a text copy `<name>.txt` for PDF, Word, Excel and CSV files when one could be made
(`app.files.extract`), and `.hub-attachment.json` with what the hub knows about it. The session's folder is outside
any repository and is deleted with the session. The `user_message` event keeps the attachments' metadata, so
the transcript, handoff briefs and summaries need nothing else.

Agents get attachments their own way (images and PDFs as content blocks for Claude, `-i` images for Codex,
Ollama's `images` for a vision model); everything else goes by path plus its text copy, through `with_files`.
"""

import json
import logging
import os
import re
import secrets
import shutil
import stat
import time
import uuid
from collections.abc import AsyncIterator, Collection, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from app.files.extract import TEXT_COPY_KINDS, make_text_copy

log = logging.getLogger(__name__)

META_FILE = ".hub-attachment.json"
# Uploads never used by a message are removed at startup once they are this old (the UI discards them itself after a
# failed send; this catches a closed window or a crash in between).
UNSENT_MAX_AGE = 24 * 3600
# Mark of the Web on stored originals: the file came from elsewhere, so Office opens it in Protected View.
ZONE_IDENTIFIER = "[ZoneTransfer]\r\nZoneId=3\r\n"
ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
MAX_NAME = 120
MB = 1024 * 1024

# Size caps per kind, checked while the upload streams in. Images: Claude Code and Codex both downsize large
# images themselves (checked with a 21 MB PNG); 20 MB keeps uploads and base64 content blocks sane. PDF: the
# Claude API's document limit (32 MB, 100 pages). Office files and CSV: far above normal documents, low enough
# that the text copy stays quick. Text and code: a 5 MB source file is already unusual.
SIZE_CAPS = {
    "image": 20 * MB,
    "pdf": 32 * MB,
    "word": 25 * MB,
    "excel": 25 * MB,
    "csv": 10 * MB,
    "text": 5 * MB,
}
KIND_NAMES = {"image": "image", "pdf": "PDF", "word": "Word document", "excel": "Excel workbook", "csv": "CSV file",
              "text": "text file"}
DOCUMENT_TYPES: dict[str, tuple[str, str]] = {
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"),
    ".webp": ("image", "image/webp"),
    ".pdf": ("pdf", "application/pdf"),
    ".docx": ("word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".xlsx": ("excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".xls": ("excel", "application/vnd.ms-excel"),
    ".csv": ("csv", "text/csv"),
}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".env", ".xml", ".html", ".htm", ".css", ".scss", ".less", ".js", ".mjs", ".cjs", ".jsx", ".ts",
    ".tsx", ".py", ".pyi", ".ipynb", ".rs", ".go", ".java", ".kt", ".kts", ".swift", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".cs", ".fs", ".vb", ".rb", ".php", ".pl", ".lua", ".r", ".scala", ".sh", ".bash", ".zsh", ".ps1",
    ".psm1", ".bat", ".cmd", ".sql", ".graphql", ".proto", ".gradle", ".tf", ".hcl", ".vue", ".svelte", ".dart",
    ".ex", ".exs", ".erl", ".hs", ".clj", ".elm", ".tex", ".bib", ".diff", ".patch", ".tsv", ".csproj", ".sln",
}
TEXT_NAMES = {"dockerfile", "makefile", "license", "readme", ".gitignore", ".gitattributes", ".editorconfig"}
# Magic bytes: an upload must be what its extension says.
SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "application/pdf": (b"%PDF-",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (b"PK\x03\x04",),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
    "application/vnd.ms-excel": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}
# Opened with the file's own app by the open route; anything else (code, scripts) is shown in Explorer instead.
OPENABLE_KINDS = {"image", "pdf", "word", "excel", "csv"}
OPENABLE_TEXT = {".txt", ".md", ".log"}
# The local model gets text copies inline, each cut to this much (fit() shortens further when needed).
INLINE_CHARS = 30_000
INLINE_TOTAL = 60_000
# Claude's content blocks, per request (the API's limits are for the whole request, not each file): at most 100 PDF
# pages and 32 MB. Anything past the budget goes by path and text copy. An image is counted at most
# IMAGE_SENT_BYTES, since Claude Code downsizes images before sending (a 21 MB PNG was taken); a PDF is sent as is
# and counted in full, base64 included. Earlier turns' blocks aren't counted: on --resume a second 60-page PDF after
# a first one was accepted (Haiku 5.5, 2026-11-18), as were two 60-page PDFs in one request, so the budget is
# the documented limit, kept on the safe side.
PDF_BLOCK_PAGES = 100
PDF_BLOCK_BYTES = 32 * MB
IMAGE_SENT_BYTES = 5 * MB


class AttachmentError(Exception):
    """An upload or a reference that can't be used; `status` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Attachment:
    id: str
    name: str  # as the user named it
    mime: str
    size: int
    path: str  # the original, absolute
    text_path: str | None  # the text copy beside it, when one was made
    kind: str  # image, pdf, word, excel, csv, text
    pages: int | None = None  # a PDF's page count, when it could be read
    note: str | None = None  # why there is no text copy

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Attachment":
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in fields})


def classify(name: str) -> tuple[str, str]:
    """(kind, mime) for a file name, or AttachmentError (415) for a type that isn't allowed."""
    suffix = Path(name).suffix.lower()
    if suffix in DOCUMENT_TYPES:
        return DOCUMENT_TYPES[suffix]
    if suffix in TEXT_EXTENSIONS or Path(name).name.lower() in TEXT_NAMES:
        return "text", "text/plain"
    raise AttachmentError(
        f"{name}: this file type can't be attached. Allowed: images (PNG, JPEG, GIF, WebP), PDF, Word (.docx), "
        "Excel (.xlsx, .xls), CSV, and text or code files.", 415)


def safe_name(name: str) -> str:
    """A file name safe to store and to pass on a command line: no folders, no characters Windows refuses, and no
    commas (Codex's `-i` splits its value on them)."""
    base = Path(name.replace("\\", "/")).name.strip().strip(".")
    base = re.sub(r'[<>:"/\\|?*,\x00-\x1f]', "_", base)
    stem, suffix = os.path.splitext(base)
    if stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                        *(f"LPT{i}" for i in range(1, 10))}:
        stem = f"_{stem}"
    stem = stem[: MAX_NAME - len(suffix)] or "file"
    return stem + suffix


def check_content(kind: str, mime: str, head: bytes) -> None:
    """Refuse a file whose first bytes don't match its type (a renamed program, for example)."""
    if mime == "image/webp":
        ok = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    elif mime in SIGNATURES:
        ok = any(head.startswith(sig) for sig in SIGNATURES[mime])
    else:  # text, code, CSV: not binary
        ok = b"\x00" not in head or head.startswith((b"\xff\xfe", b"\xfe\xff"))
    if not ok:
        raise AttachmentError(f"The file's contents don't match its type ({KIND_NAMES[kind]}).", 415)


class AttachmentStore:
    """The attachments folder: saving uploads, finding them again, and deleting a session's."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def session_dir(self, session_id: str) -> Path:
        """`<root>/<session id>`, the id as the database stores it (32 hex digits). Session ids are UUIDs; parsing
        keeps a stored value from naming another folder. PR 1's development builds used the dashed form, and a
        session that already has such a folder keeps it."""
        parsed = uuid.UUID(session_id)
        folder = self.root / parsed.hex
        legacy = self.root / str(parsed)
        if not folder.exists() and legacy.is_dir():
            return legacy
        return folder

    async def save(self, session_id: str, name: str, chunks: AsyncIterator[bytes],
                   declared_size: int | None = None) -> Attachment:
        """Stream an upload to disk within its type's size cap, check it, and make its text copy."""
        name = Path((name or "").replace("\\", "/")).name.strip()
        if not name:
            raise AttachmentError("The file needs a name.")
        kind, mime = classify(name)
        cap = SIZE_CAPS[kind]
        if declared_size is not None and declared_size > cap:
            raise AttachmentError(_too_big(name, kind), 413)
        attachment_id = secrets.token_hex(6)
        folder = self.session_dir(session_id) / attachment_id
        # Plain mkdir: a folder from tempfile.mkdtemp is owner-only, which Codex's sandbox can't read.
        folder.mkdir(parents=True, exist_ok=False)
        path = folder / safe_name(name)
        size, head = 0, b""
        try:
            with open(path, "wb") as out:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > cap:
                        raise AttachmentError(_too_big(name, kind), 413)
                    if len(head) < 4096:
                        head += chunk[: 4096 - len(head)]
                    out.write(chunk)
            if size == 0:
                raise AttachmentError(f"{name} is empty.")
            check_content(kind, mime, head)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        attachment = Attachment(id=attachment_id, name=name, mime=mime, size=size, path=str(path.resolve()),
                                text_path=None, kind=kind)
        if kind in TEXT_COPY_KINDS:
            copy = await make_text_copy(kind, path)
            attachment = replace(attachment, text_path=str(copy.path) if copy.path else None, pages=copy.pages,
                                 note=copy.error)
        mark_from_elsewhere(path)
        # Read-only, so an agent (Claude edits files without asking, and the folder is one of its --add-dir
        # folders) can't change the user's original or the text copy by accident. The metadata stays writable.
        for stored in (path, Path(attachment.text_path) if attachment.text_path else None):
            if stored is not None:
                make_read_only(stored)
        self._write_meta(folder, attachment, sent=False)
        return attachment

    def get(self, session_id: str, attachment_id: str) -> Attachment:
        return self._read_meta(session_id, attachment_id)[0]

    def load(self, session_id: str, attachment_ids: Sequence[str]) -> list[Attachment]:
        """The attachments a message names, in order, each once. AttachmentError (400) for an unknown id."""
        seen: dict[str, Attachment] = {}
        for attachment_id in attachment_ids:
            if attachment_id not in seen:
                seen[attachment_id] = self.get(session_id, attachment_id)
        return list(seen.values())

    def mark_sent(self, session_id: str, attachments: Sequence[Attachment]) -> None:
        for attachment in attachments:
            self._write_meta(self.session_dir(session_id) / attachment.id, attachment, sent=True)

    def discard(self, session_id: str, attachment_id: str) -> None:
        """Delete an attachment that was uploaded but never sent (its chip was removed, or the send failed)."""
        _, sent = self._read_meta(session_id, attachment_id)
        if sent:
            raise AttachmentError("This file was already sent in a message and stays with the session.", 409)
        remove_tree(self.session_dir(session_id) / attachment_id)

    def delete_session(self, session_id: str) -> bool:
        """Delete the session's folder. False (and a warning in the log) if something in it couldn't be removed."""
        try:
            folder = self.session_dir(session_id)
        except ValueError:
            return True
        return remove_tree(folder)

    def sweep_unsent(self, max_age: float = UNSENT_MAX_AGE, known_sessions: Collection[str] | None = None) -> int:
        """Remove uploads no message used that are older than `max_age` seconds (and folders a crash left without
        metadata). With `known_sessions` (the ids in the database), an **empty** session folder whose session is gone
        is removed too. Returns how many uploads were removed. Run at startup, so it never raises for a folder it
        can't list or read: that folder is skipped."""
        removed = 0
        cutoff = time.time() - max_age
        for session in _listing(self.root):
            if not session.is_dir():
                continue
            for folder in _listing(session):
                if not (folder.is_dir() and ID_PATTERN.match(folder.name)):
                    continue
                meta = folder / META_FILE
                try:
                    if meta.is_file():
                        data = json.loads(meta.read_text(encoding="utf-8"))
                        # Metadata that isn't the hub's object is left alone (and must not stop the startup).
                        if not isinstance(data, dict) or data.get("sent"):
                            continue
                        age_from = meta.stat().st_mtime
                    else:
                        age_from = folder.stat().st_mtime
                except (OSError, ValueError):
                    continue
                if age_from < cutoff and remove_tree(folder):
                    removed += 1
            if known_sessions is not None and not {session.name, _session_key(session.name)} & set(known_sessions):
                try:
                    # Only succeed when empty: a folder with files is never touched here. An empty outputs folder
                    # (made before each turn) doesn't count as files.
                    if (session / "outputs").is_dir():
                        (session / "outputs").rmdir()
                    session.rmdir()
                except OSError:
                    pass
        if removed:
            log.info("Removed %d unsent attachment(s) older than %d hours", removed, max_age // 3600)
        return removed

    def file(self, session_id: str, attachment_id: str, text: bool = False) -> tuple[Attachment, Path]:
        """An attachment's original (or text copy) on disk, refusing anything outside the session's folder."""
        attachment = self.get(session_id, attachment_id)
        raw = attachment.text_path if text else attachment.path
        if not raw:
            raise AttachmentError("This file has no text copy.", 404)
        path = Path(raw).resolve()
        if not path.is_relative_to(self.session_dir(session_id).resolve()) or not path.is_file():
            raise AttachmentError("File not found.", 404)
        return attachment, path

    def _read_meta(self, session_id: str, attachment_id: str) -> tuple[Attachment, bool]:
        if not ID_PATTERN.match(attachment_id or ""):
            raise AttachmentError(f"Unknown attachment {attachment_id!r}.", 404)
        try:
            meta = json.loads((self.session_dir(session_id) / attachment_id / META_FILE).read_text(encoding="utf-8"))
            attachment = Attachment.from_json(meta)  # TypeError/AttributeError: not the hub's metadata
        except (OSError, ValueError, TypeError, AttributeError):
            raise AttachmentError(f"Unknown attachment {attachment_id!r}.", 404) from None
        return attachment, bool(meta.get("sent"))

    @staticmethod
    def _write_meta(folder: Path, attachment: Attachment, sent: bool) -> None:
        (folder / META_FILE).write_text(json.dumps({**attachment.to_json(), "sent": sent}), encoding="utf-8")


def make_read_only(path: Path) -> None:
    try:
        path.chmod(stat.S_IREAD)
    except OSError:
        log.warning("Couldn't make %s read-only", path)


def mark_from_elsewhere(path: Path) -> None:
    """Write the Mark of the Web (a `Zone.Identifier` stream, Internet zone) so Office and Windows treat the file as
    downloaded: Word and Excel open it in Protected View. NTFS only; elsewhere it is skipped."""
    if os.name != "nt":
        return
    try:
        with open(f"{path}:Zone.Identifier", "w", encoding="ascii", newline="") as stream:
            stream.write(ZONE_IDENTIFIER)
    except OSError:
        pass


def remove_tree(folder: Path) -> bool:
    """Delete a folder of attachments, clearing the read-only flag on the way and retrying once after a moment (a
    viewer or an agent may still hold a file). False, with a warning, if anything is left."""

    def writable_then_retry(function, failed_path, _exc) -> None:
        try:
            os.chmod(failed_path, stat.S_IWRITE | stat.S_IREAD)
            function(failed_path)
        except OSError:
            pass

    for attempt in range(2):
        if not folder.exists():
            return True
        shutil.rmtree(folder, onexc=writable_then_retry)
        if not folder.exists():
            return True
        if attempt == 0:
            time.sleep(0.5)
    log.warning("Couldn't delete everything in %s; a file may still be open", folder)
    return False


def _too_big(name: str, kind: str) -> str:
    return f"{name} is larger than the {SIZE_CAPS[kind] // MB} MB allowed for a {KIND_NAMES[kind]}."


def size_text(size: int) -> str:
    if size >= MB:
        return f"{size / MB:.1f} MB"
    return f"{max(1, round(size / 1024))} KB"


def openable(attachment: Attachment) -> bool:
    """Whether the open route may start the file's own app; scripts and code are only shown in Explorer."""
    return attachment.kind in OPENABLE_KINDS or Path(attachment.path).suffix.lower() in OPENABLE_TEXT


def openable_name(name: str) -> bool:
    """The same rule for any file by its name (an agent's output): documents, images and plain text open, anything
    else (scripts, code, `.xlsm` with macros) is only shown in Explorer."""
    try:
        kind, _ = classify(name)
    except AttachmentError:
        return False
    return kind in OPENABLE_KINDS or Path(name).suffix.lower() in OPENABLE_TEXT


def claude_block_ok(attachment: Attachment) -> bool:
    """Whether Claude could take the attachment as a content block at all: any allowed image, and a PDF within the
    API's document limits (its page count must be known)."""
    if attachment.is_image:
        return True
    return (attachment.kind == "pdf" and attachment.pages is not None and attachment.pages <= PDF_BLOCK_PAGES
            and _sent_bytes(attachment) <= PDF_BLOCK_BYTES)


def claude_blocks(attachments: Sequence[Attachment]) -> list[Attachment]:
    """The attachments that go as content blocks in one request, in order, while the request's running totals stay
    within `PDF_BLOCK_PAGES` pages and `PDF_BLOCK_BYTES`; the rest go by path and text copy."""
    chosen, pages, size = [], 0, 0
    for attachment in attachments:
        if not claude_block_ok(attachment):
            continue
        more_pages = (attachment.pages or 0) if attachment.kind == "pdf" else 0
        more_bytes = _sent_bytes(attachment)
        if pages + more_pages > PDF_BLOCK_PAGES or size + more_bytes > PDF_BLOCK_BYTES:
            continue
        chosen.append(attachment)
        pages += more_pages
        size += more_bytes
    return chosen


def _sent_bytes(attachment: Attachment) -> int:
    encoded = (attachment.size + 2) // 3 * 4  # base64
    return min(encoded, IMAGE_SENT_BYTES) if attachment.is_image else encoded


def with_files(prompt: str, attachments: Sequence[Attachment], attached: Collection[str] = (),
               inline_text: bool = False, note: str | None = None) -> str:
    """The prompt followed by a list of the attachments: each one's path and text copy, and whether it already
    came with the message in the agent's own form (`attached`, by id). With `inline_text` (the local model, which
    can't open a PDF itself) the text copies, and text files themselves, follow in full, within a size budget."""
    if not attachments:
        return prompt
    folder = str(Path(attachments[0].path).parent.parent)
    lines = [
        "## Attached files",
        "",
        f"The user attached {'this file' if len(attachments) == 1 else f'these {len(attachments)} files'}. They are "
        f"stored in {folder}: read them from there, and never change them (write anything new to the session's "
        "outputs folder, or elsewhere).",
    ]
    if note:
        lines.append(note)
    lines.append("")
    for attachment in attachments:
        details = KIND_NAMES[attachment.kind]
        if attachment.pages:
            details += f", {attachment.pages} page{'s' if attachment.pages != 1 else ''}"
        details += f", {size_text(attachment.size)}"
        line = f"- {attachment.name} ({details}): `{attachment.path}`"
        if attachment.id in attached:
            line += " (also attached to this message)"
        if attachment.text_path:
            line += f"; text copy: `{attachment.text_path}`"
        elif attachment.kind in TEXT_COPY_KINDS:
            line += " (no text copy could be made" + (f": {attachment.note})" if attachment.note else ")")
        lines.append(line)
    text = "\n".join(lines)
    if inline_text:
        text += _inline_texts(attachments)
    return f"{prompt}\n\n{text}" if prompt.strip() else text


def _inline_texts(attachments: Sequence[Attachment]) -> str:
    parts, budget = [], INLINE_TOTAL
    for attachment in attachments:
        source = attachment.text_path or (attachment.path if attachment.kind == "text" else None)
        if not source or budget <= 0:
            continue
        try:
            content = Path(source).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        limit = min(INLINE_CHARS, budget)
        if len(content) > limit:
            content = content[:limit] + f"\n[… {len(content) - limit:,} more characters; read the file for the rest]"
        budget -= len(content)
        label = "Text of" if attachment.text_path else "Contents of"
        # A fence longer than any run of backticks in the file, so a Markdown file with code blocks stays whole.
        fence = "`" * max(3, _longest_backtick_run(content) + 1)
        parts.append(f"\n\n### {label} {attachment.name}\n\n{fence}\n{content.rstrip()}\n{fence}")
    return "".join(parts)


def _longest_backtick_run(text: str) -> int:
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


def _session_key(name: str) -> str:
    """A session folder's name as the database's id (a dashed folder from PR 1's builds included)."""
    try:
        return uuid.UUID(name).hex
    except ValueError:
        return name


def _listing(folder: Path) -> list[Path]:
    """A folder's entries, or none (with a warning) when it doesn't exist or can't be listed."""
    try:
        return list(folder.iterdir()) if folder.is_dir() else []
    except OSError as exc:
        log.warning("Skipped %s while cleaning up attachments: %s", folder, exc)
        return []

