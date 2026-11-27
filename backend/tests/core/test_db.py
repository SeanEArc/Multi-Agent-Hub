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
from app.models import (AgentKind, AgentSession, HubSettings, SessionEvent, SessionStatus, Stint, StintReason,
                        load_hub_settings, save_hub_settings, session_read)


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


def test_session_lists_running_subagents(db: Session) -> None:
    agent_session = AgentSession(title="t", project_path=".", status=SessionStatus.WORKING)
    db.add(agent_session)
    stint = Stint(session_id=agent_session.id, seq=1, agent=AgentKind.CLAUDE, reason=StintReason.NEW)
    db.add(stint)
    db.commit()

    def add(type: str, **payload: object) -> None:
        db.add(SessionEvent(session_id=agent_session.id, stint_id=stint.id, agent=AgentKind.CLAUDE, type=type,
                            payload=payload))

    def task(tool_use_id: str, description: str) -> None:
        add("tool_use", tool_use_id=tool_use_id, name="Task",
            input={"description": description, "subagent_type": "Explore"})

    task("old", "from an earlier turn")  # stopped before it reported back
    add("user_message", text="go")
    task("a", "Find the refund code")
    task("b", "Read the tests")
    task("c", "Check the docs")
    add("tool_use", tool_use_id="a1", name="Grep", input={"pattern": "refund"}, parent_tool_use_id="a")
    add("message", text="Found it in ledger.py", parent_tool_use_id="a")
    add("tool_use", tool_use_id="b1", name="Read", input={"file_path": "x.py"}, parent_tool_use_id="b")
    add("tool_result", tool_use_id="c", content="Docs are current", is_error=False)
    add("tool_use", tool_use_id="t1", name="Read", input={"file_path": "y.py"})
    db.commit()

    running = session_read(db, agent_session).running_subagents
    assert [(s.tool_use_id, s.kind, s.description, s.steps) for s in running] == [
        ("a", "Explore", "Find the refund code", 2), ("b", "Explore", "Read the tests", 1)
    ]
    assert running[0].last_step.payload["text"] == "Found it in ledger.py"

    # Only a working session lists them.
    agent_session.status = SessionStatus.WAITING_FOR_INPUT
    assert session_read(db, agent_session).running_subagents == []


def test_session_shows_current_stints_latest_plan(db: Session) -> None:
    agent_session = AgentSession(title="t", project_path=".")
    db.add(agent_session)
    first = Stint(session_id=agent_session.id, seq=1, agent=AgentKind.CODEX, reason=StintReason.NEW)
    db.add(first)
    db.commit()
    assert session_read(db, agent_session).plan is None

    def plan(stint: Stint, *statuses: str) -> None:
        items = [{"text": f"step {i + 1}", "status": s} for i, s in enumerate(statuses)]
        db.add(SessionEvent(session_id=agent_session.id, stint_id=stint.id, agent=stint.agent, type="plan",
                            payload={"items": items}))
        db.commit()

    plan(first, "in_progress", "pending")
    plan(first, "completed", "in_progress")
    assert [(i.text, i.status) for i in session_read(db, agent_session).plan] == [
        ("step 1", "completed"), ("step 2", "in_progress")
    ]

    # After a handoff, the new agent has no plan until it writes one.
    second = Stint(session_id=agent_session.id, seq=2, agent=AgentKind.CLAUDE, reason=StintReason.MANUAL)
    db.add(second)
    db.commit()
    assert session_read(db, agent_session).plan is None


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
