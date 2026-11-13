import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import Request
from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlmodel import Session, create_engine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
# How many pre-migration copies of the database `backup_before_migrating` keeps.
BACKUPS_KEPT = 5

log = logging.getLogger(__name__)


def make_engine(database_url: str, **kwargs) -> Engine:
    url = make_url(database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    if is_sqlite:
        if url.database not in (None, "", ":memory:"):
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        # FastAPI runs sync endpoints in a thread pool, so SQLite must allow cross-thread use.
        kwargs.setdefault("connect_args", {"check_same_thread": False})

    engine = create_engine(database_url, **kwargs)
    if is_sqlite:
        event.listen(engine, "connect", _configure_sqlite)
    return engine


def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    # SQLite ignores foreign keys unless asked, per connection.
    cursor.execute("PRAGMA foreign_keys=ON")
    # WAL lets the UI read while an agent run writes events. No-op for in-memory DBs.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", MIGRATIONS_DIR.as_posix())
    return config


def init_db(engine: Engine, backup: bool = True) -> Path | None:
    """Bring the database schema up to date by running any pending migrations.

    With `backup`, an existing file database is copied first when a migration is pending, so a release
    whose migration goes wrong can't lose the user's history. Returns the copy's path, if one was made.
    """
    config = alembic_config()
    copy = backup_before_migrating(engine, config) if backup else None
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    return copy


def backup_before_migrating(engine: Engine, config: Config, keep: int = BACKUPS_KEPT) -> Path | None:
    """Copy a SQLite file database to `<its folder>/backups` when a migration is about to run.

    Nothing is copied for an in-memory or brand-new database (no schema yet) or one already at head.
    The copy uses SQLite's backup API, which is safe with WAL, and is named after the revision it holds.
    Only the newest `keep` copies are kept.
    """
    url = engine.url
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        return None
    head = ScriptDirectory.from_config(config).get_current_head()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
        if current is None or current == head:
            return None
        folder = Path(url.database).parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = folder / f"{Path(url.database).stem}-{stamp}-{current}.db"
        source = connection.connection.driver_connection
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    log.info("Database at revision %s needs migrating to %s; copied it to %s first", current, head, target)
    for old in sorted(folder.glob(f"{Path(url.database).stem}-*.db"), key=lambda p: p.stat().st_mtime)[:-keep]:
        old.unlink(missing_ok=True)
    return target


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session
