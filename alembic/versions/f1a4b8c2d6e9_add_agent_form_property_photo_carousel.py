"""Add agent form property_photo_carousel

Revision ID: f1a4b8c2d6e9
Revises: e2b6c4a8f0d5
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a4b8c2d6e9'
down_revision: Union[str, None] = 'e2b6c4a8f0d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_forms', sa.Column('property_photo_carousel', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('agent_forms', 'property_photo_carousel')
