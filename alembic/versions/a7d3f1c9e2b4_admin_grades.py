"""Admin grading: the admin's own grade for each agent-made blog or CMA (owner, 11/01)

- admin_grades: one row per blog/CMA the admin marks - graded yes/no, the grade it got, notes.
  Admin-only; agents never see it.

A new empty table, so it never rewrites or locks an existing one.

Revision ID: a7d3f1c9e2b4
Revises: e5c9a1f3b7d2
Create Date: 2026-11-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3f1c9e2b4"
down_revision: Union[str, None] = "e5c9a1f3b7d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_grades",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("item_type", sa.String(8), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("graded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("grade", sa.String(40), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("item_type", "item_id", name="uq_admin_grades_item"),
    )


def downgrade() -> None:
    op.drop_table("admin_grades")
