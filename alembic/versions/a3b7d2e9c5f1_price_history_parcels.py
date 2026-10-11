"""Price history backfill bookkeeping: which parcels have had their 2012+ listings pulled

Bright's feed carries every listing back to 2012 (Bright Data Licensing, Oct 2026), but the
recurring sync only adds listings as they appear - a home's earlier listings and sales were never
pulled. price_history_backfill_service fetches them parcel by parcel (Bright allows one parcel per
query) and records each parcel here so a run picks up where the last one stopped.

Revision ID: a3b7d2e9c5f1
Revises: f2a9c4e7b1d3
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa

revision = 'a3b7d2e9c5f1'
down_revision = 'f2a9c4e7b1d3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'price_history_parcels',
        sa.Column('tax_id', sa.String(), primary_key=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('listings_found', sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_table('price_history_parcels')
