"""Calendar: remember where the app last put each event (owner, 11/01)

- calendar_event_links.pushed_start: the start OpenHousePal last sent ("2026-11-03T15:00" UTC, or
  "date:2026-11-03" for an all-day one). When the event on the agent's calendar starts somewhere
  else, they moved it there - the follow-up's day follows it (Follow-ups, the Dash, the briefing)
  instead of the app moving it back on its next update.

Nullable with no default, so adding it never rewrites the table.

Revision ID: e5c9a1f3b7d2
Revises: d4a8c2e6f9b1
Create Date: 2026-11-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5c9a1f3b7d2"
down_revision: Union[str, None] = "d4a8c2e6f9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("calendar_event_links", sa.Column("pushed_start", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("calendar_event_links", "pushed_start")
