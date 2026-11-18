"""Agent Hubs can be switched off (owner, 11/18: "people just don't want the feature"): agent_hubs.is_public,
on by default. Off = the Hub's own page, Sales/Listings pages and Save Contact card don't open (404, out of
Google) and nothing links to it; forms, reviews and blogs keep working. One column with a default -
instant, every existing Hub stays on.

Revision ID: f3c7a9e2b5d1
Revises: e2b6d4f8a1c3
Create Date: 2026-11-18
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f3c7a9e2b5d1"
down_revision: Union[str, None] = "e2b6d4f8a1c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_hubs ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_hubs DROP COLUMN IF EXISTS is_public")
