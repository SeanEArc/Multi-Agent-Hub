"""Uploading, showing and opening files attached to a session's messages (`app.attachments`).

The upload is the raw file as the request body (the name in `?name=`), so it streams to disk within its type's
cap with no multipart parser. Every route is behind the API token (main.py) and only reaches files inside the
session's own attachments folder.
"""

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session

from app.attachments import Attachment, AttachmentError, AttachmentStore, openable
from app.db import get_session
from app.models import AgentSession

router = APIRouter(prefix="/sessions/{session_id}/attachments", tags=["attachments"])

# Opens a file with its own app (`reveal`: shows it in Explorer instead). Tests replace it.
FileOpener = Callable[[Path, bool], None]


class AttachmentRead(BaseModel):
    id: str
    name: str
    mime: str
    size: int
    path: str
    text_path: str | None
    kind: str
    pages: int | None = None
    note: str | None = None


class OpenResult(BaseModel):
    # "opened" with the file's own app, or "revealed" in Explorer (code and scripts are never run).
    action: str


def get_store(request: Request) -> AttachmentStore:
    return request.app.state.orchestrator.attachments


Store = Annotated[AttachmentStore, Depends(get_store)]
DbSession = Annotated[Session, Depends(get_session)]


def default_open_file(path: Path, reveal: bool) -> None:
    """The backend runs on the user's PC (loopback only), so it can hand the file to Windows."""
    if os.name != "nt":
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Opening files is only supported on Windows.")
    if reveal:
        subprocess.Popen(["explorer", f"/select,{path}"])  # noqa: S603 - fixed program, path from the store
    else:
        os.startfile(path)  # type: ignore[attr-defined]


def _check_session(db: Session, session_id: str) -> None:
    if db.get(AgentSession, session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")


def _http(exc: AttachmentError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


@router.post("", response_model=AttachmentRead, status_code=status.HTTP_201_CREATED)
async def upload(session_id: str, name: str, request: Request, db: DbSession, store: Store) -> Attachment:
    """Store one file for the session's next message. Answers once its text copy (if any) is made."""
    _check_session(db, session_id)
    db.close()  # the upload and text copy can take a while; don't hold a connection
    length = request.headers.get("content-length")
    try:
        return await store.save(session_id, name, request.stream(),
                                declared_size=int(length) if length and length.isdigit() else None)
    except AttachmentError as exc:
        raise _http(exc) from None


@router.get("/{attachment_id}", response_model=AttachmentRead)
def get_attachment(session_id: str, attachment_id: str, store: Store) -> Attachment:
    try:
        return store.get(session_id, attachment_id)
    except AttachmentError as exc:
        raise _http(exc) from None


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
def discard(session_id: str, attachment_id: str, store: Store) -> None:
    """Delete an upload that was never sent. A sent file stays with the session (409)."""
    try:
        store.discard(session_id, attachment_id)
    except AttachmentError as exc:
        raise _http(exc) from None


@router.get("/{attachment_id}/file")
def get_file(session_id: str, attachment_id: str, store: Store, text: bool = False) -> FileResponse:
    """The original (or with `text=true` its text copy). Only images and PDFs are served as themselves; anything
    else as plain text or a download, so nothing in a file can run as a page."""
    try:
        attachment, path = store.file(session_id, attachment_id, text=text)
    except AttachmentError as exc:
        raise _http(exc) from None
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"}
    if text or attachment.kind in ("text", "csv"):
        return FileResponse(path, media_type="text/plain; charset=utf-8", headers=headers,
                            filename=path.name, content_disposition_type="inline")
    if attachment.kind in ("image", "pdf"):
        return FileResponse(path, media_type=attachment.mime, headers=headers, filename=attachment.name,
                            content_disposition_type="inline")
    return FileResponse(path, media_type="application/octet-stream", headers=headers, filename=attachment.name)


@router.post("/{attachment_id}/open", response_model=OpenResult)
def open_file(session_id: str, attachment_id: str, request: Request, store: Store) -> OpenResult:
    """Open the original with its own app on this PC. Code and scripts are shown in Explorer instead, never run."""
    try:
        attachment, path = store.file(session_id, attachment_id)
    except AttachmentError as exc:
        raise _http(exc) from None
    reveal = not openable(attachment)
    opener: FileOpener = request.app.state.open_file
    opener(path, reveal)
    return OpenResult(action="revealed" if reveal else "opened")
