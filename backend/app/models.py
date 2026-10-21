import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import field_validator
from sqlalchemy import Enum as SAEnum
from sqlmodel import JSON, Column, Field, SQLModel


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


# --- Sessions ---------------------------------------------------------------


class SessionBase(SQLModel):
    title: str
    project_path: str
    agent: AgentKind = AgentKind.CLAUDE
    model: str | None = None


class AgentSession(SessionBase, table=True):
    __tablename__ = "sessions"

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    agent: AgentKind = Field(default=AgentKind.CLAUDE, sa_column=enum_column(AgentKind, nullable=False))
    status: SessionStatus = Field(
        default=SessionStatus.READY, sa_column=enum_column(SessionStatus, nullable=False)
    )
    # The agent CLI's own session id, used to resume (e.g. `claude --resume`).
    native_session_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow, sa_column_kwargs={"onupdate": utcnow})


class SessionCreate(SessionBase):
    @field_validator("project_path")
    @classmethod
    def project_path_must_exist(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_dir():
            raise ValueError("must be an existing directory")
        return str(path.resolve())


class MessageCreate(SQLModel):
    text: str = Field(min_length=1)


class SessionRead(SessionBase):
    id: str
    status: SessionStatus
    native_session_id: str | None
    created_at: datetime
    updated_at: datetime


# --- Events -----------------------------------------------------------------


class SessionEvent(SQLModel, table=True):
    """One normalized event from an agent run, with the raw payload kept for debugging."""

    __tablename__ = "events"

    id: int | None = Field(default=None, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", ondelete="CASCADE", index=True)
    agent: AgentKind = Field(sa_column=enum_column(AgentKind, nullable=False))
    type: str
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # The agent's untouched event, for debugging and re-parsing. Not returned by the API.
    raw: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class EventRead(SQLModel):
    id: int
    session_id: str
    agent: AgentKind
    type: str
    payload: dict[str, Any]
    created_at: datetime
