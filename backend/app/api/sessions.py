import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session, col, select

from app.db import get_session
from app.handoff import git_state
from app.models import (
    AgentSession,
    EventRead,
    GitSummary,
    HandoffCreate,
    MessageCreate,
    SessionCreate,
    SessionEvent,
    SessionRead,
    SessionSummary,
    Stint,
    StintDetail,
    StintReason,
    SummaryRead,
    Worktree,
    WorktreeRemove,
    WorktreeStatus,
    latest_summary,
    session_read,
    session_worktree,
    summary_read,
    utcnow,
)
from app.orchestrator import (
    AgentUnavailable,
    Orchestrator,
    SessionBusy,
    SessionNotFound,
    StreamMessage,
    SummaryFailed,
    session_workdir,
)
from app.worktrees import WorktreeError, commit_all, create_worktree, remove_worktree, repo_info, worktree_changes

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
async def create_session(body: SessionCreate, db: DbSession, request: Request) -> SessionRead:
    """Create a session. In a git repository it gets its own worktree and branch unless `use_worktree` is false."""
    agent_session = AgentSession(
        title=body.title,
        project_path=body.project_path,
        chain=[entry.model_dump(mode="json") for entry in body.chain] if body.chain else None,
        limit_policy=body.limit_policy,
    )
    repo = await repo_info(Path(body.project_path)) if body.use_worktree else None
    if repo is not None:
        try:
            created = await create_worktree(request.app.state.worktrees_dir, repo, agent_session.id, body.title)
        except WorktreeError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not create a worktree: {exc}") from None
    db.add(agent_session)
    db.add(Stint(session_id=agent_session.id, seq=1, agent=body.agent, model=body.model, reason=StintReason.NEW))
    if repo is not None:
        db.add(Worktree(session_id=agent_session.id, repo_path=str(repo.root), path=str(created.path),
                        subdir=repo.subdir, branch=created.branch, base_commit=created.base_commit))
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
    """Delete the session and its history. Its worktree is removed too (branch kept), but only if it is clean."""
    agent_session = _get_or_404(db, session_id)
    await orchestrator.forget(session_id)
    worktree = session_worktree(db, session_id)
    if worktree is not None and worktree.status is WorktreeStatus.ACTIVE:
        await _remove_worktree(db, agent_session, worktree, WorktreeRemove())
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


@router.get("/{session_id}/summary", response_model=SessionSummary)
async def get_summary(session_id: str, db: DbSession, orchestrator: Orch) -> SessionSummary:
    """Git state for the hub's live summary, the latest agent-written summary, and whether one is being written."""
    agent_session = _get_or_404(db, session_id)
    worktree = session_worktree(db, session_id)
    try:
        workdir = session_workdir(db, agent_session)
    except AgentUnavailable:
        git = None  # the worktree was removed
    else:
        git = await git_state(workdir, base=worktree.base_commit if worktree else None)
    return SessionSummary(
        git=GitSummary(status=git.status, diff_stat=git.diff_stat) if git else None,
        summary=latest_summary(db, session_id),
        summarizing=orchestrator.is_summarizing(session_id),
    )


@router.post("/{session_id}/summary", response_model=SummaryRead, status_code=status.HTTP_201_CREATED)
async def write_summary(session_id: str, db: DbSession, orchestrator: Orch) -> SummaryRead:
    """Have the current agent (or the next in the chain) write a summary. Waits until it is written."""
    try:
        summary = await orchestrator.summarize(session_id)
    except SessionNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from None
    except SessionBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except AgentUnavailable as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    except SummaryFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
    return summary_read(db, summary)


@router.post("/{session_id}/stop", response_model=SessionRead)
async def stop_session(session_id: str, db: DbSession, orchestrator: Orch) -> SessionRead:
    agent_session = _get_or_404(db, session_id)
    if not await orchestrator.stop(session_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "No turn is running for this session")
    db.refresh(agent_session)
    return session_read(db, agent_session)


@router.post("/{session_id}/worktree/remove", response_model=SessionRead)
async def remove_session_worktree(
    session_id: str, body: WorktreeRemove, db: DbSession, orchestrator: Orch
) -> SessionRead:
    """Delete the session's worktree folder. The branch is kept for merging unless it has no commits.

    Uncommitted changes are refused (409, with the changed files) unless `commit` or `discard` says what to do.
    """
    agent_session = _get_or_404(db, session_id)
    worktree = session_worktree(db, session_id)
    if worktree is None or worktree.status is not WorktreeStatus.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, "This session has no worktree to remove")
    if orchestrator.is_running(session_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "A turn is running for this session; stop it first")
    await _remove_worktree(db, agent_session, worktree, body)
    await orchestrator.forget(session_id)
    db.refresh(agent_session)
    return session_read(db, agent_session)


async def _remove_worktree(db: Session, agent_session: AgentSession, worktree: Worktree, body: WorktreeRemove) -> None:
    path = Path(worktree.path)
    try:
        changes = await worktree_changes(path) if path.exists() else []
        if changes and body.commit:
            await commit_all(path, f"{agent_session.title}\n\nWork from a Multi-Agent Hub session.")
        elif changes and not body.discard:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"message": "The worktree has uncommitted changes. Commit or discard them first.", "changes": changes},
            )
        await remove_worktree(Path(worktree.repo_path), path, worktree.branch, worktree.base_commit,
                              force=body.discard)
    except WorktreeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Could not remove the worktree: {exc}") from None
    worktree.status = WorktreeStatus.REMOVED
    worktree.removed_at = utcnow()
    db.add(worktree)
    db.commit()


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
