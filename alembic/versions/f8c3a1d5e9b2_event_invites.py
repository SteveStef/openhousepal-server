"""Invites - event_invites, event_invite_rsvps, event_invite_recipients

Forms & Invites -> Invites: an agent's designed invitation (open house, broker's open, client
event / dinner, custom) at /i/<code>, guests' RSVPs, and the people it was emailed to. New tables
only - nothing existing is altered or backfilled.

Revision ID: f8c3a1d5e9b2
Revises: e7b2d4f6a8c1
Create Date: 2026-10-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f8c3a1d5e9b2"
down_revision: Union[str, None] = "e7b2d4f6a8c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "event_invites",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("invite_type", sa.String(24), nullable=False),
        sa.Column("audience", sa.String(12), nullable=False, server_default="clients"),
        sa.Column("title", sa.String(140), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("start_time", sa.String(5), nullable=True),
        sa.Column("end_time", sa.String(5), nullable=True),
        sa.Column("place_name", sa.String(140), nullable=True),
        sa.Column("address", sa.String(300), nullable=True),
        sa.Column("linked_property", postgresql.JSONB(), nullable=True),
        sa.Column("cover_image_url", sa.String(1000), nullable=True),
        sa.Column("design", sa.String(24), nullable=False, server_default="classic"),
        sa.Column("accent_color", sa.String(9), nullable=True),
        sa.Column("questions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(12), nullable=False, server_default="live"),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_event_invites_owner_id", "event_invites", ["owner_id"])
    op.create_index("ix_event_invites_code", "event_invites", ["code"], unique=True)

    op.create_table(
        "event_invite_rsvps",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("invite_id", sa.String(), sa.ForeignKey("event_invites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token", sa.String(48), nullable=False),
        sa.Column("response", sa.String(8), nullable=False),
        sa.Column("name", sa.String(140), nullable=True),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("guest_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("answers", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("lead_id", sa.String(), sa.ForeignKey("leads.id", ondelete="SET NULL"), nullable=True),
        sa.Column("is_seen", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_event_invite_rsvps_invite_id", "event_invite_rsvps", ["invite_id"])
    op.create_index("ix_event_invite_rsvps_owner_id", "event_invite_rsvps", ["owner_id"])
    op.create_index("ix_event_invite_rsvps_token", "event_invite_rsvps", ["token"], unique=True)

    op.create_table(
        "event_invite_recipients",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("invite_id", sa.String(), sa.ForeignKey("event_invites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(200), nullable=False),
        sa.Column("name", sa.String(140), nullable=True),
        sa.Column("send_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("invite_id", "email", name="uq_event_invite_recipients_invite_email"),
    )
    op.create_index("ix_event_invite_recipients_invite_id", "event_invite_recipients", ["invite_id"])


def downgrade() -> None:
    op.drop_index("ix_event_invite_recipients_invite_id", table_name="event_invite_recipients")
    op.drop_table("event_invite_recipients")
    op.drop_index("ix_event_invite_rsvps_token", table_name="event_invite_rsvps")
    op.drop_index("ix_event_invite_rsvps_owner_id", table_name="event_invite_rsvps")
    op.drop_index("ix_event_invite_rsvps_invite_id", table_name="event_invite_rsvps")
    op.drop_table("event_invite_rsvps")
    op.drop_index("ix_event_invites_code", table_name="event_invites")
    op.drop_index("ix_event_invites_owner_id", table_name="event_invites")
    op.drop_table("event_invites")
