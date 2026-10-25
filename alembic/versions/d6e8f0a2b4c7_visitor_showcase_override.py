"""Per-visitor Showcase switch

open_house_visitors.showcase_override - the agent's Showcase on/off for one guest in the Visitors
window: True = gets a Showcase though the kit's Showcases are off, False = doesn't though they're on,
NULL = follows the kit. One nullable column, nothing backfilled.

Revision ID: d6e8f0a2b4c7
Revises: b7d9f1a3c5e8
Create Date: 2026-10-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6e8f0a2b4c7"
down_revision: Union[str, None] = "b7d9f1a3c5e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("open_house_visitors", sa.Column("showcase_override", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("open_house_visitors", "showcase_override")
