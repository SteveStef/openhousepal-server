"""Add open house qr_dark_mode

Revision ID: e5f3a8b1c9d2
Revises: d8e21f6c4a13
Create Date: 2026-08-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f3a8b1c9d2'
down_revision = 'd8e21f6c4a13'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('open_house_events', sa.Column('qr_dark_mode', sa.Boolean(), server_default=sa.text('false'), nullable=False))


def downgrade() -> None:
    op.drop_column('open_house_events', 'qr_dark_mode')
