"""add cma_reports table

Revision ID: b3e6a1d9f024
Revises: f2a9c7d4e6b1
Create Date: 2026-08-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b3e6a1d9f024'
down_revision: Union[str, None] = 'f2a9c7d4e6b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cma_reports',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('client_name', sa.String(), nullable=True),
        sa.Column('mode', sa.String(), nullable=False, server_default='listing'),
        sa.Column('status', sa.String(), nullable=False, server_default='DRAFT'),
        sa.Column('subject', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('comparables', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('pricing', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('market_context', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('improvements', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('presentation', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
        sa.Column('share_slug', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('share_slug'),
    )
    op.create_index(op.f('ix_cma_reports_owner_id'), 'cma_reports', ['owner_id'])
    op.create_index(op.f('ix_cma_reports_status'), 'cma_reports', ['status'])
    op.create_index(op.f('ix_cma_reports_share_slug'), 'cma_reports', ['share_slug'])


def downgrade() -> None:
    op.drop_table('cma_reports')
