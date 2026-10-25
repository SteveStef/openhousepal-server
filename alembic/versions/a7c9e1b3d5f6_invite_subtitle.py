"""Invite flyer line 2

event_invites.subtitle - the agent's own "line 2" under the big title (owner, 10/24 round 8:
line 1 = their message, line 2 = e.g. the address they type). NULL = the automatic line (the
town / place); '' = no line 2 at all. One nullable column, nothing backfilled.

Revision ID: a7c9e1b3d5f6
Revises: e9f1a3b5c7d2
Create Date: 2026-10-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e1b3d5f6"
down_revision: Union[str, None] = "e9f1a3b5c7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("event_invites", sa.Column("subtitle", sa.String(160), nullable=True))


def downgrade() -> None:
    op.drop_column("event_invites", "subtitle")
