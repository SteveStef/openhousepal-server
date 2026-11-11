"""Offers: a private sign-up offer page (percent off chosen plans, IDX fee waiver, trial on/off,
spots + end date) with its own PayPal plans

Revision ID: e6a1c3f5b7d9
Revises: d5f9b3a7e2c1
Create Date: 2026-11-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e6a1c3f5b7d9"
down_revision: Union[str, None] = "d5f9b3a7e2c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "offers",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("headline", sa.String(), nullable=True),
        sa.Column("duration", sa.String(), nullable=False, server_default="forever"),
        sa.Column("months", sa.Integer(), nullable=True),
        sa.Column("trial", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("waive_idx_fee", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_offers_token", "offers", ["token"], unique=True)
    op.create_table(
        "offer_plans",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("offer_id", sa.String(), sa.ForeignKey("offers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tier", sa.String(), nullable=False),
        sa.Column("percent_off", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("base_price", sa.Float(), nullable=False),
        sa.Column("offer_price", sa.Float(), nullable=False),
        sa.Column("setup_fee", sa.Float(), nullable=False, server_default="0"),
        sa.Column("trial_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paypal_plan_id", sa.String(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_offer_plans_offer_id", "offer_plans", ["offer_id"])
    op.create_index("ix_offer_plans_paypal_plan_id", "offer_plans", ["paypal_plan_id"], unique=True)
    op.add_column("users", sa.Column("offer_id", sa.String(), sa.ForeignKey("offers.id", ondelete="SET NULL"), nullable=True))
    op.create_index("ix_users_offer_id", "users", ["offer_id"])


def downgrade() -> None:
    op.drop_index("ix_users_offer_id", table_name="users")
    op.drop_column("users", "offer_id")
    op.drop_index("ix_offer_plans_paypal_plan_id", table_name="offer_plans")
    op.drop_index("ix_offer_plans_offer_id", table_name="offer_plans")
    op.drop_table("offer_plans")
    op.drop_index("ix_offers_token", table_name="offers")
    op.drop_table("offers")
