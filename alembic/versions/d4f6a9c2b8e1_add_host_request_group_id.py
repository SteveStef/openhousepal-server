"""add group_id to open_house_host_requests

Revision ID: d4f6a9c2b8e1
Revises: b7e2a5c8f1d3
Create Date: 2026-09-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4f6a9c2b8e1'
down_revision = 'b7e2a5c8f1d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('open_house_host_requests', sa.Column('group_id', sa.String(), nullable=True))
    op.create_index(op.f('ix_open_house_host_requests_group_id'), 'open_house_host_requests', ['group_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_open_house_host_requests_group_id'), table_name='open_house_host_requests')
    op.drop_column('open_house_host_requests', 'group_id')
