"""Add photo_url to users

Revision ID: f4b8e21c9a06
Revises: a3f1c9d02b7e
Create Date: 2026-07-27 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4b8e21c9a06'
down_revision: Union[str, None] = 'a3f1c9d02b7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('photo_url', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'photo_url')
