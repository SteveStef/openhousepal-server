"""blogs.luxe_metal - the Luxe article theme's finish (gold / silver / bronze)

Revision ID: b9d3f6a2c8e4
Revises: a4c7e2f9b1d3
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b9d3f6a2c8e4"
down_revision: Union[str, None] = "a4c7e2f9b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("blogs", sa.Column("luxe_metal", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("blogs", "luxe_metal")
