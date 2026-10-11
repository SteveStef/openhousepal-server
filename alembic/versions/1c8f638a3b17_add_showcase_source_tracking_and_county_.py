"""add showcase source tracking and county state acres sqft preferences

Revision ID: 1c8f638a3b17
Revises: b3d8f2a5c1e7
Create Date: 2026-08-06 12:54:15.014761

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1c8f638a3b17'
down_revision: Union[str, None] = 'b3d8f2a5c1e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Note: autogenerate also picked up unrelated pre-existing drift (a leads index and an
    # open_house_listing_notes constraint/index) not touched by this change - intentionally
    # excluded here so this migration only does what its message says.
    op.add_column('collection_preferences', sa.Column('county', sa.String(), nullable=True))
    op.add_column('collection_preferences', sa.Column('state', sa.String(), nullable=True))
    op.add_column('collection_preferences', sa.Column('min_acres', sa.Float(), nullable=True))
    op.add_column('collection_preferences', sa.Column('max_acres', sa.Float(), nullable=True))
    op.add_column('collection_preferences', sa.Column('min_sqft', sa.Float(), nullable=True))
    op.add_column('collection_preferences', sa.Column('max_sqft', sa.Float(), nullable=True))
    op.add_column('collections', sa.Column('source', sa.String(), nullable=True))
    op.add_column('collections', sa.Column('source_form_submission_id', sa.String(), nullable=True))
    op.create_foreign_key(None, 'collections', 'form_submissions', ['source_form_submission_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    op.drop_constraint(None, 'collections', type_='foreignkey')
    op.drop_column('collections', 'source_form_submission_id')
    op.drop_column('collections', 'source')
    op.drop_column('collection_preferences', 'max_sqft')
    op.drop_column('collection_preferences', 'min_sqft')
    op.drop_column('collection_preferences', 'max_acres')
    op.drop_column('collection_preferences', 'min_acres')
    op.drop_column('collection_preferences', 'state')
    op.drop_column('collection_preferences', 'county')
