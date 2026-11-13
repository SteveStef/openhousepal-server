"""Client email send time: the client picks when their Showcase emails arrive (owner, 11/11)

Revision ID: f7b2d4a6c8e1
Revises: e6a1c3f5b7d9
Create Date: 2026-11-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f7b2d4a6c8e1"
down_revision: Union[str, None] = "e6a1c3f5b7d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("client_email_prefs", sa.Column("send_time", sa.String(length=5), nullable=True))
    op.add_column("client_email_prefs", sa.Column("client_notice", sa.String(length=10), nullable=True))
    op.add_column("client_email_prefs", sa.Column("client_notice_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("client_email_prefs", "client_notice_at")
    op.drop_column("client_email_prefs", "client_notice")
    op.drop_column("client_email_prefs", "send_time")
