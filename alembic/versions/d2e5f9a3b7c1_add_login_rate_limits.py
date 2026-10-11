"""Add login_rate_limits

Revision ID: d2e5f9a3b7c1
Revises: c1d4e8f2a6b9
Create Date: 2026-08-30 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e5f9a3b7c1'
down_revision: Union[str, None] = 'c1d4e8f2a6b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'login_rate_limits',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('scope', sa.String(), nullable=False),
        sa.Column('identifier', sa.String(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('window_started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('scope', 'identifier', name='uq_login_rate_limit_scope_identifier'),
    )


def downgrade() -> None:
    op.drop_table('login_rate_limits')
