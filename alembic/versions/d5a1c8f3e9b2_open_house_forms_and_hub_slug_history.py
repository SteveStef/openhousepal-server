"""add open_house_form_requests table and agent_hubs.previous_slugs

Revision ID: d5a1c8f3e9b2
Revises: c4f8a2e6b7d1
Create Date: 2026-08-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "d5a1c8f3e9b2"
down_revision: Union[str, None] = "c4f8a2e6b7d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "open_house_form_requests",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("open_house_event_id", sa.String(), sa.ForeignKey("open_house_events.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("form_type", sa.String(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=False),
        sa.Column("wants_another_tour", sa.Boolean(), nullable=True),
        sa.Column("another_tour_time", sa.String(), nullable=True),
        sa.Column("preferred_visit_time", sa.String(), nullable=True),
        sa.Column("lead_id", sa.String(), sa.ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    op.add_column("agent_hubs", sa.Column("previous_slugs", postgresql.JSONB(), nullable=False, server_default="[]"))


def downgrade() -> None:
    op.drop_column("agent_hubs", "previous_slugs")
    op.drop_table("open_house_form_requests")
