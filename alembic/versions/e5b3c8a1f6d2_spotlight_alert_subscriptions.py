"""spotlight alert subscriptions (guest price / open house alerts)

Revision ID: e5b3c8a1f6d2
Revises: d4f2a7c9e1b3
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5b3c8a1f6d2"
down_revision: Union[str, None] = "d4f2a7c9e1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "spotlight_alert_subscriptions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("property_website_id", sa.String(), sa.ForeignKey("property_websites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lead_id", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("token", sa.String(), nullable=False, unique=True),
        sa.Column("last_price", sa.Float(), nullable=True),
        sa.Column("last_state", postgresql.JSONB(), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_spotlight_alert_subscriptions_property_website_id", "spotlight_alert_subscriptions", ["property_website_id"])
    op.create_index("ix_spotlight_alert_subscriptions_owner_id", "spotlight_alert_subscriptions", ["owner_id"])
    op.create_index("ux_spotlight_alert_site_email", "spotlight_alert_subscriptions", ["property_website_id", "email"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_spotlight_alert_site_email", table_name="spotlight_alert_subscriptions")
    op.drop_index("ix_spotlight_alert_subscriptions_owner_id", table_name="spotlight_alert_subscriptions")
    op.drop_index("ix_spotlight_alert_subscriptions_property_website_id", table_name="spotlight_alert_subscriptions")
    op.drop_table("spotlight_alert_subscriptions")
