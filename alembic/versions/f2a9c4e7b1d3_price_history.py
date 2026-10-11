"""Price history: a log of every price / status change the sync sees, and a parcel index

Bright sends only the latest price change on a listing (OriginalListPrice / PreviousListPrice /
PriceChangeTimestamp), so a home cut twice showed one change. property_price_events records each
change as the sync observes it. ix_properties_listing_tax_id makes "every listing this parcel has
had" (the price history timeline, the 2012+ history backfill) an index lookup instead of a scan.

Revision ID: f2a9c4e7b1d3
Revises: e8c3a1f6b2d4
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'f2a9c4e7b1d3'
down_revision = 'e8c3a1f6b2d4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'property_price_events',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('listing_key', sa.String(), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),  # 'listed' | 'price' | 'status'
        sa.Column('price', sa.Float(), nullable=True),
        sa.Column('previous_price', sa.Float(), nullable=True),
        sa.Column('status', sa.String(), nullable=True),
        sa.Column('previous_status', sa.String(), nullable=True),
    )
    op.create_index('ix_property_price_events_listing_key', 'property_price_events', ['listing_key', 'observed_at'])
    op.create_index('ix_properties_listing_tax_id', 'properties', ['listing_tax_id'])


def downgrade():
    op.drop_index('ix_properties_listing_tax_id', table_name='properties')
    op.drop_index('ix_property_price_events_listing_key', table_name='property_price_events')
    op.drop_table('property_price_events')
