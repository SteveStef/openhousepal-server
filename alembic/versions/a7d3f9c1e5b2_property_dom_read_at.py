"""properties.dom_read_at - when DaysOnMarket was last read from Bright

Days on market is Bright's count on the day we read the listing, aged by the days since. That read
time was taken from updated_at, which geocoding, the open-house pass and other jobs bump too - each
bump froze the count (10/20: 712 Stoke Rd, listed 10/14, still "2 days" in a follow-up text).

Column only, no backfill: rewriting every property row holds a lock on the table for minutes (it
froze the dev DB), and the site would stall during the deploy. Rows without it fall back to a best
guess in utils/listing_dom.dom_fetched_at and get the real value the next time Bright sends them.

Revision ID: a7d3f9c1e5b2
Revises: f1a3c5e7b9d2
Create Date: 2026-10-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a7d3f9c1e5b2'
down_revision: Union[str, None] = 'f1a3c5e7b9d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # nullable, no default: a catalog-only change in Postgres - instant on a table of any size
    op.add_column('properties', sa.Column('dom_read_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('properties', 'dom_read_at')
