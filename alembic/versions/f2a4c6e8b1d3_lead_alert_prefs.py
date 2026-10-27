"""Per-lead alert settings

leads.alert_prefs - the agent's own switches for ONE person (owner, 10/27): {"calendar": bool,
"in_app": bool, "emails": bool}. Missing key or NULL = on. Calendar off keeps their follow-ups in
OpenHousePal but off the calendar; in_app off stops their automatic follow-ups altogether; emails
off stops the activity alert emails about them. Nothing is backfilled (NULL = everything on).

Revision ID: f2a4c6e8b1d3
Revises: d8b4e6a2c1f3
Create Date: 2026-10-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2a4c6e8b1d3"
down_revision: Union[str, None] = "d8b4e6a2c1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("alert_prefs", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("leads", "alert_prefs")
