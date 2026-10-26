"""Admin "test copy" accounts

users.test_copy_of_user_id - set by an admin to make an account a test copy of a real agent, so it
can share that agent's MLS ID and see their listings, sales and open houses (owner, 10/26). The
one-account-per-MLS-ID rule now applies to real accounts only: the unique index on users.mls_id
becomes a partial unique index (WHERE test_copy_of_user_id IS NULL) plus a plain index for lookups.
Real agents keep exactly the protection they had; nothing is backfilled.

Revision ID: b3d9f2a6c8e4
Revises: a7c9e1b3d5f6
Create Date: 2026-10-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3d9f2a6c8e4"
down_revision: Union[str, None] = "a7c9e1b3d5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("test_copy_of_user_id", sa.String(), nullable=True))
    op.create_foreign_key("fk_users_test_copy_of_user_id", "users", "users", ["test_copy_of_user_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_users_test_copy_of_user_id", "users", ["test_copy_of_user_id"])
    # The old rule was made by the model as a unique index (ix_users_mls_id); a constraint form is
    # dropped too in case a database has one.
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_mls_id_key")
    op.execute("DROP INDEX IF EXISTS ix_users_mls_id")
    op.create_index("ix_users_mls_id", "users", ["mls_id"])
    op.create_index("ux_users_mls_id_real", "users", ["mls_id"], unique=True,
                    postgresql_where=sa.text("test_copy_of_user_id IS NULL"))


def downgrade() -> None:
    op.drop_index("ux_users_mls_id_real", table_name="users")
    op.drop_index("ix_users_mls_id", table_name="users")
    op.create_index("ix_users_mls_id", "users", ["mls_id"], unique=True)  # fails while test copies share an ID - stop them first
    op.drop_index("ix_users_test_copy_of_user_id", table_name="users")
    op.drop_constraint("fk_users_test_copy_of_user_id", "users", type_="foreignkey")
    op.drop_column("users", "test_copy_of_user_id")
