"""add summaries

A new table only. Autogenerate also reported the usual false events.stint_id FK change, removed here.

Revision ID: d5f2ecc04d7f
Revises: c5e66c0f3b10
Create Date: 2026-10-29 11:57:07.260754
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = 'd5f2ecc04d7f'
down_revision: str | None = 'c5e66c0f3b10'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('summaries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('agent', sa.Enum('claude', 'codex', 'ollama', name='agentkind', native_enum=False, length=32), nullable=False),
    sa.Column('model', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('text', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('tokens', sa.Integer(), nullable=True),
    sa.Column('fallback_from', sa.Enum('claude', 'codex', 'ollama', name='agentkind', native_enum=False, length=32), nullable=True),
    sa.Column('last_event_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_summaries_session_id', 'summaries', ['session_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_summaries_session_id', table_name='summaries')
    op.drop_table('summaries')
