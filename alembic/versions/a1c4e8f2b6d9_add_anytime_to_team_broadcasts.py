"""add is_anytime to open_house_team_broadcasts, nullable times

Revision ID: a1c4e8f2b6d9
Revises: d4f6a9c2b8e1
Create Date: 2026-09-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1c4e8f2b6d9'
down_revision = 'd4f6a9c2b8e1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('open_house_team_broadcasts', sa.Column('is_anytime', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column('open_house_team_broadcasts', 'requested_start_time', existing_type=sa.Time(), nullable=True)
    op.alter_column('open_house_team_broadcasts', 'requested_end_time', existing_type=sa.Time(), nullable=True)


def downgrade() -> None:
    op.alter_column('open_house_team_broadcasts', 'requested_end_time', existing_type=sa.Time(), nullable=False)
    op.alter_column('open_house_team_broadcasts', 'requested_start_time', existing_type=sa.Time(), nullable=False)
    op.drop_column('open_house_team_broadcasts', 'is_anytime')
