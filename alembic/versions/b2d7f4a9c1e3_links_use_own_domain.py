"""users.links_use_own_domain - IDX agents choose whether new links use their own domain

Revision ID: b2d7f4a9c1e3
Revises: f8a1c3e5d7b9
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'b2d7f4a9c1e3'
down_revision: Union[str, None] = 'f8a1c3e5d7b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # On by default - the behavior IDX agents already had (their links use their own domain).
    op.add_column('users', sa.Column('links_use_own_domain', sa.Boolean(), nullable=False, server_default='true'))


def downgrade() -> None:
    op.drop_column('users', 'links_use_own_domain')
