"""CMA accuracy test through the real agent UI: cma_backtest_arms.

An "arm" tells the server that, for one test agent account and one sold listing, the next CMA the
agent builds in the normal wizard is an accuracy-test run as of a past date: the address search,
listing lookup, comp search and AI generation then see the home as it stood that day. Only the
admin test runner creates arms; they expire on their own.

Revision ID: d5e7f9a1b3c4
Revises: c4d6e8f0a2b3
Create Date: 2026-10-20 00:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'd5e7f9a1b3c4'
down_revision: Union[str, None] = 'c4d6e8f0a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cma_backtest_arms',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('batch_id', sa.String(), sa.ForeignKey('cma_backtest_batches.id', ondelete='CASCADE'), nullable=True),
        sa.Column('item_index', sa.Integer(), nullable=True),
        sa.Column('listing_key', sa.String(), nullable=False),
        sa.Column('address_key', sa.String(), nullable=True),
        sa.Column('facts', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('dry_run', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('report_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_cma_backtest_arms_owner_id', 'cma_backtest_arms', ['owner_id'])
    op.create_index('ix_cma_backtest_arms_report_id', 'cma_backtest_arms', ['report_id'])


def downgrade() -> None:
    op.drop_index('ix_cma_backtest_arms_report_id', table_name='cma_backtest_arms')
    op.drop_index('ix_cma_backtest_arms_owner_id', table_name='cma_backtest_arms')
    op.drop_table('cma_backtest_arms')
