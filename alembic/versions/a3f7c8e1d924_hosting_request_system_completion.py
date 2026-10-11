"""hosting request system completion - team invites, request history, sharing/eligibility toggles

Revision ID: a3f7c8e1d924
Revises: 6d520472a09b
Create Date: 2026-08-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f7c8e1d924"
down_revision: Union[str, None] = "6d520472a09b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Brokerage/team request-eligibility restriction (default False = today's brokerage-scoped
    # default behavior; True = only accepted My Team members may request to host).
    op.add_column(
        "users",
        sa.Column("restrict_host_requests_to_team", sa.Boolean(), nullable=False, server_default="false"),
    )

    # Hosting-agent-controlled, per open house, guest-detail sharing with the listing agent.
    op.add_column(
        "open_house_events",
        sa.Column("share_guests_with_listing_agent", sa.Boolean(), nullable=False, server_default="false"),
    )

    # Team invite lifecycle - existing rows grandfather in as already-accepted.
    op.add_column(
        "open_house_team_members",
        sa.Column("status", sa.String(), nullable=False, server_default="accepted"),
    )
    op.add_column(
        "open_house_team_members",
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_open_house_team_members_status", "open_house_team_members", ["status"])

    # Append-only history/thread for OpenHouseHostRequest negotiation.
    op.create_table(
        "open_house_host_request_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("request_id", sa.String(), sa.ForeignKey("open_house_host_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.String(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("proposed_date", sa.Date(), nullable=True),
        sa.Column("proposed_start_time", sa.Time(), nullable=True),
        sa.Column("proposed_end_time", sa.Time(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_open_house_host_request_events_request_id", "open_house_host_request_events", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_open_house_host_request_events_request_id", table_name="open_house_host_request_events")
    op.drop_table("open_house_host_request_events")

    op.drop_index("ix_open_house_team_members_status", table_name="open_house_team_members")
    op.drop_column("open_house_team_members", "responded_at")
    op.drop_column("open_house_team_members", "status")

    op.drop_column("open_house_events", "share_guests_with_listing_agent")

    op.drop_column("users", "restrict_host_requests_to_team")
