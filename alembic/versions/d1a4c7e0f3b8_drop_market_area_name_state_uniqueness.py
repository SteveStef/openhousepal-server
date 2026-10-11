"""drop_market_area_name_state_uniqueness

Drops uq_market_areas_name_state, superseded by uq_market_areas_canonical_key (see the previous
migration, market_area_canonical_key_dedup). Keeping the old (name, state) constraint turned out
to be actively wrong, not just redundant: it incorrectly forbids two genuinely different canonical
geographies that happen to share a literal name+state pair - e.g. two differently-parented
neighborhoods both legitimately named "Oak Grove" in the same state, or a 'city' and a
'neighborhood' sharing a name (both correctly distinct under canonical_key, since area_type and
parent are part of the canonical identity but were never part of the old constraint). Found via
the real-database concurrency test for exactly this scenario
(tests/test_market_area_concurrency.py::test_f_same_neighborhood_name_different_parents_stay_distinct),
which failed against uq_market_areas_name_state even though canonical_key correctly kept the two
rows distinct.

Revision ID: d1a4c7e0f3b8
Revises: c8e2f4b6a9d1
Create Date: 2026-08-16 13:40:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd1a4c7e0f3b8'
down_revision: Union[str, None] = 'c8e2f4b6a9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('uq_market_areas_name_state', 'market_areas', type_='unique')


def downgrade() -> None:
    op.create_unique_constraint('uq_market_areas_name_state', 'market_areas', ['name', 'state'])
