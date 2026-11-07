"""How long a guest stayed on a home on their Guest Pass, when the agent last looked at their activity

Revision ID: f3c8a1d6b2e9
Revises: e7b2c9a4d1f3
"""
from alembic import op
import sqlalchemy as sa

revision = "f3c8a1d6b2e9"
down_revision = "e7b2c9a4d1f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("guest_pass_events", sa.Column("seconds", sa.Integer(), nullable=True))
    op.add_column("open_house_visitors", sa.Column("pass_seen_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("open_house_visitors", "pass_seen_at")
    op.drop_column("guest_pass_events", "seconds")
