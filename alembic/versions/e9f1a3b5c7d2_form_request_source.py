"""Where an open house request came from

open_house_form_requests.source - guest_pass | cheat_sheet | invite, so the agent's follow-up,
calendar event, alert and Opens list say HOW the guest asked for the disclosure / second tour.
One nullable column, nothing backfilled.

Revision ID: e9f1a3b5c7d2
Revises: d6e8f0a2b4c7
Create Date: 2026-10-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9f1a3b5c7d2"
down_revision: Union[str, None] = "d6e8f0a2b4c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("open_house_form_requests", sa.Column("source", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("open_house_form_requests", "source")
