"""Invites: several days, Normal / Luxe animation, Luxe metal, cover framing, the open house it
belongs to, the day-before email; RSVPs: which day(s), agent / has-an-agent answers, lead sorting,
"still interested" / "keep me updated" and the guest's one-tap requests.

event_invites.extra_dates        - days beyond the first ([{date, start, end}]); event_date /
                                   start_time / end_time stay the first day, so nothing else changes.
event_invites.motion             - 'normal' | 'luxe' (the envelope + page animation).
event_invites.luxe_metal         - 'gold' | 'silver' | 'bronze' when a Luxe metal colour is picked.
event_invites.cover_position     - {x, y} % framing of the cover photo (dragged in the builder).
event_invites.open_house_event_id- the agent's open house for this home (Cheat Sheet + requests).
event_invites.agent_reminded     - the days the agent already got their "tomorrow" email for.
event_invite_rsvps.days          - which of the days they're coming to (several-day invites).
event_invite_rsvps.is_agent / has_agent / lead_kind / still_interested / keep_updated / requests.

New nullable columns (one with a constant default) - nothing is backfilled.

Revision ID: f3b5d7e9a1c4
Revises: a8d0c2e4b6f9
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3b5d7e9a1c4"
down_revision: Union[str, None] = "a8d0c2e4b6f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("event_invites", sa.Column("extra_dates", postgresql.JSONB(), nullable=True))
    op.add_column("event_invites", sa.Column("motion", sa.String(12), nullable=False, server_default="normal"))
    op.add_column("event_invites", sa.Column("luxe_metal", sa.String(8), nullable=True))
    op.add_column("event_invites", sa.Column("cover_position", postgresql.JSONB(), nullable=True))
    op.add_column("event_invites", sa.Column("open_house_event_id", sa.String(), nullable=True))
    op.add_column("event_invites", sa.Column("agent_reminded", postgresql.JSONB(), nullable=True))
    op.add_column("event_invite_rsvps", sa.Column("days", postgresql.JSONB(), nullable=True))
    op.add_column("event_invite_rsvps", sa.Column("is_agent", sa.Boolean(), nullable=True))
    op.add_column("event_invite_rsvps", sa.Column("has_agent", sa.Boolean(), nullable=True))
    op.add_column("event_invite_rsvps", sa.Column("lead_kind", sa.String(12), nullable=True))
    op.add_column("event_invite_rsvps", sa.Column("still_interested", sa.Boolean(), nullable=True))
    op.add_column("event_invite_rsvps", sa.Column("keep_updated", sa.Boolean(), nullable=True))
    op.add_column("event_invite_rsvps", sa.Column("requests", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    for col in ("requests", "keep_updated", "still_interested", "lead_kind", "has_agent", "is_agent", "days"):
        op.drop_column("event_invite_rsvps", col)
    for col in ("agent_reminded", "open_house_event_id", "cover_position", "luxe_metal", "motion", "extra_dates"):
        op.drop_column("event_invites", col)
