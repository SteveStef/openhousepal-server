"""Email opens from the agent's own Sent folder

client_emails.sent_folder_seen_at - the last time an email's tracking image loaded as part of the
agent scrolling their own Sent folder (emails sent from their Gmail / Outlook keep a copy there).
Those loads aren't counted as the client opening it (owner, 10/26). Nothing is backfilled here; the
app corrects past opens itself on start-up (client_email_service.correct_sent_folder_opens).

Revision ID: d8b4e6a2c1f3
Revises: c5f1a3d7e9b2
Create Date: 2026-10-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8b4e6a2c1f3"
down_revision: Union[str, None] = "c5f1a3d7e9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("client_emails", sa.Column("sent_folder_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("client_emails", "sent_folder_seen_at")
