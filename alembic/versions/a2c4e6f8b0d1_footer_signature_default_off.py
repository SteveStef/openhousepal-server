"""IDX footer signature card: off by default (user, 10/18 - "it doesn't look that great, and we
have other footers"). The Design tab used to save the old default (on) into every site's theme, so
those saved values are switched off too; an agent who wants it back turns it on in Design.

Revision ID: a2c4e6f8b0d1
Revises: f1b3d5e7a9c2
Create Date: 2026-10-18 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a2c4e6f8b0d1'
down_revision: Union[str, None] = 'f1b3d5e7a9c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for col in ('draft_data', 'published_snapshot'):
        op.execute(
            f"UPDATE sites SET {col} = jsonb_set({col}, '{{theme,footer_signature}}', 'false'::jsonb) "
            f"WHERE jsonb_typeof({col}->'theme') = 'object' AND {col}->'theme' ? 'footer_signature'"
        )


def downgrade() -> None:
    pass
