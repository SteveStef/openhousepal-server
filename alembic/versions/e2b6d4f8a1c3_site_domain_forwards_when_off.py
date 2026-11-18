"""Sites remember what the agent's own domain does while their IDX website is switched off:
false (the default) = it shows their Agent Hub by itself; true = "Turn my domain off completely" -
it forwards to their openhousepal.com Agent Hub address, so Google drops the domain over time
(owner, 11/18). One column with a default - instant, every existing site keeps showing its Hub.

Revision ID: e2b6d4f8a1c3
Revises: d7a3c9e1f4b8
Create Date: 2026-11-18
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e2b6d4f8a1c3"
down_revision: Union[str, None] = "d7a3c9e1f4b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE sites ADD COLUMN IF NOT EXISTS domain_forwards_when_off BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE sites DROP COLUMN IF EXISTS domain_forwards_when_off")
