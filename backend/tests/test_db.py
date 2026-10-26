from collections.abc import Iterator

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, select

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlmodel import SQLModel

from app.db import alembic_config, init_db, make_engine
from app.models import AgentKind, AgentSession, HubSettings, SessionEvent, Stint, StintReason, load_hub_settings, save_hub_settings


@pytest.fixture
def db() -> Iterator[Session]:
    engine = make_engine("sqlite://", poolclass=StaticPool)
    init_db(engine)
    with Session(engine) as session:
        yield session


def test_migrations_create_tables(db: Session) -> None:
    tables = set(inspect(db.get_bind()).get_table_names())
    assert {"sessions", "stints", "events", "settings", "alembic_version"} <= tables


def test_models_match_migrations(db: Session) -> None:
    diffs = compare_metadata(MigrationContext.configure(db.connection()), SQLModel.metadata)
    # events.stint_id was added with an inline REFERENCES clause, whose ON DELETE SQLAlchemy's
    # SQLite reflection doesn't read, so autogenerate sees the key as changed. Check it directly.
    fk = [row for row in db.exec(text("PRAGMA foreign_key_list(events)")) if row[3] == "stint_id"]
    assert [(row[2], row[6]) for row in fk] == [("stints", "CASCADE")]
    diffs = [d for d in diffs if not (d[0] in ("remove_fk", "add_fk") and d[1].column_keys == ["stint_id"])]
    assert diffs == []


def test_enums_stored_by_value(db: Session) -> None:
    agent_session = AgentSession(title="t", project_path=".")
    db.add(agent_session)
    db.add(Stint(session_id=agent_session.id, seq=1, agent=AgentKind.CODEX, reason=StintReason.NEW))
    db.commit()
    assert db.exec(text("SELECT status FROM sessions")).one() == ("ready",)
    assert db.exec(text("SELECT agent, reason FROM stints")).one() == ("codex", "new")


def test_stints_migration_keeps_existing_history() -> None:
    """Sessions saved before stints existed become one stint each, and keep their events."""
    engine = make_engine("sqlite://", poolclass=StaticPool)
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "eb23da6d5410")
        connection.execute(text(
            "INSERT INTO sessions (id, title, project_path, agent, model, status, native_session_id, created_at, updated_at) "
            "VALUES ('s1', 't', '.', 'codex', 'gpt-6-sol', 'completed', 'thread-1', '2026-10-20 10:00:00', '2026-10-20 10:00:00')"
        ))
        connection.execute(text(
            "INSERT INTO events (session_id, agent, type, payload, created_at) "
            "VALUES ('s1', 'codex', 'message', '{}', '2026-10-20 10:00:01')"
        ))
    init_db(engine)

    with Session(engine) as db:
        stint = db.exec(select(Stint)).one()
        assert (stint.session_id, stint.seq, stint.agent, stint.model, stint.native_session_id, stint.reason) == (
            "s1", 1, AgentKind.CODEX, "gpt-6-sol", "thread-1", StintReason.NEW
        )
        event = db.exec(select(SessionEvent)).one()
        assert event.stint_id == stint.id
        assert db.get(AgentSession, "s1").status == "completed"

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "eb23da6d5410")
        assert connection.execute(text("SELECT agent, model, native_session_id FROM sessions")).one() == (
            "codex", "gpt-6-sol", "thread-1"
        )
        assert connection.execute(text("SELECT count(*) FROM events")).scalar() == 1


def test_hub_settings_default_and_saved(db: Session) -> None:
    defaults = load_hub_settings(db)
    assert [e.agent for e in defaults.default_chain] == ["claude", "codex", "ollama"]
    assert defaults.progress_layout == "panel"
    save_hub_settings(db, HubSettings.model_validate(
        {"default_chain": [{"agent": "codex"}], "limit_policy": "ask", "progress_layout": "split"}))
    saved = load_hub_settings(db)
    assert ([e.agent for e in saved.default_chain], saved.limit_policy) == (["codex"], "ask")
    assert saved.progress_layout == "split"


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
