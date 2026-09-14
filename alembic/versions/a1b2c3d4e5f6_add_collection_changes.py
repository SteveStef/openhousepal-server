"""add_collection_changes

Revision ID: a1b2c3d4e5f6
Revises: 36811bfbee6a
Create Date: 2026-09-13 00:00:00.000000

Idempotent: the table may already exist from Base.metadata.create_all (init_db runs it
on app startup). We therefore use IF NOT EXISTS and (re)create the partial unique index
explicitly so the schema is correct regardless of how the table first appeared.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '36811bfbee6a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS collection_changes (
            id VARCHAR NOT NULL PRIMARY KEY,
            collection_id VARCHAR NOT NULL REFERENCES collections (id) ON DELETE CASCADE,
            property_id VARCHAR NOT NULL REFERENCES properties (id),
            change_type VARCHAR NOT NULL,
            old_price DOUBLE PRECISION,
            new_price DOUBLE PRECISION,
            notified_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
        )
    """)

    op.execute("CREATE INDEX IF NOT EXISTS ix_collection_changes_collection_id ON collection_changes (collection_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_collection_changes_property_id ON collection_changes (property_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_collection_changes_notified_at ON collection_changes (notified_at)")

    # Enforce at most one OPEN (un-notified) row per (collection, property).
    # Drop any prior non-partial variant first (it may exist as a constraint or a plain
    # index depending on how the table was first created) so this is authoritative.
    op.execute("ALTER TABLE collection_changes DROP CONSTRAINT IF EXISTS uq_collection_change_open")
    op.execute("DROP INDEX IF EXISTS uq_collection_change_open")
    op.execute("""
        CREATE UNIQUE INDEX uq_collection_change_open
        ON collection_changes (collection_id, property_id)
        WHERE notified_at IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_collection_change_open")
    op.execute("DROP INDEX IF EXISTS ix_collection_changes_notified_at")
    op.execute("DROP INDEX IF EXISTS ix_collection_changes_property_id")
    op.execute("DROP INDEX IF EXISTS ix_collection_changes_collection_id")
    op.execute("DROP TABLE IF EXISTS collection_changes")
