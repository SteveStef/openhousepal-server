"""Send a home from a Showcase - the agent's saved message + mail app, the intro-texted mark,
and homes the agent took out of a Showcase

- users.home_send_prefs: {"message": str|None, "mail_app": "gmail"|"outlook"|"default"|None}
- collections.home_intro_texted_at: when the agent first texted this client the intro message
  with a home link - later texts start without it (just the links).
- property_interactions.agent_removed_at: the agent removed the home from this Showcase; the
  search never puts it back (adding it by hand from the home's page does).

All nullable with no default, so adding them never rewrites a table.

Revision ID: e5a9c2f7d4b1
Revises: d7e1b4a9c3f2
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a9c2f7d4b1"
down_revision: Union[str, None] = "d7e1b4a9c3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("home_send_prefs", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("collections", sa.Column("home_intro_texted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("property_interactions", sa.Column("agent_removed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("property_interactions", "agent_removed_at")
    op.drop_column("collections", "home_intro_texted_at")
    op.drop_column("users", "home_send_prefs")
