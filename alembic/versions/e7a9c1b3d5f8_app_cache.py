"""A small key -> JSON store that survives restarts (user, 10/19 - "can we just load it once
ourselves first so nobody ever has a bad experience"). The IDX/town-page market numbers were kept
only in memory, so after every deploy the first visitor to a site waited ~16 s while they were
rebuilt. They're now also saved here and loaded back at startup (see app/services/app_cache.py).

Revision ID: e7a9c1b3d5f8
Revises: b3d5f7a9c1e2
Create Date: 2026-10-19 22:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'e7a9c1b3d5f8'
down_revision: Union[str, None] = 'b3d5f7a9c1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_cache',
        sa.Column('key', sa.String(), primary_key=True),
        sa.Column('value', postgresql.JSONB(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('app_cache')
