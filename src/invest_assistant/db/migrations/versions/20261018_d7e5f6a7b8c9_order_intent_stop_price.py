"""add order_intent.stop_price (mechanical stop at signal time)

Revision ID: d7e5f6a7b8c9
Revises: c6d4e5f6a7b8
Create Date: 2026-10-18 22:30:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd7e5f6a7b8c9'
down_revision = 'c6d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('order_intent', schema=None) as batch_op:
        batch_op.add_column(sa.Column('stop_price', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('order_intent', schema=None) as batch_op:
        batch_op.drop_column('stop_price')
