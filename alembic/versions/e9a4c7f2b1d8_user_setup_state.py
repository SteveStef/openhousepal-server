"""One-time setup flow: remember where the agent is and what they skipped.

users.setup_state = {"last_step": "<id>", "skipped": [...], "confirmed": [...]}. Everything the
flow asks for is stored where the features already read it (Agent Hub config, User, Brand Kit,
signature, site...) - this column only holds the flow's own bookkeeping.

Revision ID: e9a4c7f2b1d8
Revises: a3b7d2e9c5f1
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e9a4c7f2b1d8'
down_revision = 'a3b7d2e9c5f1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users', sa.Column('setup_state', postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column('users', 'setup_state')
