"""Listing Spotlight: the email-signature card is OFF on every existing page (user, 10/24 - "it
doesn't look right there"). New pages already start with it off (no saved value = off); this
switches off the few pages that had it turned on explicitly, on both the draft and the live copy.
property_websites is a small table (a handful of rows) - a plain UPDATE.

Revision ID: c4e6a8b0d2f1
Revises: f3b5d7e9a1c4
Create Date: 2026-10-24
"""
from typing import Sequence, Union

from alembic import op

revision: str = "c4e6a8b0d2f1"
down_revision: Union[str, None] = "f3b5d7e9a1c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for col in ("draft_data", "published_snapshot"):
        op.execute(
            f"""
            UPDATE property_websites
               SET {col} = jsonb_set({col}, '{{section_visibility,signature}}', 'false'::jsonb, true)
             WHERE {col}->'section_visibility'->>'signature' = 'true'
            """
        )


def downgrade() -> None:
    # Which pages had it on isn't kept - nothing to restore.
    pass
