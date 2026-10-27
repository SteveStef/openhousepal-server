"""Keep every AI cost row: owner optional, kept when an agent is deleted

ai_usage_events is also the platform's record of what AI cost us (Admin > Costs, owner 10/27:
"site spend" was far under the real bills). Two holes closed here:
- AI work nobody signed in started (cheat-sheet proofread / downtown / school lookups run for a
  public page) had no owner to put on the row, so it was never written down at all - owner_id is
  now optional (NULL = the platform paid, no agent's usage bar).
- Deleting an agent deleted every cost row they ever made (ON DELETE CASCADE), so the all-time
  spend went down - now the rows stay with owner_id NULL.
No data is changed.

Revision ID: b3e5d7f9a2c4
Revises: f2a4c6e8b1d3
Create Date: 2026-10-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3e5d7f9a2c4"
down_revision: Union[str, None] = "f2a4c6e8b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("ai_usage_events", "owner_id", existing_type=sa.String(), nullable=True)
    op.drop_constraint("ai_usage_events_owner_id_fkey", "ai_usage_events", type_="foreignkey")
    op.create_foreign_key("ai_usage_events_owner_id_fkey", "ai_usage_events", "users", ["owner_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("ai_usage_events_owner_id_fkey", "ai_usage_events", type_="foreignkey")
    op.create_foreign_key("ai_usage_events_owner_id_fkey", "ai_usage_events", "users", ["owner_id"], ["id"], ondelete="CASCADE")
    op.execute("DELETE FROM ai_usage_events WHERE owner_id IS NULL")
    op.alter_column("ai_usage_events", "owner_id", existing_type=sa.String(), nullable=False)
