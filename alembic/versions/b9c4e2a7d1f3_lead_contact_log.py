"""Leads: how the agent reached out - texted / emailed and what it was (owner, 11/02)

- leads.contact_log: [{"at", "via": "text"|"email"|"call"|null, "what", "task"}], oldest first.
  Shown as "Texted" / "Emailed" on the Visitors list and the Leads page.

Nullable with no default, so adding it never rewrites the table.

Revision ID: b9c4e2a7d1f3
Revises: a7d3f1c9e2b4
Create Date: 2026-11-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b9c4e2a7d1f3"
down_revision: Union[str, None] = "a7d3f1c9e2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("contact_log", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "contact_log")
