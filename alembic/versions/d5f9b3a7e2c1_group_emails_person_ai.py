"""Group email list (people join a brokerage group when they sign up), per-person AI limit inside a
group, and "this payment replaces that one" on payment requests

Revision ID: d5f9b3a7e2c1
Revises: c4e8a2f6d1b9
Create Date: 2026-11-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d5f9b3a7e2c1"
down_revision: Union[str, None] = "c4e8a2f6d1b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("ai_budget_override_usd", sa.Float(), nullable=True))
    op.add_column("payment_requests", sa.Column("replaces_request_id", sa.String(), nullable=True))
    op.create_table(
        "billing_group_emails",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.String(), sa.ForeignKey("billing_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("apply_plan", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ux_billing_group_emails_email", "billing_group_emails", ["email"], unique=True)
    op.create_index("ix_billing_group_emails_group_id", "billing_group_emails", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_billing_group_emails_group_id", table_name="billing_group_emails")
    op.drop_index("ux_billing_group_emails_email", table_name="billing_group_emails")
    op.drop_table("billing_group_emails")
    op.drop_column("payment_requests", "replaces_request_id")
    op.drop_column("users", "ai_budget_override_usd")
