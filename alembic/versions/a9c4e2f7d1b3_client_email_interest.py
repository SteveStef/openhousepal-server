"""Client email interest: which homes an email was about, when it was last opened / clicked, and
when the agent cleared its "worth a check-in" nudge (owner, 11/13)

Revision ID: a9c4e2f7d1b3
Revises: 05333178db78
Create Date: 2026-11-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a9c4e2f7d1b3"
down_revision: Union[str, None] = "05333178db78"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # All nullable, no backfill - instant on a big table.
    op.add_column("client_emails", sa.Column("about", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("client_emails", sa.Column("last_engaged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("client_emails", sa.Column("nudge_cleared_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("client_emails", "nudge_cleared_at")
    op.drop_column("client_emails", "last_engaged_at")
    op.drop_column("client_emails", "about")
