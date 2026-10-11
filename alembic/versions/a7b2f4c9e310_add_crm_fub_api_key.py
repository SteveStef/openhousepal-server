"""add crm_fub_api_key to users

Revision ID: a7b2f4c9e310
Revises: f3d8b1e6a940
Create Date: 2026-09-08 21:30:00.000000

A native Follow Up Boss connection: the agent pastes their FUB API key and every new lead is
pushed straight to FUB's /v1/events endpoint (structured, no email parsing). Stored server-side
only; never echoed back to the client.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7b2f4c9e310"
down_revision: Union[str, None] = "f3d8b1e6a940"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("crm_fub_api_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "crm_fub_api_key")
