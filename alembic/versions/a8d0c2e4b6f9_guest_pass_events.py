"""Guest Pass activity log + "Showcase on the pass" switch

guest_pass_events - what each guest did on their Guest Pass, with times (visits + time spent, homes
opened, Cheat Sheet, map, requests) for the agent's activity log in the Visitors window.
open_house_events.pass_showcase - with Showcases switched off for an open house, the Guest Pass can
still offer a "Get my Showcase" button (built only when tapped). New table + one column with a
constant default - nothing is backfilled.

Revision ID: a8d0c2e4b6f9
Revises: e5a7c9b1d3f2
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a8d0c2e4b6f9"
down_revision: Union[str, None] = "e5a7c9b1d3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("open_house_events", sa.Column("pass_showcase", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "guest_pass_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("visitor_id", sa.String(), sa.ForeignKey("open_house_visitors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("open_house_event_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("key", sa.String(64), nullable=True),
        sa.Column("label", sa.String(200), nullable=True),
        sa.Column("session", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_guest_pass_events_visitor_id", "guest_pass_events", ["visitor_id"])
    op.create_index("ix_guest_pass_events_open_house_event_id", "guest_pass_events", ["open_house_event_id"])


def downgrade() -> None:
    op.drop_index("ix_guest_pass_events_open_house_event_id", table_name="guest_pass_events")
    op.drop_index("ix_guest_pass_events_visitor_id", table_name="guest_pass_events")
    op.drop_table("guest_pass_events")
    op.drop_column("open_house_events", "pass_showcase")
