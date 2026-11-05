"""add paper trading tables

Revision ID: c3e6a7b8d9f0
Revises: b2d5f6a1c3d4
Create Date: 2026-10-15 13:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c3e6a7b8d9f0'
down_revision = 'b2d5f6a1c3d4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'paper_position',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('qty', sa.Float(), nullable=False, server_default='0'),
        sa.Column('avg_cost', sa.Float(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='open'),
        sa.Column('opened_at', sa.DateTime(), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('last_price', sa.Float(), nullable=False, server_default='0'),
        sa.Column('realized_pnl', sa.Float(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_paper_position_code'), 'paper_position', ['code'])
    op.create_index(op.f('ix_paper_position_status'), 'paper_position', ['status'])

    op.create_table(
        'paper_trade',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('side', sa.String(length=8), nullable=False),
        sa.Column('qty', sa.Float(), nullable=False),
        sa.Column('price', sa.Float(), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('fee', sa.Float(), nullable=False, server_default='0'),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('realized_pnl', sa.Float(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_paper_trade_ts'), 'paper_trade', ['ts'])
    op.create_index(op.f('ix_paper_trade_code'), 'paper_trade', ['code'])

    op.create_table(
        'paper_report',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('model', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('content_json', sa.JSON(), nullable=False),
        sa.Column('execution_json', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('tokens_in', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens_out', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost', sa.Float(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('date'),
    )
    op.create_index(op.f('ix_paper_report_date'), 'paper_report', ['date'])

    op.create_table(
        'paper_daily',
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('cash', sa.Float(), nullable=False, server_default='0'),
        sa.Column('market_value', sa.Float(), nullable=False, server_default='0'),
        sa.Column('equity', sa.Float(), nullable=False, server_default='0'),
        sa.Column('day_pnl', sa.Float(), nullable=False, server_default='0'),
        sa.Column('total_pnl_pct', sa.Float(), nullable=False, server_default='0'),
        sa.Column('positions', sa.Integer(), nullable=False, server_default='0'),
        sa.PrimaryKeyConstraint('date'),
    )


def downgrade() -> None:
    op.drop_table('paper_daily')
    op.drop_index(op.f('ix_paper_report_date'), table_name='paper_report')
    op.drop_table('paper_report')
    op.drop_index(op.f('ix_paper_trade_code'), table_name='paper_trade')
    op.drop_index(op.f('ix_paper_trade_ts'), table_name='paper_trade')
    op.drop_table('paper_trade')
    op.drop_index(op.f('ix_paper_position_status'), table_name='paper_position')
    op.drop_index(op.f('ix_paper_position_code'), table_name='paper_position')
    op.drop_table('paper_position')
