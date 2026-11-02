"""add chat to sessions

A plain ADD/DROP COLUMN, never a batch rebuild: rebuilding sessions would cascade-delete its child rows.
Autogenerate also reported the usual false events.stint_id FK change, removed here.

Revision ID: 95160a18dfff
Revises: d5f2ecc04d7f
Create Date: 2026-11-02 00:48:38.531352
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '95160a18dfff'
down_revision: str | None = 'd5f2ecc04d7f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('sessions', sa.Column('chat', sa.Boolean(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    op.drop_column('sessions', 'chat')
