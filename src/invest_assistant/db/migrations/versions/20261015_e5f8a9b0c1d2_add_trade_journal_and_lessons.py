"""add trade journal and trading lessons

Revision ID: e5f8a9b0c1d2
Revises: c3e6a7b8d9f0
Create Date: 2026-10-15 14:30:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'e5f8a9b0c1d2'
down_revision = 'c3e6a7b8d9f0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'trade_journal',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('ref_id', sa.Integer(), nullable=True),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('action', sa.String(length=16), nullable=False),
        sa.Column('qty', sa.Float(), nullable=False, server_default='0'),
        sa.Column('price', sa.Float(), nullable=False, server_default='0'),
        sa.Column('amount', sa.Float(), nullable=False, server_default='0'),
        sa.Column('fee', sa.Float(), nullable=False, server_default='0'),
        sa.Column('realized_pnl', sa.Float(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('context_json', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_trade_journal_ts'), 'trade_journal', ['ts'])
    op.create_index(op.f('ix_trade_journal_source'), 'trade_journal', ['source'])
    op.create_index(op.f('ix_trade_journal_code'), 'trade_journal', ['code'])

    op.create_table(
        'trading_lesson',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('ref_id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('entry_date', sa.Date(), nullable=True),
        sa.Column('exit_date', sa.Date(), nullable=True),
        sa.Column('holding_days', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('pnl', sa.Float(), nullable=False, server_default='0'),
        sa.Column('pnl_pct', sa.Float(), nullable=False, server_default='0'),
        sa.Column('rating', sa.String(length=8), nullable=False, server_default=''),
        sa.Column('summary', sa.Text(), nullable=False, server_default=''),
        sa.Column('lessons_json', sa.JSON(), nullable=False),
        sa.Column('model', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('cost', sa.Float(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'ref_id', name='uq_lesson_ref'),
    )
    op.create_index(op.f('ix_trading_lesson_created_at'), 'trading_lesson', ['created_at'])
    op.create_index(op.f('ix_trading_lesson_source'), 'trading_lesson', ['source'])
    op.create_index(op.f('ix_trading_lesson_code'), 'trading_lesson', ['code'])


def downgrade() -> None:
    op.drop_index(op.f('ix_trading_lesson_code'), table_name='trading_lesson')
    op.drop_index(op.f('ix_trading_lesson_source'), table_name='trading_lesson')
    op.drop_index(op.f('ix_trading_lesson_created_at'), table_name='trading_lesson')
    op.drop_table('trading_lesson')
    op.drop_index(op.f('ix_trade_journal_code'), table_name='trade_journal')
    op.drop_index(op.f('ix_trade_journal_source'), table_name='trade_journal')
    op.drop_index(op.f('ix_trade_journal_ts'), table_name='trade_journal')
    op.drop_table('trade_journal')
