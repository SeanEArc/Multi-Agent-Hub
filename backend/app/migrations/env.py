"""Alembic environment.

At app startup, `app.db.init_db` passes in an open connection. From the CLI
(`alembic revision --autogenerate`), the database URL comes from app settings.
"""

from alembic import context
from sqlmodel import SQLModel

from app import models  # noqa: F401  register tables on SQLModel.metadata
from app.config import get_settings
from app.db import make_engine

target_metadata = SQLModel.metadata


def run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite can't ALTER most things in place; batch mode rebuilds the table instead.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    run_migrations(connection)
else:
    engine = make_engine(get_settings().database_url)
    with engine.connect() as connection:
        run_migrations(connection)
