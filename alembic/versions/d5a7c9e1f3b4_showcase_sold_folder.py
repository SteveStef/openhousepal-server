"""collection_properties.sold_filed_at - a Showcase home moved into its Sold/Removed folder

Homes that go under contract / pending / sold / off the market no longer move into Sold/Removed on
their own (user, 10/21): they stay in the list with a notice and a "Move to Sold/Removed" button for
the buyer or the agent. This column records that move.

Homes already sitting in Sold/Removed stay there: the links whose home is off the market today are
stamped once. That touches collection_properties only (a few rows per Showcase) - the properties
table is only read, never rewritten.

Revision ID: d5a7c9e1f3b4
Revises: c4f6a8b0d2e1
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd5a7c9e1f3b4'
down_revision: Union[str, None] = 'c4f6a8b0d2e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('collection_properties', sa.Column('sold_filed_at', sa.DateTime(timezone=True), nullable=True))
    # Same Active / Coming Soon rule as collections_service._is_property_available (no status = for sale).
    op.execute("""
        UPDATE collection_properties cp
           SET sold_filed_at = now()
          FROM properties p
         WHERE p.id = cp.property_id
           AND p.home_status IS NOT NULL
           AND regexp_replace(lower(p.home_status), '[^a-z0-9]', '', 'g')
               NOT IN ('active', 'activebright', 'comingsoon', 'comingsoonbright')
    """)


def downgrade() -> None:
    op.drop_column('collection_properties', 'sold_filed_at')
