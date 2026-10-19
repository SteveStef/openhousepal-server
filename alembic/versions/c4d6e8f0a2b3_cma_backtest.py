"""CMA accuracy test: cma_reports.is_backtest + cma_backtest_batches.

Admin-only backtests re-run real past sales through the normal CMA flow "as of" the day before
they went under contract. Their reports are flagged so no agent list, share link or client view
ever shows them, and the batches table holds each run's scoreboard.

Revision ID: c4d6e8f0a2b3
Revises: e7a9c1b3d5f8
Create Date: 2026-10-19 23:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'c4d6e8f0a2b3'
down_revision: Union[str, None] = 'e7a9c1b3d5f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('cma_reports', sa.Column('is_backtest', sa.Boolean(), nullable=False, server_default='false'))
    op.create_index('ix_cma_reports_is_backtest', 'cma_reports', ['is_backtest'])
    op.create_table(
        'cma_backtest_batches',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='queued'),
        sa.Column('settings', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('items', postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('estimated_cost_usd', sa.Float(), nullable=True),
        sa.Column('actual_cost_usd', sa.Float(), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_cma_backtest_batches_owner_id', 'cma_backtest_batches', ['owner_id'])


def downgrade() -> None:
    op.drop_index('ix_cma_backtest_batches_owner_id', table_name='cma_backtest_batches')
    op.drop_table('cma_backtest_batches')
    op.drop_index('ix_cma_reports_is_backtest', table_name='cma_reports')
    op.drop_column('cma_reports', 'is_backtest')
