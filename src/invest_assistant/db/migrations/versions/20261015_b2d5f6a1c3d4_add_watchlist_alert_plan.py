"""add watchlist alert_plan

Revision ID: b2d5f6a1c3d4
Revises: ae1804d2b920
Create Date: 2026-10-15 12:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b2d5f6a1c3d4'
down_revision = 'ae1804d2b920'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('watchlist_item', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('alert_plan', sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table('watchlist_item', schema=None) as batch_op:
        batch_op.drop_column('alert_plan')
