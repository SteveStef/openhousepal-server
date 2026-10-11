"""Brokerage billing groups: agents paid for by their brokerage on a custom deal (PayPal invoice)

Revision ID: a3d5e7f9b1c4
Revises: f9c3a7e1b5d2
Create Date: 2026-10-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a3d5e7f9b1c4"
down_revision: Union[str, None] = "f9c3a7e1b5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "billing_groups",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("brokerage", sa.String(), nullable=True),
        sa.Column("contact_name", sa.String(), nullable=True),
        sa.Column("contact_email", sa.String(), nullable=True),
        sa.Column("contact_phone", sa.String(), nullable=True),
        sa.Column("paypal_reference", sa.String(), nullable=True),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("billing_cycle", sa.String(), nullable=False, server_default="monthly"),
        sa.Column("seats", sa.Integer(), nullable=True),
        sa.Column("plan_tier", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("started_on", sa.Date(), nullable=True),
        sa.Column("next_invoice_on", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("users", sa.Column("billing_group_id", sa.String(), sa.ForeignKey("billing_groups.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_users_billing_group_id", "users", ["billing_group_id"])


def downgrade() -> None:
    op.drop_index("ix_users_billing_group_id", table_name="users")
    op.drop_column("users", "billing_group_id")
    op.drop_table("billing_groups")
