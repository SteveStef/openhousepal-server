"""Agent email health (user, 10/24 - "are agents opening our emails?"): open / click tracking on
emails TO agents (four new email_log columns, all with defaults - adding them is instant in
Postgres 11+, no rewrite, no backfill) and one small agent_email_health table per agent.

Revision ID: b7d9f1a3c5e8
Revises: c4e6a8b0d2f1
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d9f1a3c5e8"
down_revision: Union[str, None] = "c4e6a8b0d2f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("email_log", sa.Column("tracked", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("email_log", sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("email_log", sa.Column("open_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("email_log", sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True))
    # One email of each kind per person per event per day (email_dedupe_service) - a hash, no address.
    op.add_column("email_log", sa.Column("dedupe_key", sa.String(64), nullable=True))
    op.create_index("ix_email_log_dedupe_key", "email_log", ["dedupe_key"])
    # The app's startup create_all may already have made the (new, empty) table.
    if sa.inspect(op.get_bind()).has_table("agent_email_health"):
        return
    op.create_table(
        "agent_email_health",
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("mode", sa.String(12), nullable=False, server_default="normal"),
        sa.Column("warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("weekly_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_summary_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("held", postgresql.JSONB(), nullable=True),
        sa.Column("held_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("agent_email_health")
    op.drop_index("ix_email_log_dedupe_key", table_name="email_log")
    for c in ("dedupe_key", "clicked_at", "open_count", "opened_at", "tracked"):
        op.drop_column("email_log", c)
