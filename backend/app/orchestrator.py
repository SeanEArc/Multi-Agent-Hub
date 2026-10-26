"""Runs agent turns in the background, saves their events and keeps session status current.

One orchestrator serves the whole app. A session's work is a sequence of stints, one
agent each; phase 3 runs one turn per session at a time, always on its latest stint.
When a turn ends in a usage limit, the next agent in the session's chain takes over
with a brief built by the hub (see app.handoff), or the user is asked first.
Listeners (the UI's live stream) subscribe per session and receive every saved
event and status change.
"""

import asyncio
import logging
import time
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
from app.handoff import build_brief, git_state, next_in_chain
from app.models import (
    AgentKind,
    AgentSession,
    ChainEntry,
    EventRead,
    LimitPolicy,
    SessionEvent,
    SessionStatus,
    Stint,
    StintReason,
    WorktreeStatus,
    current_stint,
    load_hub_settings,
    session_worktree,
    utcnow,
)

log = logging.getLogger(__name__)

# Builds the adapter for a stint, running in `cwd` (the session's worktree or project folder).
AdapterFactory = Callable[[AgentSession, Stint, Path], AgentAdapter]

# The event that ends a turn decides where the session rests afterwards.
RESTING_STATUS = {
    EventType.QUESTION: SessionStatus.WAITING_FOR_INPUT,
    EventType.COMPLETED: SessionStatus.COMPLETED,
    EventType.LIMIT_REACHED: SessionStatus.FAILED,  # unless the session hands off
    EventType.ERROR: SessionStatus.FAILED,
}
STOP_TIMEOUT = 10.0


class SessionNotFound(LookupError):
    pass


class SessionBusy(RuntimeError):
    pass


class AgentUnavailable(RuntimeError):
    pass


def session_workdir(db: Session, agent_session: AgentSession) -> Path:
    """Where the session's agents run: its worktree, or the project folder when it has none."""
    worktree = session_worktree(db, agent_session.id)
    if worktree is None:
        return Path(agent_session.project_path)
    if worktree.status is WorktreeStatus.REMOVED:
        raise AgentUnavailable(
            f"This session's worktree was removed. Its work is on branch {worktree.branch} in {worktree.repo_path}."
        )
    return Path(worktree.path) / worktree.subdir


@dataclass
class StreamMessage:
    kind: str  # "status", "event" or "ping"
    data: dict[str, Any]
    id: int | None = None


def default_adapter_factory(session: AgentSession, stint: Stint, cwd: Path) -> AgentAdapter:
    settings = get_settings()
    try:
        match stint.agent:
            case AgentKind.CLAUDE:
                return ClaudeAdapter(
                    cwd,
                    model=stint.model,
                    native_session_id=stint.native_session_id,
                    permission_mode=settings.claude_permission_mode,
                )
            case AgentKind.CODEX:
                return CodexAdapter(
                    cwd,
                    model=stint.model,
                    native_session_id=stint.native_session_id,
                    sandbox=settings.codex_sandbox,
                    command=[find_codex(settings.codex_path)],
                )
    except (ClaudeCliNotFound, CodexCliNotFound) as exc:
        raise AgentUnavailable(str(exc)) from exc
    raise AgentUnavailable(f"The {stint.agent} agent is not supported yet")


class Orchestrator:
    def __init__(
        self,
        engine: Engine,
        adapter_factory: AdapterFactory = default_adapter_factory,
        simulate_limit: set[AgentKind] | None = None,
    ) -> None:
        self._engine = engine
        self._adapter_factory = adapter_factory
        # HUB_SIMULATE_LIMIT: these agents' turns end in a usage limit (development only).
        self.simulate_limit = set(simulate_limit or ())
        if self.simulate_limit:
            log.warning("HUB_SIMULATE_LIMIT is on: turns by %s end as if the usage limit was reached",
                        ", ".join(sorted(self.simulate_limit)))
        # The adapter of each session's current stint; replaced on handoff.
        self._adapters: dict[str, AgentAdapter] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Sessions between two agents: the brief is being built, no turn is running.
        self._handing_off: set[str] = set()
        self._listeners: dict[str, set[asyncio.Queue[StreamMessage]]] = defaultdict(set)

    def is_running(self, session_id: str) -> bool:
        return session_id in self._tasks or session_id in self._handing_off

    def recover_interrupted(self) -> None:
        """Mark turns that were running when the backend last stopped as failed."""
        with Session(self._engine) as db:
            stuck = db.exec(
                select(AgentSession).where(
                    col(AgentSession.status).in_([SessionStatus.WORKING, SessionStatus.HANDING_OFF])
                )
            ).all()
            stints = {s.id: current_stint(db, s.id) for s in stuck}
        for session_id, stint in stints.items():
            self._save_event(
                session_id,
                stint,
                AgentEvent(EventType.ERROR, {"message": "Interrupted: the backend stopped during this turn"}),
            )
            self._set_status(session_id, SessionStatus.FAILED)

    async def send(self, session_id: str, text: str) -> None:
        """Start the task, or reply to it if it already started. Returns once the turn is running."""
        if self.is_running(session_id):
            raise SessionBusy("A turn is already running for this session")

        with Session(self._engine) as db:
            agent_session = db.get(AgentSession, session_id)
            if agent_session is None:
                raise SessionNotFound(session_id)
            stint = current_stint(db, session_id)
            limit_known = _limit_already_reached(db, stint)
            if not limit_known:
                adapter = self._adapters.get(session_id) or self._adapter_factory(
                    agent_session, stint, session_workdir(db, agent_session)
                )

        self._save_event(session_id, stint, AgentEvent(EventType.USER_MESSAGE, {"text": text}))
        if limit_known:
            # Codex reports a reached limit only after a turn; don't spend a turn finding out again.
            self._save_event(session_id, stint, AgentEvent(EventType.LIMIT_REACHED, {
                "message": "The usage limit was already reached, so this message was not sent to this agent.",
            }))
            await self._after_limit(session_id)
            return

        self._adapters[session_id] = adapter
        events = adapter.start_task(text) if adapter.native_session_id is None else adapter.send_reply(text)
        self._start_turn(session_id, stint, adapter, events)

    async def handoff(self, session_id: str, agent: AgentKind, model: str | None, note: str | None = None) -> None:
        """Move the session to another agent now (the hand-off button). Returns once its turn is running."""
        if self.is_running(session_id):
            raise SessionBusy("A turn is running for this session; stop it before handing off")
        with Session(self._engine) as db:
            if db.get(AgentSession, session_id) is None:
                raise SessionNotFound(session_id)
        await self._handoff(session_id, ChainEntry(agent=agent, model=model), StintReason.MANUAL, note)

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

    def _start_turn(
        self, session_id: str, stint: Stint, adapter: AgentAdapter, events: AsyncGenerator[AgentEvent]
    ) -> None:
        with Session(self._engine) as db:
            # Claude's id is chosen when the turn starts; Codex's arrives later in its STARTED event.
            row = db.get(Stint, stint.id)
            row.native_session_id = adapter.native_session_id or row.native_session_id
            agent_session = db.get(AgentSession, session_id)
            agent_session.status = SessionStatus.WORKING
            db.add_all([row, agent_session])
            db.commit()
        self._publish(session_id, StreamMessage("status", {"status": SessionStatus.WORKING}))
        if stint.agent in self.simulate_limit:
            events = _simulated_limit(events)
        self._tasks[session_id] = asyncio.create_task(self._consume(session_id, stint, events))

    async def _consume(self, session_id: str, stint: Stint, events: AsyncGenerator[AgentEvent]) -> None:
        status = SessionStatus.FAILED  # if the turn ends without saying how
        limit_reached = False
        hand_off = False
        try:
            async with aclosing(events):
                async for event in events:
                    if event.type is EventType.ERROR and event.data.get("stopped"):
                        status = SessionStatus.READY
                    else:
                        status = RESTING_STATUS.get(event.type, status)
                    if event.type is EventType.LIMIT_REACHED:
                        limit_reached = True
                    if event.type is EventType.STARTED:
                        # Codex only reports its thread id once the turn starts; save it so replies resume it.
                        self._save_native_session_id(stint.id, event.data.get("session_id"))
                    self._save_event(session_id, stint, event)
            hand_off = limit_reached and status is SessionStatus.FAILED
        except Exception as exc:
            log.exception("Turn failed for session %s", session_id)
            self._save_event(session_id, stint, AgentEvent(EventType.ERROR, {"message": f"Agent run failed: {exc}"}))
            status = SessionStatus.FAILED
        finally:
            self._tasks.pop(session_id, None)
            if not hand_off:
                self._set_status(session_id, status)
        if hand_off:
            await self._after_limit(session_id)

    async def _after_limit(self, session_id: str) -> None:
        """The current agent is out of usage: hand off to the next agent in the chain, or ask first."""
        with Session(self._engine) as db:
            agent_session = db.get(AgentSession, session_id)
            if agent_session is None:
                return
            stint = current_stint(db, session_id)
            defaults = load_hub_settings(db)
            chain = [ChainEntry.model_validate(e) for e in agent_session.chain] if agent_session.chain else defaults.default_chain
            policy = agent_session.limit_policy or defaults.limit_policy
            limited = _limited_agents(db, session_id)
            candidate = next(
                (e for e in next_in_chain(chain, stint.agent) if e.agent not in limited and self._can_run(agent_session, e)),
                None,
            )

        if candidate is None:
            self._save_event(session_id, stint, AgentEvent(EventType.ERROR, {
                "message": "Usage limit reached and no other agent in the handoff chain is available.",
            }))
            self._set_status(session_id, SessionStatus.FAILED)
        elif policy is LimitPolicy.ASK:
            self._save_event(session_id, stint, AgentEvent(EventType.HANDOFF_OFFER, candidate.model_dump(mode="json")))
            self._set_status(session_id, SessionStatus.WAITING_FOR_INPUT)
        else:
            try:
                await self._handoff(session_id, candidate, StintReason.LIMIT)
            except Exception:
                log.exception("Automatic handoff failed for session %s", session_id)  # _handoff saved the error

    def _can_run(self, agent_session: AgentSession, entry: ChainEntry) -> bool:
        try:
            stint = Stint(session_id=agent_session.id, seq=0, agent=entry.agent, model=entry.model,
                          reason=StintReason.LIMIT)
            self._adapter_factory(agent_session, stint, Path(agent_session.project_path))
        except AgentUnavailable:
            return False
        return True

    async def _handoff(self, session_id: str, target: ChainEntry, reason: StintReason, note: str | None = None) -> None:
        """End the current stint, start one for `target` and run its first turn on the brief."""
        self._handing_off.add(session_id)
        try:
            with Session(self._engine) as db:
                agent_session = db.get(AgentSession, session_id)
                previous = current_stint(db, session_id)
                events = db.exec(
                    select(SessionEvent).where(SessionEvent.session_id == session_id).order_by(col(SessionEvent.id))
                ).all()
                workdir = session_workdir(db, agent_session)
                worktree = session_worktree(db, session_id)
            stint = Stint(session_id=session_id, seq=previous.seq + 1, agent=target.agent, model=target.model,
                          reason=reason)
            adapter = self._adapter_factory(agent_session, stint, workdir)  # AgentUnavailable before anything changes

            self._set_status(session_id, SessionStatus.HANDING_OFF)
            try:
                git = await git_state(workdir, base=worktree.base_commit if worktree else None)
                stint.brief = build_brief(events, previous.id, previous.agent, target.agent, reason, git, note)
                with Session(self._engine) as db:
                    ended = db.get(Stint, previous.id)
                    ended.ended_at = utcnow()
                    db.add_all([ended, stint])
                    db.commit()
                    db.refresh(stint)
                self._adapters[session_id] = adapter
                self._save_event(session_id, stint, AgentEvent(EventType.HANDOFF, {
                    "from_agent": previous.agent,
                    "to_agent": target.agent,
                    "model": target.model,
                    "reason": reason,
                    "from_stint_id": previous.id,
                }))
                if note and note.strip():
                    self._save_event(session_id, stint, AgentEvent(EventType.USER_MESSAGE, {"text": note.strip()}))
                self._start_turn(session_id, stint, adapter, adapter.start_task(stint.brief))
            except Exception as exc:
                log.exception("Handoff failed for session %s", session_id)
                self._save_event(session_id, previous, AgentEvent(EventType.ERROR, {"message": f"Handoff failed: {exc}"}))
                self._set_status(session_id, SessionStatus.FAILED)
                raise
        finally:
            self._handing_off.discard(session_id)

    # SQLite writes are small and fast in WAL mode, so they run inline on the event loop.
    def _save_event(self, session_id: str, stint: Stint, event: AgentEvent) -> None:
        with Session(self._engine) as db:
            row = SessionEvent(session_id=session_id, stint_id=stint.id, agent=stint.agent, type=event.type,
                               payload=event.data, raw=event.raw)
            db.add(row)
            db.commit()
            db.refresh(row)
        self._publish(session_id, _event_message(row))

    def _save_native_session_id(self, stint_id: int, native_id: str | None) -> None:
        if not native_id:
            return
        with Session(self._engine) as db:
            stint = db.get(Stint, stint_id)
            if stint is None or stint.native_session_id == native_id:
                return
            stint.native_session_id = native_id
            db.add(stint)
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


def _limited_agents(db: Session, session_id: str) -> set[AgentKind]:
    """Agents out of usage in the current run of automatic handoffs, which must not be tried again.

    Walks back from the current stint (which just hit its limit) through stints that took over
    because of a limit and never finished a turn: the agent before each of those hit its limit
    too. A stint that finished a turn made progress, so agents before it may have reset since.
    Without this, a chain like Claude, Codex, Claude could hand off back and forth forever.
    """
    stints = db.exec(select(Stint).where(Stint.session_id == session_id).order_by(col(Stint.seq).desc())).all()
    limited = {stints[0].agent}
    for stint, before in zip(stints, stints[1:]):
        finished = db.exec(
            select(SessionEvent.id).where(
                SessionEvent.stint_id == stint.id,
                col(SessionEvent.type).in_([EventType.COMPLETED, EventType.QUESTION]),
            )
        ).first()
        if stint.reason is not StintReason.LIMIT or finished is not None:
            break
        limited.add(before.agent)
    return limited


async def _simulated_limit(events: AsyncGenerator[AgentEvent]) -> AsyncGenerator[AgentEvent]:
    """HUB_SIMULATE_LIMIT: the turn runs for real, but ends in a usage limit instead of completing."""
    async with aclosing(events):
        async for event in events:
            if event.type in (EventType.COMPLETED, EventType.QUESTION):
                yield AgentEvent(EventType.LIMIT_REACHED, {
                    "message": "Simulated usage limit (HUB_SIMULATE_LIMIT is set).",
                    "simulated": True,
                })
            else:
                yield event


def _limit_already_reached(db: Session, stint: Stint) -> bool:
    """True if the stint's last usage report says the limit is reached and hasn't reset yet."""
    report = db.exec(
        select(SessionEvent)
        .where(SessionEvent.stint_id == stint.id, SessionEvent.type == EventType.RATE_LIMIT)
        .order_by(col(SessionEvent.id).desc())
    ).first()
    if report is None or report.payload.get("status") != "rejected":
        return False
    resets_at = report.payload.get("resets_at")
    return not (isinstance(resets_at, int | float) and resets_at < time.time())


def _event_message(row: SessionEvent) -> StreamMessage:
    return StreamMessage("event", EventRead.model_validate(row).model_dump(mode="json"), id=row.id)
