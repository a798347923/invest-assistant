"""add order_intent.ref_position_id (link sell intents to live positions)

Revision ID: c6d4e5f6a7b8
Revises: b5c2d3e4f5a6
Create Date: 2026-10-18 21:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c6d4e5f6a7b8'
down_revision = 'b5c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('order_intent', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ref_position_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('order_intent', schema=None) as batch_op:
        batch_op.drop_column('ref_position_id')
