"""add tool_names to ai_usage_events

Revision ID: c4f8b7d2e6a1
Revises: a7c3e9f1b2d4
Create Date: 2026-08-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'c4f8b7d2e6a1'
down_revision: Union[str, None] = 'a7c3e9f1b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ai_usage_events', sa.Column('tool_names', JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('ai_usage_events', 'tool_names')
