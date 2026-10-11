"""add market area research tracking columns

Revision ID: 22728c527592
Revises: f1a7c3e9d2b5
Create Date: 2026-08-14 02:32:10.309617

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '22728c527592'
down_revision: Union[str, None] = 'f1a7c3e9d2b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# NOTE: autogenerate also picked up a large batch of unrelated index/constraint-naming drift on
# cma_reports/leads/open_house_listing_notes/site_domains/sites (pre-existing, not caused by this
# change) - deliberately excluded from this migration so it only does what its message says.
def upgrade() -> None:
    op.add_column('market_areas', sa.Column('last_verified_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('market_areas', sa.Column('research_version', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('market_areas', 'research_version')
    op.drop_column('market_areas', 'last_verified_at')
