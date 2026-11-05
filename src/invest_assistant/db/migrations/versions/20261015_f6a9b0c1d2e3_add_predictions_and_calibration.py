"""add predictions and calibration reports

Revision ID: f6a9b0c1d2e3
Revises: e5f8a9b0c1d2
Create Date: 2026-10-15 22:30:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f6a9b0c1d2e3'
down_revision = 'e5f8a9b0c1d2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'prediction',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('card_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.String(length=24), nullable=False, server_default=''),
        sa.Column('price', sa.Float(), nullable=False, server_default='0'),
        sa.Column('zone_low', sa.Float(), nullable=True),
        sa.Column('zone_high', sa.Float(), nullable=True),
        sa.Column('breakout', sa.Float(), nullable=True),
        sa.Column('stop', sa.Float(), nullable=True),
        sa.Column('regime', sa.String(length=24), nullable=False, server_default=''),
        sa.Column('trend', sa.String(length=24), nullable=False, server_default=''),
        sa.Column('price_pct', sa.Float(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('outcome_json', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_prediction_date'), 'prediction', ['date'])
    op.create_index(op.f('ix_prediction_code'), 'prediction', ['code'])
    op.create_index(op.f('ix_prediction_status'), 'prediction', ['status'])

    op.create_table(
        'calibration_report',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False, server_default=''),
        sa.Column('content_json', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('date'),
    )
    op.create_index(op.f('ix_calibration_report_date'), 'calibration_report', ['date'])


def downgrade() -> None:
    op.drop_index(op.f('ix_calibration_report_date'), table_name='calibration_report')
    op.drop_table('calibration_report')
    op.drop_index(op.f('ix_prediction_status'), table_name='prediction')
    op.drop_index(op.f('ix_prediction_code'), table_name='prediction')
    op.drop_index(op.f('ix_prediction_date'), table_name='prediction')
    op.drop_table('prediction')
