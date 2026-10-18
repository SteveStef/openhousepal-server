"""Short, personal Showcase links: collections.short_code ("dana-k7q2" -> /s/dana-k7q2).

Revision ID: e6a8c0d2f4b5
Revises: d5f7b9c1e3a4
Create Date: 2026-10-18 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e6a8c0d2f4b5'
down_revision: Union[str, None] = 'd5f7b9c1e3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('collections', sa.Column('short_code', sa.String(), nullable=True))
    op.create_index('ix_collections_short_code', 'collections', ['short_code'], unique=True)
    # Every existing shareable Showcase gets its short code now, so the agent's Share button and
    # every email switch over at once (new ones are minted on first use - showcase_links.py).
    import re
    import secrets
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, visitor_name FROM collections WHERE share_token IS NOT NULL AND short_code IS NULL")).fetchall()
    used = set()
    for cid, visitor_name in rows:
        first = re.sub(r"[^a-z]", "", ((visitor_name or "").strip().split(" ")[0]).lower())[:10]
        base = first if len(first) >= 2 and first not in ("anonymous", "visitor", "guest") else "home"
        for _ in range(20):
            code = f"{base}-{''.join(secrets.choice(alphabet) for _ in range(4))}"
            if code not in used:
                used.add(code)
                conn.execute(sa.text("UPDATE collections SET short_code = :c WHERE id = :i"), {"c": code, "i": cid})
                break


def downgrade() -> None:
    op.drop_index('ix_collections_short_code', table_name='collections')
    op.drop_column('collections', 'short_code')
