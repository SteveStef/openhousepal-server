"""Add idx_domain_migration_agreed_at to users

Revision ID: b7c3e9f1a4d2
Revises: d9f3a2b5c714
Create Date: 2026-08-25 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c3e9f1a4d2'
down_revision: Union[str, None] = 'd9f3a2b5c714'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('idx_domain_migration_agreed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'idx_domain_migration_agreed_at')
