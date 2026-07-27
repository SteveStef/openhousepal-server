"""Add Agent Hub reserved URLs

Revision ID: 91c0d7e8a4f2
Revises: 36811bfbee6a
Create Date: 2026-07-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '91c0d7e8a4f2'
down_revision: Union[str, None] = '36811bfbee6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_hubs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_index(op.f('ix_agent_hubs_slug'), 'agent_hubs', ['slug'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_agent_hubs_slug'), table_name='agent_hubs')
    op.drop_table('agent_hubs')
