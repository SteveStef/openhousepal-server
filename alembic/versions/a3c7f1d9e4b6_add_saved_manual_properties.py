"""Add saved_manual_properties

Revision ID: a3c7f1d9e4b6
Revises: d2e5f9a3b7c1
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c7f1d9e4b6'
down_revision: Union[str, None] = 'd2e5f9a3b7c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'saved_manual_properties',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('agent_id', sa.String(), nullable=False),
        sa.Column('address', sa.String(), nullable=False),
        sa.Column('city', sa.String(), nullable=True),
        sa.Column('state', sa.String(), nullable=True),
        sa.Column('zipcode', sa.String(), nullable=True),
        sa.Column('price', sa.Float(), nullable=True),
        sa.Column('beds', sa.Float(), nullable=True),
        sa.Column('baths', sa.Float(), nullable=True),
        sa.Column('sqft', sa.Float(), nullable=True),
        sa.Column('image_url', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['agent_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_saved_manual_properties_agent_id', 'saved_manual_properties', ['agent_id'])


def downgrade() -> None:
    op.drop_index('ix_saved_manual_properties_agent_id', table_name='saved_manual_properties')
    op.drop_table('saved_manual_properties')
