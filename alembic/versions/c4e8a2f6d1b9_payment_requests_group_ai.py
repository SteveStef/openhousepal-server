"""Payment requests (custom card pay links, one time or recurring) + group AI usage limits

Revision ID: c4e8a2f6d1b9
Revises: b9d2e6f1a3c7
Create Date: 2026-11-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c4e8a2f6d1b9"
down_revision: Union[str, None] = "b9d2e6f1a3c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("billing_groups", sa.Column("ai_budget_per_agent_usd", sa.Float(), nullable=True))
    op.add_column("billing_groups", sa.Column("ai_budget_total_usd", sa.Float(), nullable=True))
    op.create_table(
        "payment_requests",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("cycle", sa.String(), nullable=False, server_default="monthly"),
        sa.Column("total_payments", sa.Integer(), nullable=True),
        sa.Column("billing_group_id", sa.String(), sa.ForeignKey("billing_groups.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payer_name", sa.String(), nullable=True),
        sa.Column("payer_email", sa.String(), nullable=True),
        sa.Column("plan_tier", sa.String(), nullable=True),
        sa.Column("seats", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("paypal_plan_id", sa.String(), nullable=True),
        sa.Column("paypal_subscription_id", sa.String(), nullable=True),
        sa.Column("payments_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("amount_paid_total", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_charge_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_payment_requests_token", "payment_requests", ["token"], unique=True)
    op.create_index("ix_payment_requests_billing_group_id", "payment_requests", ["billing_group_id"])
    op.create_index("ix_payment_requests_user_id", "payment_requests", ["user_id"])
    op.create_index("ix_payment_requests_paypal_subscription_id", "payment_requests", ["paypal_subscription_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_requests_paypal_subscription_id", table_name="payment_requests")
    op.drop_index("ix_payment_requests_user_id", table_name="payment_requests")
    op.drop_index("ix_payment_requests_billing_group_id", table_name="payment_requests")
    op.drop_index("ix_payment_requests_token", table_name="payment_requests")
    op.drop_table("payment_requests")
    op.drop_column("billing_groups", "ai_budget_total_usd")
    op.drop_column("billing_groups", "ai_budget_per_agent_usd")
