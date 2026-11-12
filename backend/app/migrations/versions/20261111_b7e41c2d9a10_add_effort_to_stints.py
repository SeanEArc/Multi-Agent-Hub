"""add effort to stints

A plain ADD/DROP COLUMN, never a batch rebuild: rebuilding stints would cascade-delete its child rows.

Revision ID: b7e41c2d9a10
Revises: 95160a18dfff
Create Date: 2026-11-11 14:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = 'b7e41c2d9a10'
down_revision: str | None = '95160a18dfff'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('stints', sa.Column('effort', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    op.drop_column('stints', 'effort')
