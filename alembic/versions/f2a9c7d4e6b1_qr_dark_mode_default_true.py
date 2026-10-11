"""Default qr_dark_mode to true for new open houses

Revision ID: f2a9c7d4e6b1
Revises: e5f3a8b1c9d2
Create Date: 2026-08-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'f2a9c7d4e6b1'
down_revision = 'e5f3a8b1c9d2'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('open_house_events', 'qr_dark_mode', server_default=sa.text('true'))


def downgrade():
    op.alter_column('open_house_events', 'qr_dark_mode', server_default=sa.text('false'))
