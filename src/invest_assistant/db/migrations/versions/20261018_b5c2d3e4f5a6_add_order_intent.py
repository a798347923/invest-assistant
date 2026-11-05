"""add order_intent (live instruction sheet + audit)

Revision ID: b5c2d3e4f5a6
Revises: a8c1d2e3f4b5
Create Date: 2026-10-18 10:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b5c2d3e4f5a6'
down_revision = 'a8c1d2e3f4b5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'order_intent',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('trade_date', sa.Date(), nullable=False),
        sa.Column('code', sa.String(16), nullable=False),
        sa.Column('name', sa.String(64), nullable=False, server_default=''),
        sa.Column('side', sa.String(8), nullable=False),
        sa.Column('qty', sa.Integer(), nullable=False),
        sa.Column('price', sa.Float(), nullable=False),
        sa.Column('source', sa.String(32), nullable=False, server_default='manual'),
        sa.Column('mode', sa.String(8), nullable=False, server_default='semi'),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('risk_json', sa.JSON(), nullable=False),
        sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column('filled_price', sa.Float(), nullable=True),
        sa.Column('filled_qty', sa.Integer(), nullable=True),
        sa.Column('filled_at', sa.DateTime(), nullable=True),
        sa.Column('executor', sa.String(16), nullable=False, server_default='manual'),
        sa.Column('note', sa.Text(), nullable=False, server_default=''),
    )
    op.create_index('ix_order_intent_created_at', 'order_intent', ['created_at'])
    op.create_index('ix_order_intent_trade_date', 'order_intent', ['trade_date'])
    op.create_index('ix_order_intent_code', 'order_intent', ['code'])
    op.create_index('ix_order_intent_status', 'order_intent', ['status'])
    op.create_index(
        'ix_order_intent_idempotency_key', 'order_intent', ['idempotency_key'], unique=True
    )


def downgrade() -> None:
    op.drop_table('order_intent')
