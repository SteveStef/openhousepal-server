"""Email Signature card placements: CMA web view + Guest Pass footer switches

Revision ID: c1e5a7d9f2b4
Revises: b9d3f6a2c8e4
Create Date: 2026-10-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c1e5a7d9f2b4"
down_revision: Union[str, None] = "b9d3f6a2c8e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cma_reports", sa.Column("use_signature_card", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.add_column("open_house_events", sa.Column("pass_signature", sa.Boolean(), nullable=False, server_default=sa.text("true")))


def downgrade() -> None:
    op.drop_column("open_house_events", "pass_signature")
    op.drop_column("cma_reports", "use_signature_card")
