"""Per-open-house guest settings: auto_showcase (build a Showcase for each unrepresented guest -
a listing agent sitting their own listing can turn it off), guest_pass (after signing in, a guest's
phone opens their Guest Pass for this open house - Bright MLS plans), and after_signin (the No-MLS
Pro plan's choice instead: open the agent's Agent Hub, or nothing).

Revision ID: f8a1c3e5d7b9
Revises: e7f4a0c3b2d6
Create Date: 2026-09-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f8a1c3e5d7b9'
down_revision: Union[str, None] = 'e7f4a0c3b2d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('open_house_events', sa.Column('auto_showcase', sa.Boolean(), nullable=False, server_default='true'))
    op.add_column('open_house_events', sa.Column('guest_pass', sa.Boolean(), nullable=False, server_default='true'))
    # "hub" | "none"; NULL = nothing.
    op.add_column('open_house_events', sa.Column('after_signin', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('open_house_events', 'after_signin')
    op.drop_column('open_house_events', 'guest_pass')
    op.drop_column('open_house_events', 'auto_showcase')
