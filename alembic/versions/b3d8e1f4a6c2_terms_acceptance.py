"""Record Terms of Service acceptance at sign-up

- users.terms_accepted_at: when the agent ticked "I agree" on the sign-up form.
- users.terms_version: the "Last updated" date of the Terms they agreed to.

Accounts made before this stay NULL (they agreed, but nothing recorded it then).
Both nullable with no default, so adding them never rewrites the table.

Revision ID: b3d8e1f4a6c2
Revises: e5a9c2f7d4b1
Create Date: 2026-10-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3d8e1f4a6c2"
down_revision: Union[str, None] = "e5a9c2f7d4b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("terms_version", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "terms_version")
    op.drop_column("users", "terms_accepted_at")
