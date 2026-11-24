"""The session's outputs folder in the UI: the files agents wrote, with Open and Save as… (`app.outputs`).

Behind the API token like every route (main.py). Save as takes the destination path the user chose in the hub's
own folder picker: the desktop shell has no native save dialog (no Tauri dialog plugin), and the backend already
runs on the user's PC, so the copy is made here.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session

from app.api.attachments import FileOpener, OpenResult
from app.attachments import AttachmentError
from app.db import get_session
from app.models import AgentSession
from app.outputs import OutputFile, OutputsStore

router = APIRouter(prefix="/sessions/{session_id}/outputs", tags=["outputs"])


class OutputRead(BaseModel):
    name: str
    size: int
    modified: float
    path: str


class OutputName(BaseModel):
    name: str


class SaveRequest(BaseModel):
    name: str
    destination: str


class SaveResult(BaseModel):
    path: str


def get_outputs(request: Request) -> OutputsStore:
    return request.app.state.orchestrator.outputs


Outputs = Annotated[OutputsStore, Depends(get_outputs)]
DbSession = Annotated[Session, Depends(get_session)]


def _check_session(db: Session, session_id: str) -> None:
    if db.get(AgentSession, session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")


@router.get("", response_model=list[OutputRead])
def list_outputs(session_id: str, db: DbSession, outputs: Outputs) -> list[OutputFile]:
    _check_session(db, session_id)
    return outputs.list(session_id)


@router.post("/open", response_model=OpenResult)
def open_output(session_id: str, body: OutputName, request: Request, db: DbSession, outputs: Outputs) -> OpenResult:
    """Open the file with its own app on this PC; scripts and code are shown in Explorer instead, never run."""
    _check_session(db, session_id)
    try:
        path, reveal = outputs.open_target(session_id, body.name)
    except AttachmentError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    opener: FileOpener = request.app.state.open_file
    opener(path, reveal)
    return OpenResult(action="revealed" if reveal else "opened")


@router.post("/save", response_model=SaveResult)
def save_output(session_id: str, body: SaveRequest, db: DbSession, outputs: Outputs) -> SaveResult:
    """Copy the file to the path the user chose (Save as…)."""
    _check_session(db, session_id)
    try:
        saved = outputs.save_copy(session_id, body.name, body.destination)
    except AttachmentError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return SaveResult(path=str(saved))
