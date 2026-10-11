"""IDX setup fee: remember who has paid (or had waived) the one-time fee

Everyone on IDX now, or who ever was (they have an IDX site saved), counts as settled - coming
back on IDX never charges it again. Re-runnable.

Revision ID: d7b2e5f1a9c3
Revises: c6f1d8a3e2b7
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "d7b2e5f1a9c3"
down_revision: Union[str, None] = "c6f1d8a3e2b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS idx_setup_settled_at TIMESTAMPTZ")
    op.execute("UPDATE users SET idx_setup_settled_at = now() WHERE plan_tier = 'IDX' AND idx_setup_settled_at IS NULL")
    op.execute("UPDATE users SET idx_setup_settled_at = now() WHERE idx_setup_settled_at IS NULL AND id IN (SELECT owner_id FROM sites)")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS idx_setup_settled_at")
