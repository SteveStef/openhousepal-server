"""market_area_canonical_key_dedup

Adds MarketArea.canonical_key - the real, DB-enforced deduplication identity (state + area_type +
a deterministic local text normalization of name, + normalized parent name for neighborhoods) -
replacing the previous app-code-only "SELECT every row for this state, compare normalized strings
in Python" dedup check, which had no database-level guarantee behind it (see
app.services.areas.canonical_key.compute_canonical_key for the exact algorithm, shared verbatim
with the runtime get_or_create_market_area code path so the two can never drift apart).

Sequence, in order (see the audit this migration implements the fix for):
  1. Add canonical_key (nullable for now).
  2. Backfill canonical_key on every existing row using the exact same algorithm the app uses.
  3. Scan for any existing rows that would collapse to the same canonical_key (there should be
     none in a healthy DB, but this MUST run before the constraint below can safely be added) -
     report every group found, consolidate each into a single survivor row (highest
     enrichment_status, most current research_version, richest facts, oldest as final tiebreak -
     see canonical_key.survivor_sort_key), repoint every AgentMarketArea link from the losing
     row(s) to the survivor (de-duplicating any owner who already links to both), then delete the
     losing row(s). Never triggers new research - this is pure data consolidation.
  4. Make canonical_key NOT NULL now that every row has one.
  5. Add the real UNIQUE constraint on canonical_key - this is the actual guarantee; everything
     above just makes it safe to turn on without breaking existing data.

Downgrade only reverses the schema change (drops the constraint/column) - the consolidation in
step 3, if it ever merges real duplicate rows, is not reversible (matches how this kind of
data-consolidation migration normally works; the merged/deleted rows' own data was already the
inferior/duplicate copy of what the survivor kept).

Revision ID: c8e2f4b6a9d1
Revises: f2b7c93e4a1d
Create Date: 2026-08-16 13:05:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.services.areas.canonical_key import compute_canonical_key, survivor_sort_key

# revision identifiers, used by Alembic.
revision: str = 'c8e2f4b6a9d1'
down_revision: Union[str, None] = 'f2b7c93e4a1d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('market_areas', sa.Column('canonical_key', sa.String(), nullable=True))
    bind = op.get_bind()

    rows = bind.execute(sa.text(
        "SELECT id, name, state, area_type, parent_area_name, enrichment_status, "
        "research_version, narrative, facts, created_at FROM market_areas"
    )).mappings().all()

    print(f"[market_area_canonical_key_dedup] Backfilling canonical_key on {len(rows)} existing MarketArea row(s)...")

    by_key: dict[str, list[dict]] = {}
    for row in rows:
        key = compute_canonical_key(row["name"], row["state"], row["area_type"], row["parent_area_name"])
        bind.execute(
            sa.text("UPDATE market_areas SET canonical_key = :key WHERE id = :id"),
            {"key": key, "id": row["id"]},
        )
        by_key.setdefault(key, []).append(dict(row))

    duplicate_groups = {key: group for key, group in by_key.items() if len(group) > 1}

    if not duplicate_groups:
        print(f"[market_area_canonical_key_dedup] No duplicate canonical geographies found among {len(rows)} row(s). Nothing to consolidate.")
    else:
        print(f"[market_area_canonical_key_dedup] Found {len(duplicate_groups)} duplicate canonical geography group(s) - consolidating:")
        for key, group in duplicate_groups.items():
            group_sorted = sorted(group, key=survivor_sort_key, reverse=True)
            survivor = group_sorted[0]
            losers = group_sorted[1:]
            names = ", ".join(f"{r['name']!r} ({r['id']}, status={r['enrichment_status']})" for r in group)
            print(f"  - canonical_key={key!r}: {names}")
            print(f"    -> keeping {survivor['name']!r} ({survivor['id']}, status={survivor['enrichment_status']}) as the survivor")

            for loser in losers:
                # Repoint any agent's link to the loser row onto the survivor, unless that agent
                # already links to the survivor too (in which case repointing would violate
                # uq_agent_market_areas_owner_area) - drop the redundant loser link instead.
                bind.execute(
                    sa.text(
                        "UPDATE agent_market_areas SET area_id = :survivor_id "
                        "WHERE area_id = :loser_id AND owner_id NOT IN "
                        "(SELECT owner_id FROM agent_market_areas WHERE area_id = :survivor_id)"
                    ),
                    {"survivor_id": survivor["id"], "loser_id": loser["id"]},
                )
                bind.execute(
                    sa.text("DELETE FROM agent_market_areas WHERE area_id = :loser_id"),
                    {"loser_id": loser["id"]},
                )
                bind.execute(sa.text("DELETE FROM market_areas WHERE id = :loser_id"), {"loser_id": loser["id"]})
                print(f"    -> merged and removed {loser['name']!r} ({loser['id']})")

    op.alter_column('market_areas', 'canonical_key', nullable=False)
    op.create_unique_constraint('uq_market_areas_canonical_key', 'market_areas', ['canonical_key'])
    print("[market_area_canonical_key_dedup] canonical_key is now NOT NULL and UNIQUE-constrained.")


def downgrade() -> None:
    op.drop_constraint('uq_market_areas_canonical_key', 'market_areas', type_='unique')
    op.drop_column('market_areas', 'canonical_key')
