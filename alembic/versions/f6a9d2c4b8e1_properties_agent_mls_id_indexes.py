"""index the raw-MLS agent id keys used to match an agent's sales

Every "this agent's sales" lookup (IDX About stats, Recent Sales, hero search suggestions - see
content_adapters._agent_sale_identifier_filters) matches raw_mls_data->>'BuyerAgentMlsId' /
'CoBuyerAgentMlsId' / 'ListAgentMlsId' / 'CoListAgentMlsId'. With no index those were full
sequential scans of the whole properties table (~15s each), which made the IDX editor preview
and the public About/Sold sections take 20+ seconds. With these, Postgres can BitmapOr them
with the existing list_agent_mls_id / co_list_agent_mls_id indexes.

Built CONCURRENTLY so the MLS sync can keep writing to properties while they build.

Revision ID: f6a9d2c4b8e1
Revises: e5b3c8a1f6d2
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f6a9d2c4b8e1"
down_revision: Union[str, None] = "e5b3c8a1f6d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_KEYS = {
    "ix_properties_raw_buyer_agent_mls_id": "BuyerAgentMlsId",
    "ix_properties_raw_co_buyer_agent_mls_id": "CoBuyerAgentMlsId",
    "ix_properties_raw_list_agent_mls_id": "ListAgentMlsId",
    "ix_properties_raw_co_list_agent_mls_id": "CoListAgentMlsId",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, key in _KEYS.items():
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON properties ((raw_mls_data ->> '{key}'))")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _KEYS:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
