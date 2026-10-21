"""sites.google_search - getting each IDX website into Google/Bing automatically

Our Search Console verification code, the agent's own pasted code, and the status shown on the
editor's Google tab (see services/website/google_search_service.py). Additive: one JSONB column
with a constant default (a catalog-only change in Postgres - instant).

Revision ID: b8e2c4d6f1a3
Revises: a7d3f9c1e5b2
Create Date: 2026-10-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'b8e2c4d6f1a3'
down_revision: Union[str, None] = 'a7d3f9c1e5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sites', sa.Column('google_search', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'))


def downgrade() -> None:
    op.drop_column('sites', 'google_search')
