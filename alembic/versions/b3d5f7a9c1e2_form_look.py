"""Forms wear the Agent Hub's looks (user, 10/18 - "make forms colors customizable just like agent
hub, same colors and themes"): each form either matches the agent's Agent Hub (the default) or has
its own Classic colour / Luxe finish, light or dark. Forms someone already re-coloured or set to
dark keep exactly that as their own Classic look; every untouched form now matches the hub.

Revision ID: b3d5f7a9c1e2
Revises: a2c4e6f8b0d1
Create Date: 2026-10-18 23:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'b3d5f7a9c1e2'
down_revision: Union[str, None] = 'a2c4e6f8b0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('agent_forms', sa.Column('look_mode', sa.String(), nullable=False, server_default='hub'))
    op.add_column('agent_forms', sa.Column('look_theme', sa.String(), nullable=True))
    op.add_column('agent_forms', sa.Column('luxe_metal', sa.String(), nullable=True))
    op.execute(
        "UPDATE agent_forms SET look_mode = 'custom', look_theme = 'classic' "
        "WHERE dark_mode IS TRUE OR (accent_color IS NOT NULL AND lower(accent_color) <> '#816937')"
    )


def downgrade() -> None:
    op.drop_column('agent_forms', 'luxe_metal')
    op.drop_column('agent_forms', 'look_theme')
    op.drop_column('agent_forms', 'look_mode')
