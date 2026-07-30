"""Add agent_forms.headline and users.professional_title/market

Revision ID: e5f7a9c2d4b6
Revises: d2a4f6e8b1c3
Create Date: 2026-07-27 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f7a9c2d4b6'
down_revision: Union[str, None] = 'd2a4f6e8b1c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_forms', sa.Column('headline', sa.String(), nullable=True))
    op.add_column('users', sa.Column('professional_title', sa.String(), nullable=True))
    op.add_column('users', sa.Column('market', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'market')
    op.drop_column('users', 'professional_title')
    op.drop_column('agent_forms', 'headline')
