from collections.abc import Iterator

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, select

from app.db import init_db, make_engine
from app.models import AgentKind, AgentSession, SessionEvent


@pytest.fixture
def db() -> Iterator[Session]:
    engine = make_engine("sqlite://", poolclass=StaticPool)
    init_db(engine)
    with Session(engine) as session:
        yield session


def test_migrations_create_tables(db: Session) -> None:
    tables = set(inspect(db.get_bind()).get_table_names())
    assert {"sessions", "events", "alembic_version"} <= tables


def test_enums_stored_by_value(db: Session) -> None:
    db.add(AgentSession(title="t", project_path="."))
    db.commit()
    assert db.exec(text("SELECT agent, status FROM sessions")).one() == ("claude", "ready")


def test_foreign_keys_enforced(db: Session) -> None:
    db.add(SessionEvent(session_id="missing", agent=AgentKind.CLAUDE, type="message"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_deleting_session_cascades_to_events(db: Session) -> None:
    agent_session = AgentSession(title="t", project_path=".")
    db.add(agent_session)
    db.commit()
    db.add(SessionEvent(session_id=agent_session.id, agent=AgentKind.CLAUDE, type="message"))
    db.commit()

    db.delete(agent_session)
    db.commit()
    assert db.exec(select(SessionEvent)).all() == []


def test_updated_at_changes_on_update(db: Session) -> None:
    agent_session = AgentSession(title="t", project_path=".")
    db.add(agent_session)
    db.commit()
    before = agent_session.updated_at

    agent_session.title = "renamed"
    db.commit()
    db.refresh(agent_session)
    assert agent_session.updated_at > before
