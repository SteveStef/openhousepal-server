"""add market area geography

Revision ID: 8b0d7a6c2f11
Revises: 72f5c710d4a0
Create Date: 2026-08-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '8b0d7a6c2f11'
down_revision: Union[str, None] = '72f5c710d4a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'market_areas',
        sa.Column('geography', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'),
    )


def downgrade() -> None:
    op.drop_column('market_areas', 'geography')
