"""Add usage_budget_counters (merges the two existing heads)

Revision ID: a1c4f9e27b3d
Revises: 8b0d7a6c2f11, e9b4c2d8f1a6
Create Date: 2026-08-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c4f9e27b3d'
down_revision: Union[str, None] = ('8b0d7a6c2f11', 'e9b4c2d8f1a6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'usage_budget_counters',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('bucket', sa.String(), nullable=False),
        sa.Column('period_type', sa.String(), nullable=False),
        sa.Column('period_key', sa.String(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost_micros', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('bucket', 'period_type', 'period_key', name='uq_usage_budget_counter'),
    )


def downgrade() -> None:
    op.drop_table('usage_budget_counters')
