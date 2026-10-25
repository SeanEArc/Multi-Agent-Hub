import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session, col, select

from app.db import get_session
from app.models import (
    AgentSession,
    EventRead,
    HandoffCreate,
    MessageCreate,
    SessionCreate,
    SessionEvent,
    SessionRead,
    Stint,
    StintDetail,
    StintReason,
    session_read,
)
from app.orchestrator import AgentUnavailable, Orchestrator, SessionBusy, SessionNotFound, StreamMessage

router = APIRouter(prefix="/sessions", tags=["sessions"])


def get_orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


DbSession = Annotated[Session, Depends(get_session)]
Orch = Annotated[Orchestrator, Depends(get_orchestrator)]


def _get_or_404(db: Session, session_id: str) -> AgentSession:
    agent_session = db.get(AgentSession, session_id)
    if agent_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return agent_session


@router.post("", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
def create_session(body: SessionCreate, db: DbSession) -> SessionRead:
    agent_session = AgentSession(
        title=body.title,
        project_path=body.project_path,
        chain=[entry.model_dump(mode="json") for entry in body.chain] if body.chain else None,
        limit_policy=body.limit_policy,
    )
    db.add(agent_session)
    db.add(Stint(session_id=agent_session.id, seq=1, agent=body.agent, model=body.model, reason=StintReason.NEW))
    db.commit()
    db.refresh(agent_session)
    return session_read(db, agent_session)


@router.get("", response_model=list[SessionRead])
def list_sessions(db: DbSession) -> list[SessionRead]:
    sessions = db.exec(select(AgentSession).order_by(col(AgentSession.created_at).desc()))
    return [session_read(db, s) for s in sessions]


@router.get("/{session_id}", response_model=SessionRead)
def get_session_by_id(session_id: str, db: DbSession) -> SessionRead:
    return session_read(db, _get_or_404(db, session_id))


@router.get("/{session_id}/stints/{stint_id}", response_model=StintDetail)
def get_stint(session_id: str, stint_id: int, db: DbSession) -> Stint:
    """One stint with its brief, the prompt the agent started from."""
    stint = db.get(Stint, stint_id)
    if stint is None or stint.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stint not found")
    return stint


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str, db: DbSession, orchestrator: Orch) -> None:
    agent_session = _get_or_404(db, session_id)
    await orchestrator.forget(session_id)
    # The session's events are removed by the database (ON DELETE CASCADE).
    db.delete(agent_session)
    db.commit()


@router.post("/{session_id}/messages", response_model=SessionRead, status_code=status.HTTP_202_ACCEPTED)
async def send_message(session_id: str, body: MessageCreate, db: DbSession, orchestrator: Orch) -> SessionRead:
    """Start the session's task, or reply to it. The turn runs in the background; follow /stream."""
    try:
        await orchestrator.send(session_id, body.text)
    except SessionNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from None
    except SessionBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except AgentUnavailable as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    return session_read(db, _get_or_404(db, session_id))


@router.post("/{session_id}/handoff", response_model=SessionRead, status_code=status.HTTP_202_ACCEPTED)
async def hand_off(session_id: str, body: HandoffCreate, db: DbSession, orchestrator: Orch) -> SessionRead:
    """Move the session to another agent, which starts right away from the hub's brief."""
    try:
        await orchestrator.handoff(session_id, body.agent, body.model, body.note)
    except SessionNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from None
    except SessionBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except AgentUnavailable as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    return session_read(db, _get_or_404(db, session_id))


@router.post("/{session_id}/stop", response_model=SessionRead)
async def stop_session(session_id: str, db: DbSession, orchestrator: Orch) -> SessionRead:
    agent_session = _get_or_404(db, session_id)
    if not await orchestrator.stop(session_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "No turn is running for this session")
    db.refresh(agent_session)
    return session_read(db, agent_session)


@router.get("/{session_id}/events", response_model=list[EventRead])
def list_events(session_id: str, db: DbSession, after_id: int = 0) -> list[SessionEvent]:
    """Events in order. Pass `after_id` to fetch only events newer than the last one seen."""
    _get_or_404(db, session_id)
    query = (
        select(SessionEvent)
        .where(SessionEvent.session_id == session_id, col(SessionEvent.id) > after_id)
        .order_by(col(SessionEvent.id))
    )
    return list(db.exec(query))


@router.get("/{session_id}/stream")
def stream_session(
    session_id: str,
    db: DbSession,
    orchestrator: Orch,
    after_id: int = 0,
    last_event_id: Annotated[int | None, Header()] = None,
) -> StreamingResponse:
    """Server-sent events: `status`, then saved `event`s after `after_id`, then live updates.

    A reconnecting EventSource sends Last-Event-ID, so it resumes where it left off.
    """
    _get_or_404(db, session_id)
    messages = orchestrator.follow(session_id, max(after_id, last_event_id or 0))
    return StreamingResponse(
        _sse(messages),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse(messages: AsyncIterator[StreamMessage]) -> AsyncIterator[str]:
    async for message in messages:
        if message.kind == "ping":
            yield ": ping\n\n"
            continue
        id_line = f"id: {message.id}\n" if message.id is not None else ""
        yield f"{id_line}event: {message.kind}\ndata: {json.dumps(message.data)}\n\n"
