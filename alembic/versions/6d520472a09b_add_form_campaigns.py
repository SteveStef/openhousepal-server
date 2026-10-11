"""add form campaigns

Revision ID: 6d520472a09b
Revises: 0793fc4c2145
Create Date: 2026-08-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6d520472a09b"
down_revision: Union[str, None] = "0793fc4c2145"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "form_campaigns",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("form_id", sa.String(), sa.ForeignKey("agent_forms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("form_id", "token", name="uq_form_campaigns_form_token"),
    )
    op.create_index("ix_form_campaigns_form_id", "form_campaigns", ["form_id"])
    op.create_index("ix_form_campaigns_owner_id", "form_campaigns", ["owner_id"])

    op.add_column(
        "form_view_sessions",
        sa.Column("campaign_id", sa.String(), sa.ForeignKey("form_campaigns.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_form_view_sessions_campaign_id", "form_view_sessions", ["campaign_id"])

    op.add_column(
        "form_submissions",
        sa.Column("campaign_id", sa.String(), sa.ForeignKey("form_campaigns.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_form_submissions_campaign_id", "form_submissions", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_form_submissions_campaign_id", table_name="form_submissions")
    op.drop_column("form_submissions", "campaign_id")
    op.drop_index("ix_form_view_sessions_campaign_id", table_name="form_view_sessions")
    op.drop_column("form_view_sessions", "campaign_id")
    op.drop_index("ix_form_campaigns_owner_id", table_name="form_campaigns")
    op.drop_index("ix_form_campaigns_form_id", table_name="form_campaigns")
    op.drop_table("form_campaigns")
