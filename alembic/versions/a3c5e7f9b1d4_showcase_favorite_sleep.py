"""Showcases: agent favorites + why / when a Showcase went to sleep

collections.favorited_at  - the agent starred it (sorts first on the Showcases page)
collections.inactive_reason - why it's INACTIVE: 'no_response' (client ignored 3 weekly emails -
                              wakes by itself when they open their Showcase), 'agent' (put to
                              sleep by hand), NULL for older rows
collections.inactive_at   - when it went to sleep

Three nullable columns only - nothing existing is altered or backfilled.

Revision ID: a3c5e7f9b1d4
Revises: f8c3a1d5e9b2
Create Date: 2026-10-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3c5e7f9b1d4"
down_revision: Union[str, None] = "f8c3a1d5e9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("collections", sa.Column("favorited_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("collections", sa.Column("inactive_reason", sa.String(24), nullable=True))
    op.add_column("collections", sa.Column("inactive_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("collections", "inactive_at")
    op.drop_column("collections", "inactive_reason")
    op.drop_column("collections", "favorited_at")
