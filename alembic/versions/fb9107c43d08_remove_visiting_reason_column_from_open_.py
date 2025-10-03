"""Remove visiting_reason column from open_house_visitors

Revision ID: fb9107c43d08
Revises: 8cb8e1b6e011
Create Date: 2025-10-01 16:18:56.491555

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fb9107c43d08'
down_revision: Union[str, None] = '8cb8e1b6e011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop visiting_reason column from open_house_visitors table
    with op.batch_alter_table('open_house_visitors', schema=None) as batch_op:
        batch_op.drop_column('visiting_reason')


def downgrade() -> None:
    # Add visiting_reason column back to open_house_visitors table
    with op.batch_alter_table('open_house_visitors', schema=None) as batch_op:
        batch_op.add_column(sa.Column('visiting_reason', sa.VARCHAR(), nullable=False))
