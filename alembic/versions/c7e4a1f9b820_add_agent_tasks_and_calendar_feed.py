"""add agent_tasks + users.calendar_feed_token

Revision ID: c7e4a1f9b820
Revises: b3d9f1a7c210
Create Date: 2026-09-08 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c7e4a1f9b820"
down_revision: Union[str, None] = "b3d9f1a7c210"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("calendar_feed_token", sa.String(), nullable=True))
    op.create_unique_constraint("uq_users_calendar_feed_token", "users", ["calendar_feed_token"])
    op.create_index("ix_users_calendar_feed_token", "users", ["calendar_feed_token"], unique=False)

    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("lead_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False, server_default="general"),
        sa.Column("priority", sa.String(), nullable=False, server_default="normal"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_source", sa.String(), nullable=True),
        sa.Column("auto_source_id", sa.String(), nullable=True),
        sa.Column("suggested_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "auto_source", "auto_source_id", name="uq_agent_tasks_auto_dedup"),
    )
    op.create_index("ix_agent_tasks_owner_id", "agent_tasks", ["owner_id"], unique=False)
    op.create_index("ix_agent_tasks_lead_id", "agent_tasks", ["lead_id"], unique=False)
    op.create_index("ix_agent_tasks_due_at", "agent_tasks", ["due_at"], unique=False)
    op.create_index("ix_agent_tasks_remind_at", "agent_tasks", ["remind_at"], unique=False)
    op.create_index("ix_agent_tasks_completed_at", "agent_tasks", ["completed_at"], unique=False)
    op.create_index("ix_agent_tasks_auto_source_id", "agent_tasks", ["auto_source_id"], unique=False)
    op.create_index("ix_agent_tasks_owner_open", "agent_tasks", ["owner_id", "completed_at"], unique=False)


def downgrade() -> None:
    op.drop_table("agent_tasks")
    op.drop_index("ix_users_calendar_feed_token", table_name="users")
    op.drop_constraint("uq_users_calendar_feed_token", "users", type_="unique")
    op.drop_column("users", "calendar_feed_token")
