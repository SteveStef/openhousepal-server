"""Showcases: take rentals and commercial space out of Showcases that never asked for them

The Showcase search used to treat "no home type picked" as every listing on the market, so buyer
Showcases picked up rentals ($2,100/mo) and shops/offices (plus "homes" under $10k - mis-tagged
rentals, timeshare weeks). The search now leaves those out unless the agent picks Rentals or
Commercial; this removes the ones already sitting in Showcases. Likes/comments rows are left alone.

Revision ID: e8c3a1f6b2d4
Revises: d7b2e5f1a9c3
Create Date: 2026-10-05
"""
from alembic import op

revision = 'e8c3a1f6b2d4'
down_revision = 'd7b2e5f1a9c3'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        DELETE FROM collection_properties cp
        USING properties p, collection_preferences pr
        WHERE cp.property_id = p.id
          AND pr.collection_id = cp.collection_id
          AND (
                (p.home_type = 'RESIDENTIAL_LEASE' AND COALESCE(pr.is_apartment, false) = false)
             OR (p.home_type = 'COMMERCIAL' AND COALESCE(pr.is_commercial, false) = false)
             OR (p.price < 10000 AND COALESCE(p.home_type, '') NOT IN ('LAND', 'FARM', 'RESIDENTIAL_LEASE', 'COMMERCIAL')
                 AND COALESCE(pr.is_apartment, false) = false AND COALESCE(pr.is_commercial, false) = false)
          )
    """)


def downgrade():
    pass
