"""One house listed twice in the MLS shows once per Showcase

Some homes have two listing numbers (often one per town, e.g. 2801 Riverton Road, Moorestown and
2801 Riverton Rd, Cinnaminson), so a Showcase could show the same house twice. New matches are
already one per house (app.utils.same_home); this removes the extra copy from existing Showcases.
Kept: the copy the client liked / commented on / toured, else the most recently updated listing.
Data only, re-runnable.

Revision ID: e9a4c7d2f1b8
Revises: d8f3b2a6e4c1
Create Date: 2026-10-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.utils.same_home import is_same_home

revision: str = "e9a4c7d2f1b8"
down_revision: Union[str, None] = "d8f3b2a6e4c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    # Only links whose house number repeats within the same Showcase can be duplicates.
    rows = bind.execute(sa.text("""
        WITH links AS (
            SELECT cp.collection_id, p.id AS property_id, p.street_address, p.bedrooms, p.bathrooms,
                   p.living_area, p.latitude, p.longitude, p.modification_timestamp,
                   split_part(upper(trim(p.street_address)), ' ', 1) AS num
            FROM collection_properties cp JOIN properties p ON p.id = cp.property_id
        ), dup AS (
            SELECT collection_id, num FROM links GROUP BY collection_id, num HAVING count(*) > 1
        )
        SELECT l.*,
               EXISTS (SELECT 1 FROM property_interactions pi WHERE pi.collection_id = l.collection_id
                       AND pi.property_id = l.property_id AND pi.liked IS TRUE)
            OR EXISTS (SELECT 1 FROM property_comments pc WHERE pc.collection_id = l.collection_id
                       AND pc.property_id = l.property_id)
            OR EXISTS (SELECT 1 FROM property_tours pt WHERE pt.collection_id = l.collection_id
                       AND pt.property_id = l.property_id) AS acted
        FROM links l JOIN dup d ON d.collection_id = l.collection_id AND d.num = l.num
        ORDER BY l.collection_id, acted DESC, l.modification_timestamp DESC NULLS LAST
    """)).mappings().all()

    kept: dict = {}
    drop = []
    for r in rows:
        group = kept.setdefault(r["collection_id"], [])
        if any(is_same_home(r, q) for q in group) and not r["acted"]:
            drop.append((r["collection_id"], r["property_id"]))
        else:
            group.append(r)
    for cid, pid in drop:
        bind.execute(sa.text("DELETE FROM collection_properties WHERE collection_id = :c AND property_id = :p"),
                     {"c": cid, "p": pid})


def downgrade() -> None:
    pass
