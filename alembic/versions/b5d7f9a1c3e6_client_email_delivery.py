"""Client emails: what Mailgun says happened after sending (webhooks)

client_emails.delivery      - delivered | delayed | bounced | blocked | spam (NULL = no word yet,
                              or sent from the agent's own mailbox)
client_emails.delivery_note - the plain reason ("That address doesn't exist", ...)
client_emails.delivery_at   - when Mailgun reported it

Three nullable columns only - nothing existing is altered or backfilled.

Revision ID: b5d7f9a1c3e6
Revises: a3c5e7f9b1d4
Create Date: 2026-10-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5d7f9a1c3e6"
down_revision: Union[str, None] = "a3c5e7f9b1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("client_emails", sa.Column("delivery", sa.String(16), nullable=True))
    op.add_column("client_emails", sa.Column("delivery_note", sa.String(300), nullable=True))
    op.add_column("client_emails", sa.Column("delivery_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("client_emails", "delivery_at")
    op.drop_column("client_emails", "delivery_note")
    op.drop_column("client_emails", "delivery")
