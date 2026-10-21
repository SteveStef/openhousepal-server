"""property_field_changes - every field a Bright sync changed on a listing (old -> new)

Bright only ever sends a listing's current values, so this log is the only record of what a listing
said before (a fixed square footage, a changed agent, a new photo count...). Diffs only - one short
row per changed field - so it can be kept forever. Filled from the sync from now on; no backfill.

New table only - no change to any existing table.

Revision ID: c4f6a8b0d2e1
Revises: b3e5a7c9d1f4
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c4f6a8b0d2e1'
down_revision: Union[str, None] = 'b3e5a7c9d1f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'property_field_changes',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('listing_key', sa.String(), nullable=False),
        sa.Column('field', sa.String(), nullable=False),
        sa.Column('old_value', sa.Text(), nullable=True),
        sa.Column('new_value', sa.Text(), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_property_field_changes_listing_key', 'property_field_changes', ['listing_key', 'observed_at'])


def downgrade() -> None:
    op.drop_index('ix_property_field_changes_listing_key', table_name='property_field_changes')
    op.drop_table('property_field_changes')
