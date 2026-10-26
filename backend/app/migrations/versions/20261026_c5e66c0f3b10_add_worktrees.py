"""add worktrees

A new table only. Autogenerate also reported the usual false events.stint_id FK change, removed here.

Revision ID: c5e66c0f3b10
Revises: a3c5d1f0b7e2
Create Date: 2026-10-26 11:09:33.236564
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = 'c5e66c0f3b10'
down_revision: str | None = 'a3c5d1f0b7e2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('worktrees',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('stint_id', sa.Integer(), nullable=True),
    sa.Column('repo_path', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('path', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('subdir', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('branch', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('base_commit', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('status', sa.Enum('active', 'removed', name='worktreestatus', native_enum=False, length=32), nullable=False),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.Column('removed_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['stint_id'], ['stints.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_worktrees_session_id', 'worktrees', ['session_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_worktrees_session_id', table_name='worktrees')
    op.drop_table('worktrees')
