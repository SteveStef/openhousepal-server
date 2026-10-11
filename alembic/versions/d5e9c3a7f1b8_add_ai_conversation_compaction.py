"""add compaction fields to ai_conversations

Revision ID: d5e9c3a7f1b8
Revises: c4f8b7d2e6a1
Create Date: 2026-08-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5e9c3a7f1b8'
down_revision: Union[str, None] = 'c4f8b7d2e6a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ai_conversations', sa.Column('summary', sa.Text(), nullable=True))
    op.add_column('ai_conversations', sa.Column('summary_covers_through', sa.DateTime(timezone=True), nullable=True))
    op.add_column('ai_conversations', sa.Column('compaction_count', sa.Integer(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    op.drop_column('ai_conversations', 'compaction_count')
    op.drop_column('ai_conversations', 'summary_covers_through')
    op.drop_column('ai_conversations', 'summary')
