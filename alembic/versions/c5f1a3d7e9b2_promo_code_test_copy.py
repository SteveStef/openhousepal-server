"""Test-account sign-up codes

promo_codes.test_copy_of_user_id - an admin code that lets the Bright MLS sign-up take a real agent's
MLS ID; the new account becomes that agent's test copy (owner, 10/26). Nothing is backfilled.

Revision ID: c5f1a3d7e9b2
Revises: b3d9f2a6c8e4
Create Date: 2026-10-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5f1a3d7e9b2"
down_revision: Union[str, None] = "b3d9f2a6c8e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("promo_codes", sa.Column("test_copy_of_user_id", sa.String(), nullable=True))
    op.create_foreign_key("fk_promo_codes_test_copy_of_user_id", "promo_codes", "users",
                          ["test_copy_of_user_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_promo_codes_test_copy_of_user_id", "promo_codes", type_="foreignkey")
    op.drop_column("promo_codes", "test_copy_of_user_id")
