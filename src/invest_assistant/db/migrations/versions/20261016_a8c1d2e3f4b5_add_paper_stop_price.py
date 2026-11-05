"""add paper_position.stop_price (v3 mechanical stop)

Revision ID: a8c1d2e3f4b5
Revises: f6a9b0c1d2e3
Create Date: 2026-10-16 23:30:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a8c1d2e3f4b5'
down_revision = 'f6a9b0c1d2e3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('paper_position', schema=None) as batch_op:
        batch_op.add_column(sa.Column('stop_price', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('paper_position', schema=None) as batch_op:
        batch_op.drop_column('stop_price')
