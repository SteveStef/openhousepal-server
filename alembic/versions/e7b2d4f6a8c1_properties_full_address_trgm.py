"""properties.full_address trigram index - fast address search again

The address search boxes (CMA subject, OH kit, Studio, comps, forms) match full_address with
ILIKE '%...%' and the pg_trgm similarity operator. The original GIN trigram index for that
(idx_properties_full_address_trgm, migration 74476447af04) was dropped by two later autogenerate
migrations (27e3940dfd06, e86c19e7a94b) because the model never declared it, so every keystroke
became a full scan of the properties table (~1.5 s locally, longer on the live 1.1M rows).

This puts it back under a new name (so those old DROP statements can never match it) and the
model now declares it, so autogenerate stops dropping it.

Index only: built CONCURRENTLY so the MLS sync keeps writing while it builds, IF NOT EXISTS so a
re-run is harmless. The properties table itself is never rewritten.

Revision ID: e7b2d4f6a8c1
Revises: d5a7c9e1f3b4
Create Date: 2026-10-21
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e7b2d4f6a8c1"
down_revision: Union[str, None] = "d5a7c9e1f3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        # A CONCURRENTLY build that was interrupted leaves an INVALID index behind, which IF NOT
        # EXISTS would then skip forever - clear that leftover first.
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = 'ix_properties_full_address_trgm' AND NOT i.indisvalid) THEN "
            "EXECUTE 'DROP INDEX ix_properties_full_address_trgm'; END IF; END $$"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_properties_full_address_trgm "
            "ON properties USING gin (full_address gin_trgm_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_properties_full_address_trgm")
