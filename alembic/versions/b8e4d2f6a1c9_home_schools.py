"""home_schools table + collection_preferences.high_schools

Each for-sale home's high school (Bright's, else the government map / nearby listings), filled in the background
by services/home_school_service.py - a new small table, so the big properties table is never rewritten.

Revision ID: b8e4d2f6a1c9
Revises: f3c7a9e2b5d1
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b8e4d2f6a1c9"
down_revision = "f3c7a9e2b5d1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "home_schools",
        sa.Column("property_id", sa.String(), sa.ForeignKey("properties.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("district", sa.String(), nullable=True),
        sa.Column("hs_key", sa.String(), nullable=True),
        sa.Column("hs_name", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("sure", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("bright_hs", sa.String(), nullable=True),
        sa.Column("bright_norm", sa.String(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quick", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_home_schools_hs_key", "home_schools", ["hs_key"])
    # a new listing's quick look: what its district's spellings mean
    op.create_index("ix_home_schools_spelling", "home_schools", ["state", "district", "bright_norm"])
    op.add_column("collection_preferences", sa.Column("high_schools", postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column("collection_preferences", "high_schools")
    op.drop_index("ix_home_schools_spelling", table_name="home_schools")
    op.drop_index("ix_home_schools_hs_key", table_name="home_schools")
    op.drop_table("home_schools")
