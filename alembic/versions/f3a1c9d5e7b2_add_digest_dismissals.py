"""add digest dismissals

Revision ID: f3a1c9d5e7b2
Revises: b15a8a1100e5
Create Date: 2026-08-17 14:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f3a1c9d5e7b2'
down_revision: Union[str, None] = 'b15a8a1100e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'digest_dismissals',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('item_id', sa.String(), nullable=False),
        sa.Column('dismissed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id', 'item_id', name='uq_digest_dismissals_owner_item'),
    )
    op.create_index(op.f('ix_digest_dismissals_owner_id'), 'digest_dismissals', ['owner_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_digest_dismissals_owner_id'), table_name='digest_dismissals')
    op.drop_table('digest_dismissals')
