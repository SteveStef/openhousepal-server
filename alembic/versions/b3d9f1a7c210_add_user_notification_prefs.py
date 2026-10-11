"""add user.notification_prefs

Revision ID: b3d9f1a7c210
Revises: f1a4c7e9b2d5
Create Date: 2026-09-08

Per-category email/push notification opt-outs, JSONB, nullable (NULL = all defaults / everything
on). The in-app bell is never gated by this column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b3d9f1a7c210'
down_revision: Union[str, None] = 'f1a4c7e9b2d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('notification_prefs', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'notification_prefs')
