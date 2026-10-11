"""sites.is_enabled - an IDX agent can switch their IDX website off (e.g. they already have a site)

When off, the public IDX site (every page on its subdomain / custom domain) is not served and every
generated link (QR codes, Agent Hub, email signature, Guest Pass, showcases...) falls back to
openhousepal.com instead of pointing at a site that isn't there. Defaults to on - the behavior every
existing IDX agent already has. Asked right after an IDX purchase/upgrade (IdxDomainMigrationModal)
and editable any time in Settings -> IDX Website.

Revision ID: d4f2a7c9e1b3
Revises: c3e8f1a2b4d6
Create Date: 2026-09-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'd4f2a7c9e1b3'
down_revision: Union[str, None] = 'c3e8f1a2b4d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sites', sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default='true'))


def downgrade() -> None:
    op.drop_column('sites', 'is_enabled')
