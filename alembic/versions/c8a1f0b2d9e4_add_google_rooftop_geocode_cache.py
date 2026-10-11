"""add google rooftop geocode cache

Revision ID: c8a1f0b2d9e4
Revises: b3e8a7d2c4f9
Create Date: 2026-08-03 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8a1f0b2d9e4"
down_revision: Union[str, None] = "b3e8a7d2c4f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("properties", sa.Column("google_rooftop_latitude", sa.Float(), nullable=True))
    op.add_column("properties", sa.Column("google_rooftop_longitude", sa.Float(), nullable=True))
    op.add_column("properties", sa.Column("google_geocode_location_type", sa.String(), nullable=True))
    op.add_column("properties", sa.Column("google_geocoded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("properties", "google_geocoded_at")
    op.drop_column("properties", "google_geocode_location_type")
    op.drop_column("properties", "google_rooftop_longitude")
    op.drop_column("properties", "google_rooftop_latitude")
