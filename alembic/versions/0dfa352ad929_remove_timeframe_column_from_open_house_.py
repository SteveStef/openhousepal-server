"""Remove timeframe column from open_house_visitors and collection_preferences

Revision ID: 0dfa352ad929
Revises: 2ca0cb39fddd
Create Date: 2025-11-18 00:18:39.636839

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0dfa352ad929'
down_revision: Union[str, None] = '2ca0cb39fddd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop timeframe columns from both tables
    # Using batch mode for SQLite compatibility
    with op.batch_alter_table('collection_preferences', schema=None) as batch_op:
        batch_op.drop_column('timeframe')

    with op.batch_alter_table('open_house_visitors', schema=None) as batch_op:
        batch_op.drop_column('timeframe')


def downgrade() -> None:
    # Add timeframe columns back
    # Using batch mode for SQLite compatibility
    with op.batch_alter_table('open_house_visitors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('timeframe', sa.VARCHAR(), nullable=False))

    with op.batch_alter_table('collection_preferences', schema=None) as batch_op:
        batch_op.add_column(sa.Column('timeframe', sa.VARCHAR(), nullable=True))
