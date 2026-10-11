"""Add email signatures

Revision ID: f7a2c9e4b8d6
Revises: a4c1e8b7d2f9
Create Date: 2026-09-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f7a2c9e4b8d6'
down_revision: Union[str, None] = 'a4c1e8b7d2f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'email_signatures',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('public_token', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id'),
        sa.UniqueConstraint('public_token'),
    )
    op.create_index(op.f('ix_email_signatures_public_token'), 'email_signatures', ['public_token'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_email_signatures_public_token'), table_name='email_signatures')
    op.drop_table('email_signatures')
