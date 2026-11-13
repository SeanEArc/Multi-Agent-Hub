"""Released databases upgrade without losing anything, and migrations stay safe for user data.

Every release adds `tests/release_dbs/v<version>.sql` (scripts/snapshot_release_db.py). Users upgrade
from any earlier version, so each dump is migrated to head here and checked row by row.
"""

import re
import sqlite3
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, select

from app.db import MIGRATIONS_DIR, alembic_config, init_db, make_engine
from app.models import AgentSession, load_hub_settings, session_read

RELEASE_DBS = sorted((Path(__file__).parent / "release_dbs").glob("v*.sql"))
# Tables whose rows are the user's history; none may lose a row in an upgrade.
HISTORY_TABLES = ("sessions", "stints", "events", "worktrees", "summaries", "settings")


def load_dump(path: Path):
    engine = make_engine("sqlite://", poolclass=StaticPool)
    raw = engine.raw_connection()
    try:
        # The dump lists tables alphabetically, so child rows can come before their parent table exists.
        raw.driver_connection.executescript("PRAGMA foreign_keys=OFF;\n" + path.read_text(encoding="utf-8")
                                            + "\nPRAGMA foreign_keys=ON;")
    finally:
        raw.close()
    return engine


def row_counts(engine) -> dict[str, int]:
    tables = set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        return {t: connection.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
                for t in HISTORY_TABLES if t in tables}


def test_there_is_a_release_db() -> None:
    assert RELEASE_DBS, "tests/release_dbs is empty; run scripts/snapshot_release_db.py"


@pytest.mark.parametrize("dump", RELEASE_DBS, ids=lambda p: p.stem)
def test_release_db_upgrades_to_head_without_losing_rows(dump: Path) -> None:
    engine = load_dump(dump)
    before = row_counts(engine)
    assert before.get("sessions"), "the dump should hold sample sessions"

    init_db(engine)

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == head
    after = row_counts(engine)
    for table, count in before.items():
        assert after[table] == count, f"{table}: {count} rows before the upgrade, {after[table]} after"
    # The app can still read everything back the way the API does.
    with Session(engine) as db:
        sessions = db.exec(select(AgentSession)).all()
        assert all(session_read(db, s).id == s.id for s in sessions)
        load_hub_settings(db)


def test_backup_is_made_before_a_pending_migration(tmp_path: Path) -> None:
    dump = RELEASE_DBS[0]
    db_file = tmp_path / "data" / "hub.db"
    db_file.parent.mkdir()
    with sqlite3.connect(db_file) as connection:
        connection.executescript(dump.read_text(encoding="utf-8"))
    engine = make_engine(f"sqlite:///{db_file.as_posix()}")
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()

    copy = init_db(engine)

    if revision == head:
        # The newest dump is already at head; nothing to migrate, so nothing to copy.
        assert copy is None
        return
    assert copy is not None and copy.parent == db_file.parent / "backups"
    assert revision in copy.name
    with sqlite3.connect(copy) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (revision,)
    engine.dispose()


def test_backup_copies_an_old_database_and_keeps_only_the_newest(tmp_path: Path, monkeypatch) -> None:
    from alembic import command

    import app.db as db_module

    db_file = tmp_path / "hub.db"
    engine = make_engine(f"sqlite:///{db_file.as_posix()}")
    config = alembic_config()
    first = ScriptDirectory.from_config(config).get_base()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, first)
        connection.execute(text("INSERT INTO sessions (id, title, project_path, agent, status, created_at, updated_at) "
                                "VALUES ('s1', 't', '.', 'claude', 'ready', '2026-01-01', '2026-01-01')"))
    folder = tmp_path / "backups"
    folder.mkdir()
    for i in range(6):
        (folder / f"hub-2020010{i}-000000-old.db").write_bytes(b"")
    monkeypatch.setattr(db_module, "BACKUPS_KEPT", 5)

    copy = db_module.backup_before_migrating(engine, config, keep=db_module.BACKUPS_KEPT)

    assert copy is not None and first in copy.name
    with sqlite3.connect(copy) as connection:
        assert connection.execute("SELECT id FROM sessions").fetchall() == [("s1",)]
    kept = sorted(p.name for p in folder.glob("hub-*.db"))
    assert len(kept) == 5 and copy.name in kept
    engine.dispose()


def test_no_backup_for_a_new_or_current_database(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{(tmp_path / 'hub.db').as_posix()}")
    assert init_db(engine) is None  # brand new: no schema to lose
    assert init_db(engine) is None  # already at head
    assert not (tmp_path / "backups").exists()
    engine.dispose()


def test_migrations_never_rebuild_or_drop_history_tables() -> None:
    """A batch rebuild of sessions or stints cascades deletes to child rows (see CLAUDE.md)."""
    rebuild = re.compile(r"batch_alter_table\(\s*['\"](sessions|stints)['\"]")
    drop = re.compile(r"op\.drop_table\(\s*['\"](%s)['\"]" % "|".join(HISTORY_TABLES))
    offenders = []
    for path in sorted((MIGRATIONS_DIR / "versions").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        upgrade = source.split("def downgrade", 1)[0]
        if rebuild.search(upgrade) or drop.search(upgrade):
            offenders.append(path.name)
    assert offenders == []
