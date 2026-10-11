"""Backfill Showcase likes / dislikes / search-change requests onto lead activity

Until now a client's likes, dislikes and "change my search" messages lived only on the Showcase,
so a lead's activity never showed them. New ones are logged as they happen
(leads_service.log_showcase_activity); this adds the ones made before that, once. Data only -
safe to re-run (skips anything already logged).

Revision ID: e4b8c2f6a1d3
Revises: d7a3e9c1b5f2
Create Date: 2026-10-03
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e4b8c2f6a1d3"
down_revision: Union[str, None] = "d7a3e9c1b5f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The lead each Showcase belongs to: same agent, same email (the oldest lead if there are two).
_SHOWCASE_LEAD = """
    WITH showcase_lead AS (
        SELECT DISTINCT ON (c.id) c.id AS collection_id, l.id AS lead_id
        FROM collections c
        JOIN leads l ON l.owner_id = c.owner_id AND l.email = lower(trim(c.visitor_email))
        WHERE c.visitor_email IS NOT NULL
        ORDER BY c.id, l.created_at ASC
    )
"""


def upgrade() -> None:
    for kind, flag, verb in (("showcase_like", "liked", "Liked"), ("showcase_dislike", "disliked", "Passed on")):
        op.execute(_SHOWCASE_LEAD + f"""
            INSERT INTO lead_events (id, lead_id, source_type, source_id, summary, property_address, collection_id,
                                     occurred_at, agent_viewed_at)
            SELECT md5(random()::text || clock_timestamp()::text || pi.id)::uuid::text, sl.lead_id, '{kind}', pi.property_id,
                   '{verb} ' || coalesce(nullif(p.street_address, ''), 'a home') || ' in their Showcase',
                   nullif(concat_ws(', ', nullif(p.street_address, ''), nullif(p.city, '')), ''),
                   pi.collection_id, coalesce(pi.updated_at, pi.created_at, now()), pi.agent_viewed_at
            FROM property_interactions pi
            JOIN showcase_lead sl ON sl.collection_id = pi.collection_id
            LEFT JOIN properties p ON p.id = pi.property_id
            WHERE pi.{flag} = true
              AND NOT EXISTS (
                  SELECT 1 FROM lead_events e
                  WHERE e.lead_id = sl.lead_id AND e.source_type = '{kind}'
                    AND e.source_id = pi.property_id AND e.collection_id = pi.collection_id
              )
        """)
    op.execute(_SHOWCASE_LEAD + """
        INSERT INTO lead_events (id, lead_id, source_type, source_id, summary, collection_id, raw_snapshot, occurred_at)
        SELECT md5(random()::text || clock_timestamp()::text || c.id)::uuid::text, sl.lead_id, 'showcase_refine', c.id,
               'Asked you to change their Showcase search', c.id,
               jsonb_build_object('message', c.refine_request_message), coalesce(c.refine_request_at, now())
        FROM collections c
        JOIN showcase_lead sl ON sl.collection_id = c.id
        WHERE c.refine_request_message IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM lead_events e
              WHERE e.lead_id = sl.lead_id AND e.source_type = 'showcase_refine' AND e.collection_id = c.id
          )
    """)


def downgrade() -> None:
    # Leave the activity in place - it's a record of what clients did, not schema.
    pass
