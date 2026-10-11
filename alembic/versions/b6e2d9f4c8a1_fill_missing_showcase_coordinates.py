"""Give Showcase searches "within N miles of an address" the coordinates they're missing

A search with an address but no lat/long had no distance limit at all, so it matched homes across
the whole MLS (an open house saved without map coordinates passed that gap on to its Showcase).
New ones are placed when they're made / refreshed (collection_preferences_service.coordinates_for);
this places the existing ones from our own synced listings - street + ZIP. Data only, re-runnable.

Revision ID: b6e2d9f4c8a1
Revises: a3d5e7f9b1c4
Create Date: 2026-10-03
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b6e2d9f4c8a1"
down_revision: Union[str, None] = "a3d5e7f9b1c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(r"""
        UPDATE collection_preferences cp
        SET lat = m.lat, long = m.lng, diameter = COALESCE(NULLIF(cp.diameter, 0), 6)
        FROM (
            SELECT DISTINCT ON (c.id) c.id,
                   COALESCE(p.google_rooftop_latitude, p.latitude) AS lat,
                   COALESCE(p.google_rooftop_longitude, p.longitude) AS lng
            FROM collection_preferences c
            JOIN properties p
              ON upper(trim(split_part(c.address, ',', 1))) = upper(trim(p.street_address))
             AND p.zipcode = substring(c.address from '(\d{5})\s*$')
            WHERE c.lat IS NULL AND COALESCE(c.address, '') <> '' AND p.latitude IS NOT NULL
            ORDER BY c.id
        ) m
        WHERE cp.id = m.id
    """)


def downgrade() -> None:
    pass
