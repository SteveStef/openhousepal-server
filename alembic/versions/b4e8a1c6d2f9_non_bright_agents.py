"""Agents outside Bright MLS can sign up

They have no Bright MLS ID (users.mls_id becomes nullable) and give a license number instead.
Re-runnable.

Revision ID: b4e8a1c6d2f9
Revises: a3d9e2f7c5b1
Create Date: 2026-10-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b4e8a1c6d2f9"
down_revision: Union[str, None] = "a3d9e2f7c5b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN mls_id DROP NOT NULL")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS license_number VARCHAR")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS license_number")
