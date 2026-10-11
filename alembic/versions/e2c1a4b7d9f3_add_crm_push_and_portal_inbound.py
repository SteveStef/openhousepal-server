"""add CRM push + portal inbound fields to users

Revision ID: e2c1a4b7d9f3
Revises: d1f6a2c8e470
Create Date: 2026-09-08 18:00:00.000000

Two lead-plumbing features share this migration:
  - CRM push  : the agent forwards every new lead to their real CRM (FollowUpBoss / Lofty /
                kvCORE / etc.) via a webhook URL and/or that CRM's lead-parsing email address.
  - Portal in : an opaque per-agent inbound token so a unique address
                (leads-<token>@<inbound domain>) can receive forwarded Zillow / Realtor.com /
                Homes.com / Facebook lead emails and turn them into OpenHousePal leads.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2c1a4b7d9f3"
down_revision: Union[str, None] = "d1f6a2c8e470"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("crm_push_enabled", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("users", sa.Column("crm_webhook_url", sa.String(), nullable=True))
    op.add_column("users", sa.Column("crm_parse_email", sa.String(), nullable=True))
    op.add_column("users", sa.Column("crm_last_push_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("crm_last_push_status", sa.String(), nullable=True))
    op.add_column("users", sa.Column("inbound_lead_token", sa.String(), nullable=True))
    op.create_index("ix_users_inbound_lead_token", "users", ["inbound_lead_token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_inbound_lead_token", table_name="users")
    op.drop_column("users", "inbound_lead_token")
    op.drop_column("users", "crm_last_push_status")
    op.drop_column("users", "crm_last_push_at")
    op.drop_column("users", "crm_parse_email")
    op.drop_column("users", "crm_webhook_url")
    op.drop_column("users", "crm_push_enabled")
