"""Runs agent turns in the background, saves their events and keeps session status current.

One orchestrator serves the whole app. Each session has at most one running turn.
Listeners (the UI's live stream) subscribe per session and receive every saved
event and status change.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.adapters.base import AgentAdapter, AgentEvent, EventType
from app.adapters.claude import ClaudeAdapter, ClaudeCliNotFound
from app.adapters.codex import CodexAdapter, CodexCliNotFound, find_codex
from app.config import get_settings
from app.models import AgentKind, AgentSession, EventRead, SessionEvent, SessionStatus

log = logging.getLogger(__name__)

AdapterFactory = Callable[[AgentSession], AgentAdapter]

# The event that ends a turn decides where the session rests afterwards.
RESTING_STATUS = {
    EventType.QUESTION: SessionStatus.WAITING_FOR_INPUT,
    EventType.COMPLETED: SessionStatus.COMPLETED,
    EventType.LIMIT_REACHED: SessionStatus.FAILED,  # phase 3: hand off instead
    EventType.ERROR: SessionStatus.FAILED,
}
STOP_TIMEOUT = 10.0


class SessionNotFound(LookupError):
    pass


class SessionBusy(RuntimeError):
    pass


class AgentUnavailable(RuntimeError):
    pass


@dataclass
class StreamMessage:
    kind: str  # "status", "event" or "ping"
    data: dict[str, Any]
    id: int | None = None


def default_adapter_factory(session: AgentSession) -> AgentAdapter:
    settings = get_settings()
    try:
        match session.agent:
            case AgentKind.CLAUDE:
                return ClaudeAdapter(
                    Path(session.project_path),
                    model=session.model,
                    native_session_id=session.native_session_id,
                    permission_mode=settings.claude_permission_mode,
                )
            case AgentKind.CODEX:
                return CodexAdapter(
                    Path(session.project_path),
                    model=session.model,
                    native_session_id=session.native_session_id,
                    sandbox=settings.codex_sandbox,
                    command=[find_codex(settings.codex_path)],
                )
    except (ClaudeCliNotFound, CodexCliNotFound) as exc:
        raise AgentUnavailable(str(exc)) from exc
    raise AgentUnavailable(f"The {session.agent} agent is not supported yet")


class Orchestrator:
    def __init__(self, engine: Engine, adapter_factory: AdapterFactory = default_adapter_factory) -> None:
        self._engine = engine
        self._adapter_factory = adapter_factory
        self._adapters: dict[str, AgentAdapter] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._listeners: dict[str, set[asyncio.Queue[StreamMessage]]] = defaultdict(set)

    def is_running(self, session_id: str) -> bool:
        return session_id in self._tasks

    def recover_interrupted(self) -> None:
        """Mark turns that were running when the backend last stopped as failed."""
        with Session(self._engine) as db:
            stuck = db.exec(
                select(AgentSession).where(
                    col(AgentSession.status).in_([SessionStatus.WORKING, SessionStatus.HANDING_OFF])
                )
            ).all()
        for agent_session in stuck:
            self._save_event(
                agent_session.id,
                agent_session.agent,
                AgentEvent(EventType.ERROR, {"message": "Interrupted: the backend stopped during this turn"}),
            )
            self._set_status(agent_session.id, SessionStatus.FAILED)

    async def send(self, session_id: str, text: str) -> AgentSession:
        """Start the task, or reply to it if it already started. Returns once the turn is running."""
        if self.is_running(session_id):
            raise SessionBusy("A turn is already running for this session")

        with Session(self._engine) as db:
            agent_session = db.get(AgentSession, session_id)
            if agent_session is None:
                raise SessionNotFound(session_id)
            adapter = self._adapters.get(session_id) or self._adapter_factory(agent_session)
            self._adapters[session_id] = adapter

            if adapter.native_session_id is None:
                events = adapter.start_task(text)
            else:
                events = adapter.send_reply(text)
            agent_session.native_session_id = adapter.native_session_id
            agent_session.status = SessionStatus.WORKING
            db.add(agent_session)
            db.commit()
            db.refresh(agent_session)

        self._save_event(session_id, agent_session.agent, AgentEvent(EventType.USER_MESSAGE, {"text": text}))
        self._publish(session_id, StreamMessage("status", {"status": SessionStatus.WORKING}))
        self._tasks[session_id] = asyncio.create_task(self._consume(session_id, agent_session.agent, events))
        return agent_session

    async def stop(self, session_id: str) -> bool:
        """Stop the running turn and wait until its final status is saved. False if none was running."""
        task = self._tasks.get(session_id)
        if task is None:
            return False
        await self._adapters[session_id].stop()
        try:
            await asyncio.wait_for(asyncio.shield(task), STOP_TIMEOUT)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return True

    async def forget(self, session_id: str) -> None:
        """Stop any running turn and drop the session's adapter (before deleting the session)."""
        await self.stop(session_id)
        self._adapters.pop(session_id, None)

    async def shutdown(self) -> None:
        for session_id in list(self._tasks):
            await self.stop(session_id)

    async def follow(
        self, session_id: str, after_id: int = 0, heartbeat: float = 15.0
    ) -> AsyncGenerator[StreamMessage]:
        """Current status, saved events after `after_id`, then live updates until the caller stops."""
        queue: asyncio.Queue[StreamMessage] = asyncio.Queue()
        # Subscribe before reading the backlog so nothing saved in between is missed.
        self._listeners[session_id].add(queue)
        try:
            with Session(self._engine) as db:
                agent_session = db.get(AgentSession, session_id)
                if agent_session is None:
                    return
                backlog = db.exec(
                    select(SessionEvent)
                    .where(SessionEvent.session_id == session_id, col(SessionEvent.id) > after_id)
                    .order_by(col(SessionEvent.id))
                ).all()
                current_status = agent_session.status

            yield StreamMessage("status", {"status": current_status})
            last_id = after_id
            for row in backlog:
                yield _event_message(row)
                last_id = row.id

            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), heartbeat)
                except TimeoutError:
                    yield StreamMessage("ping", {})
                    continue
                if message.kind == "event" and message.id is not None and message.id <= last_id:
                    continue  # already sent from the backlog
                yield message
        finally:
            self._listeners[session_id].discard(queue)
            if not self._listeners[session_id]:
                del self._listeners[session_id]

    async def _consume(self, session_id: str, agent: AgentKind, events: AsyncGenerator[AgentEvent]) -> None:
        status = SessionStatus.FAILED  # if the turn ends without saying how
        try:
            async with aclosing(events):
                async for event in events:
                    if event.type is EventType.ERROR and event.data.get("stopped"):
                        status = SessionStatus.READY
                    else:
                        status = RESTING_STATUS.get(event.type, status)
                    if event.type is EventType.STARTED:
                        # Codex only reports its thread id once the turn starts; save it so replies resume it.
                        self._save_native_session_id(session_id, event.data.get("session_id"))
                    self._save_event(session_id, agent, event)
        except Exception as exc:
            log.exception("Turn failed for session %s", session_id)
            self._save_event(session_id, agent, AgentEvent(EventType.ERROR, {"message": f"Agent run failed: {exc}"}))
            status = SessionStatus.FAILED
        finally:
            self._tasks.pop(session_id, None)
            self._set_status(session_id, status)

    # SQLite writes are small and fast in WAL mode, so they run inline on the event loop.
    def _save_event(self, session_id: str, agent: AgentKind, event: AgentEvent) -> None:
        with Session(self._engine) as db:
            row = SessionEvent(session_id=session_id, agent=agent, type=event.type, payload=event.data, raw=event.raw)
            db.add(row)
            db.commit()
            db.refresh(row)
        self._publish(session_id, _event_message(row))

    def _save_native_session_id(self, session_id: str, native_id: str | None) -> None:
        if not native_id:
            return
        with Session(self._engine) as db:
            agent_session = db.get(AgentSession, session_id)
            if agent_session is None or agent_session.native_session_id == native_id:
                return
            agent_session.native_session_id = native_id
            db.add(agent_session)
            db.commit()

    def _set_status(self, session_id: str, status: SessionStatus) -> None:
        with Session(self._engine) as db:
            agent_session = db.get(AgentSession, session_id)
            if agent_session is None:
                return
            agent_session.status = status
            db.add(agent_session)
            db.commit()
        self._publish(session_id, StreamMessage("status", {"status": status}))

    def _publish(self, session_id: str, message: StreamMessage) -> None:
        for queue in self._listeners.get(session_id, ()):
            queue.put_nowait(message)


def _event_message(row: SessionEvent) -> StreamMessage:
    return StreamMessage("event", EventRead.model_validate(row).model_dump(mode="json"), id=row.id)
