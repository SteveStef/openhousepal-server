"""remove_server_default_from_added_at_column

Revision ID: bc42630995d7
Revises: 7b3f01fa8a3b
Create Date: 2025-11-12 13:09:53.074012

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bc42630995d7'
down_revision: Union[str, None] = '7b3f01fa8a3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Remove the default value from added_at column
    # This allows NULL values to be stored (for initial properties without NEW badge)
    with op.batch_alter_table('collection_properties', schema=None) as batch_op:
        batch_op.alter_column('added_at',
                              existing_type=sa.DateTime(timezone=True),
                              nullable=True,
                              server_default=None)  # Remove server_default


def downgrade() -> None:
    # Restore the default value to added_at column
    with op.batch_alter_table('collection_properties', schema=None) as batch_op:
        batch_op.alter_column('added_at',
                              existing_type=sa.DateTime(timezone=True),
                              nullable=True,
                              server_default=sa.func.now())  # Restore server_default
