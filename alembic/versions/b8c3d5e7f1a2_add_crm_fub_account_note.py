"""add crm_fub_account_note to users

Revision ID: b8c3d5e7f1a2
Revises: a7b2f4c9e310
Create Date: 2026-09-08 23:10:00.000000

When an agent saves a Follow Up Boss API key we probe FUB's /v1/identity to see whether the key
belongs to a non-owner/non-admin user on a team account. If so, leads we create via the API get
routed by the team's lead-routing rules (often to the account owner) rather than straight to the
connecting agent - so we store a short plain-language heads-up here and show it in Settings.
NULL = solo account, owner/admin key, or the probe couldn't run.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8c3d5e7f1a2"
down_revision: Union[str, None] = "a7b2f4c9e310"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("crm_fub_account_note", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "crm_fub_account_note")
