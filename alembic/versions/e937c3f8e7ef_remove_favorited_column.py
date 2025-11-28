"""remove_favorited_column

Revision ID: e937c3f8e7ef
Revises: 8d3ce7050151
Create Date: 2025-11-28 00:17:50.581047

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e937c3f8e7ef'
down_revision: Union[str, None] = '8d3ce7050151'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Remove the favorited column from property_interactions table
    op.drop_column('property_interactions', 'favorited')


def downgrade() -> None:
    # Restore the favorited column if needed
    op.add_column('property_interactions',
                  sa.Column('favorited', sa.Boolean(), nullable=False, server_default=sa.false()))
