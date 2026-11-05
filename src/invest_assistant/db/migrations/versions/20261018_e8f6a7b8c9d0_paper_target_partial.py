"""add paper_position target/partial fields (v12 mechanical exits)

Revision ID: e8f6a7b8c9d0
Revises: d7e5f6a7b8c9
Create Date: 2026-10-18 23:30:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'e8f6a7b8c9d0'
down_revision = 'd7e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('paper_position', schema=None) as batch_op:
        batch_op.add_column(sa.Column('target_price', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('partial_price', sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column('partial_taken', sa.Boolean(), nullable=False, server_default=sa.text('0'))
        )


def downgrade() -> None:
    with op.batch_alter_table('paper_position', schema=None) as batch_op:
        batch_op.drop_column('partial_taken')
        batch_op.drop_column('partial_price')
        batch_op.drop_column('target_price')
