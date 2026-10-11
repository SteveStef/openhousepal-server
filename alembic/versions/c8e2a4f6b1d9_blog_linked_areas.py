"""blog linked IDX neighborhood pages

Revision ID: c8e2a4f6b1d9
Revises: a8d3f1c6b2e9
Create Date: 2026-10-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c8e2a4f6b1d9'
down_revision = 'a8d3f1c6b2e9'
branch_labels = None
depends_on = None


def upgrade():
    # Area slugs of the IDX neighborhood pages an article links to (app/services/blog_neighborhoods.py).
    op.add_column('blogs', sa.Column('linked_areas', postgresql.JSONB(), nullable=False, server_default='[]'))


def downgrade():
    op.drop_column('blogs', 'linked_areas')
