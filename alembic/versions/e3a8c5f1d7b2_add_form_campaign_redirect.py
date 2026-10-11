"""form_campaigns.redirect_mode / redirect_url - where a campaign's visitors go after they submit

NULL redirect_mode = follow the form's own "Redirect to Agent Hub" switch (every existing campaign
keeps behaving exactly as before).

Revision ID: e3a8c5f1d7b2
Revises: d9b4e2a7c1f3
Create Date: 2026-10-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'e3a8c5f1d7b2'
down_revision = 'd9b4e2a7c1f3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('form_campaigns', sa.Column('redirect_mode', sa.String(length=20), nullable=True))
    op.add_column('form_campaigns', sa.Column('redirect_url', sa.String(length=500), nullable=True))


def downgrade():
    op.drop_column('form_campaigns', 'redirect_url')
    op.drop_column('form_campaigns', 'redirect_mode')
