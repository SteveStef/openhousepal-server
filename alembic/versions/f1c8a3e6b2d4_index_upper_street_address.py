"""Index properties by upper-case street address

Follow-ups match a typed-in address ("847 Lesley Rd") to its listing with
upper(street_address) = '847 LESLEY RD'. Without an index that scanned all ~300k listings
(~300 ms per follow-up card, every time the Dash / Follow-ups list loaded). Re-runnable.

Revision ID: f1c8a3e6b2d4
Revises: e9a4c7d2f1b8
Create Date: 2026-10-04
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f1c8a3e6b2d4"
down_revision: Union[str, None] = "e9a4c7d2f1b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_properties_upper_street_address ON properties (upper(street_address))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_properties_upper_street_address")
