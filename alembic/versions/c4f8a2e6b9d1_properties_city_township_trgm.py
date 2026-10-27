"""properties.city / township trigram indexes - fast town pages and town search

Every town lookup (IDX town pages and their homes, search by town, market stats, CMA city mode)
matches the town name with ILIKE on city or township. A plain btree index can't serve ILIKE, so
each one read the whole properties table - 1.2 GB locally; on live a cold town page took 8-30 s.
A GIN trigram index serves ILIKE (with or without wildcards), so no query code changes.

Index only: built CONCURRENTLY so the MLS sync keeps writing while it builds, IF NOT EXISTS so a
re-run is harmless. The properties table itself is never rewritten.

Revision ID: c4f8a2e6b9d1
Revises: b3e5d7f9a2c4
Create Date: 2026-10-27
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c4f8a2e6b9d1"
down_revision: Union[str, None] = "b3e5d7f9a2c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEXES = (("ix_properties_city_trgm", "city"), ("ix_properties_township_trgm", "township"))


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for name, column in INDEXES:
            # A CONCURRENTLY build that was interrupted leaves an INVALID index behind, which IF NOT
            # EXISTS would then skip forever - clear that leftover first.
            op.execute(
                "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                f"WHERE c.relname = '{name}' AND NOT i.indisvalid) THEN "
                f"EXECUTE 'DROP INDEX {name}'; END IF; END $$"
            )
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON properties USING gin ({column} gin_trgm_ops)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _column in INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
