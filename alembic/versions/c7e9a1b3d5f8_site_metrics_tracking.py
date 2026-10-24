"""Site metrics for the admin Insights pages

open_house_visitors.signin_seconds - how long the guest took, from first tapping the form to
                                     pressing Sign in (NULL = not measured, e.g. older rows)
open_house_visitors.signin_mode    - phone (their own) | kiosk (the agent's tablet)
open_house_visitors.signin_device  - mobile | tablet | desktop
open_house_signin_views.visitor_id - the sign-in this showing of the form turned into
open_house_signin_views.mode / .device - same words as above

email_log - one row per email OpenHousePal sends (any kind: agent alerts, client emails, account
            emails), with what Mailgun later said happened to it. Counts only: no address, no body.

New nullable columns and one new table - nothing existing is altered or backfilled.

Revision ID: c7e9a1b3d5f8
Revises: b5d7f9a1c3e6
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7e9a1b3d5f8"
down_revision: Union[str, None] = "b5d7f9a1c3e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("open_house_visitors", sa.Column("signin_seconds", sa.Integer(), nullable=True))
    op.add_column("open_house_visitors", sa.Column("signin_mode", sa.String(12), nullable=True))
    op.add_column("open_house_visitors", sa.Column("signin_device", sa.String(12), nullable=True))
    op.add_column("open_house_signin_views", sa.Column("visitor_id", sa.String(), nullable=True))
    op.add_column("open_house_signin_views", sa.Column("mode", sa.String(12), nullable=True))
    op.add_column("open_house_signin_views", sa.Column("device", sa.String(12), nullable=True))
    op.create_foreign_key("fk_signin_views_visitor", "open_house_signin_views", "open_house_visitors",
                          ["visitor_id"], ["id"], ondelete="SET NULL")

    op.create_table(
        "email_log",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("template", sa.String(80), nullable=False),
        sa.Column("audience", sa.String(12), nullable=False),   # agent | client | account
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("client_email_id", sa.String(), nullable=True),
        sa.Column("via", sa.String(16), nullable=False),        # openhousepal | mailbox
        sa.Column("status", sa.String(12), nullable=False),     # sent | failed | held (dev)
        sa.Column("provider_id", sa.String(255), nullable=True),
        sa.Column("delivery", sa.String(16), nullable=True),    # delivered | delayed | bounced | blocked | spam
        sa.Column("delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_email_log_created_at", "email_log", ["created_at"])
    op.create_index("ix_email_log_owner_id", "email_log", ["owner_id"])
    op.create_index("ix_email_log_provider_id", "email_log", ["provider_id"])


def downgrade() -> None:
    op.drop_index("ix_email_log_provider_id", table_name="email_log")
    op.drop_index("ix_email_log_owner_id", table_name="email_log")
    op.drop_index("ix_email_log_created_at", table_name="email_log")
    op.drop_table("email_log")
    op.drop_constraint("fk_signin_views_visitor", "open_house_signin_views", type_="foreignkey")
    op.drop_column("open_house_signin_views", "device")
    op.drop_column("open_house_signin_views", "mode")
    op.drop_column("open_house_signin_views", "visitor_id")
    op.drop_column("open_house_visitors", "signin_device")
    op.drop_column("open_house_visitors", "signin_mode")
    op.drop_column("open_house_visitors", "signin_seconds")
