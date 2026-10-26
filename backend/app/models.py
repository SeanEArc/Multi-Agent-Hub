import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import field_validator
from sqlalchemy import Enum as SAEnum
from sqlalchemy import UniqueConstraint
from sqlmodel import JSON, Column, Field, Session, SQLModel, col, select


def utcnow() -> datetime:
    return datetime.now(UTC)


def enum_column(enum_cls: type[StrEnum], **kwargs) -> Column:
    """Store an enum by its value ("ready"), not its member name ("READY"), as plain text."""
    return Column(
        SAEnum(
            enum_cls,
            native_enum=False,
            # No CHECK constraint, so adding a member never needs a table rebuild.
            create_constraint=False,
            length=32,
            values_callable=lambda members: [m.value for m in members],
        ),
        **kwargs,
    )


class SessionStatus(StrEnum):
    READY = "ready"
    WORKING = "working"
    HANDING_OFF = "handing_off"
    WAITING_FOR_INPUT = "waiting_for_input"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentKind(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    OLLAMA = "ollama"


class StintReason(StrEnum):
    """Why an agent took over the session."""

    NEW = "new"  # the session's first agent
    LIMIT = "limit"  # the previous agent hit its usage limit
    MANUAL = "manual"  # the user handed the session over
    DELEGATION = "delegation"  # phase 7


class LimitPolicy(StrEnum):
    AUTO = "auto"  # hand off to the next agent in the chain right away
    ASK = "ask"  # wait until the user confirms the handoff


class WorktreeStatus(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"  # folder deleted; the branch is kept for the user to merge


class ChainEntry(SQLModel):
    agent: AgentKind
    model: str | None = None


# --- Sessions ---------------------------------------------------------------


class AgentSession(SQLModel, table=True):
    """A task in a project folder. The agents working on it are its stints, one after another."""

    __tablename__ = "sessions"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    title: str
    project_path: str
    status: SessionStatus = Field(
        default=SessionStatus.READY, sa_column=enum_column(SessionStatus, nullable=False)
    )
    # Handoff order and policy for this session; None means the global default.
    chain: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    limit_policy: LimitPolicy | None = Field(default=None, sa_column=enum_column(LimitPolicy, nullable=True))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow, sa_column_kwargs={"onupdate": utcnow})


class Stint(SQLModel, table=True):
    """One agent's turn at a session, from when it took over until the next agent did."""

    __tablename__ = "stints"
    __table_args__ = (UniqueConstraint("session_id", "seq"),)

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", ondelete="CASCADE", index=True)
    seq: int  # 1, 2, ... in the order agents took over
    agent: AgentKind = Field(sa_column=enum_column(AgentKind, nullable=False))
    model: str | None = None
    # The agent CLI's own session id, used to resume (e.g. `claude --resume`).
    native_session_id: str | None = None
    reason: StintReason = Field(sa_column=enum_column(StintReason, nullable=False))
    # The stint this one returns to when done (delegation, phase 7). Never set in phase 3.
    parent_stint_id: int | None = Field(default=None, foreign_key="stints.id", ondelete="SET NULL")
    # The prompt the agent started from: the handoff brief. None for the first stint.
    brief: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None


class Worktree(SQLModel, table=True):
    """A git worktree the hub created: its own folder on its own branch, so parallel work never collides."""

    __tablename__ = "worktrees"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", ondelete="CASCADE", index=True)
    # None: the whole session works here. Set for a stint with its own worktree (parallel split, phase 8).
    stint_id: int | None = Field(default=None, foreign_key="stints.id", ondelete="CASCADE")
    repo_path: str  # top level of the user's repository
    path: str  # the worktree folder
    # The project folder relative to the repository's top level ("" when it is the top level).
    subdir: str = ""
    branch: str
    base_commit: str  # where the branch started; the handoff diff and summaries compare against it
    status: WorktreeStatus = Field(
        default=WorktreeStatus.ACTIVE, sa_column=enum_column(WorktreeStatus, nullable=False)
    )
    created_at: datetime = Field(default_factory=utcnow)
    removed_at: datetime | None = None


def session_worktree(db: Session, session_id: str) -> Worktree | None:
    """The worktree the whole session works in, if it has one."""
    return db.exec(
        select(Worktree)
        .where(Worktree.session_id == session_id, col(Worktree.stint_id).is_(None))
        .order_by(col(Worktree.id).desc())
    ).first()


def current_stint(db: Session, session_id: str) -> Stint | None:
    """The agent working on the session now. Phase 3 has one at a time: the latest."""
    return db.exec(select(Stint).where(Stint.session_id == session_id).order_by(col(Stint.seq).desc())).first()


class SessionCreate(SQLModel):
    title: str
    project_path: str
    agent: AgentKind = AgentKind.CLAUDE
    model: str | None = None
    chain: list[ChainEntry] | None = None
    limit_policy: LimitPolicy | None = None
    # Work in a new git worktree on its own branch. Ignored for folders that aren't in a git repository.
    use_worktree: bool = True

    @field_validator("project_path")
    @classmethod
    def project_path_must_exist(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_dir():
            raise ValueError("must be an existing directory")
        return str(path.resolve())


class MessageCreate(SQLModel):
    text: str = Field(min_length=1)


class HandoffCreate(SQLModel):
    agent: AgentKind
    model: str | None = None
    # Sent to the new agent after the brief, and shown as a user message.
    note: str | None = None


class StintRead(SQLModel):
    id: int
    seq: int
    agent: AgentKind
    model: str | None
    native_session_id: str | None
    reason: StintReason
    parent_stint_id: int | None
    started_at: datetime
    ended_at: datetime | None


class StintDetail(StintRead):
    brief: str | None


class WorktreeRead(SQLModel):
    id: int
    stint_id: int | None
    repo_path: str
    path: str
    subdir: str
    branch: str
    base_commit: str
    status: WorktreeStatus
    created_at: datetime
    removed_at: datetime | None


class WorktreeRemove(SQLModel):
    # What to do with uncommitted changes: commit them to the branch first, or throw them away.
    # With neither, removing a worktree that has changes is refused.
    commit: bool = False
    discard: bool = False


class SessionRead(SQLModel):
    id: str
    title: str
    project_path: str
    status: SessionStatus
    # The current stint's agent, model and native session id.
    agent: AgentKind
    model: str | None
    native_session_id: str | None
    # The model the current agent said it used, which names a default model (`model` is None).
    reported_model: str | None
    chain: list[ChainEntry] | None
    limit_policy: LimitPolicy | None
    stints: list[StintRead]
    # The session's worktree (None when it runs in the project folder itself).
    worktree: WorktreeRead | None
    created_at: datetime
    updated_at: datetime


def session_read(db: Session, agent_session: AgentSession) -> SessionRead:
    stints = db.exec(select(Stint).where(Stint.session_id == agent_session.id).order_by(col(Stint.seq))).all()
    current = stints[-1]
    reported = db.exec(
        select(SessionEvent)
        .where(SessionEvent.stint_id == current.id, col(SessionEvent.type).in_(["started", "model"]))
        .order_by(col(SessionEvent.id).desc())
    ).first()
    return SessionRead(
        **agent_session.model_dump(),
        agent=current.agent,
        model=current.model,
        native_session_id=current.native_session_id,
        reported_model=(reported.payload or {}).get("model") if reported else None,
        stints=[StintRead.model_validate(s) for s in stints],
        worktree=session_worktree(db, agent_session.id),
    )


# --- Settings ---------------------------------------------------------------


class Setting(SQLModel, table=True):
    """App-wide settings edited in the UI, one JSON value per key."""

    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: Any = Field(sa_column=Column(JSON))


DEFAULT_CHAIN = [ChainEntry(agent=AgentKind.CLAUDE), ChainEntry(agent=AgentKind.CODEX), ChainEntry(agent=AgentKind.OLLAMA)]


class HubSettings(SQLModel):
    default_chain: list[ChainEntry] = Field(default_factory=lambda: list(DEFAULT_CHAIN), min_length=1)
    limit_policy: LimitPolicy = LimitPolicy.AUTO


def load_hub_settings(db: Session) -> HubSettings:
    stored = {row.key: row.value for row in db.exec(select(Setting))}
    return HubSettings.model_validate(stored)


def save_hub_settings(db: Session, settings: HubSettings) -> None:
    for key, value in settings.model_dump(mode="json").items():
        db.merge(Setting(key=key, value=value))
    db.commit()


# --- Events -----------------------------------------------------------------


class SessionEvent(SQLModel, table=True):
    """One normalized event from an agent run, with the raw payload kept for debugging."""

    __tablename__ = "events"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", ondelete="CASCADE", index=True)
    stint_id: int | None = Field(default=None, foreign_key="stints.id", ondelete="CASCADE", index=True)
    agent: AgentKind = Field(sa_column=enum_column(AgentKind, nullable=False))
    type: str
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # The agent's untouched event, for debugging and re-parsing. Not returned by the API.
    raw: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class EventRead(SQLModel):
    id: int
    session_id: str
    stint_id: int | None
    agent: AgentKind
    type: str
    payload: dict[str, Any]
    created_at: datetime
