"""blogs.theme - the article's look (null = the default "luxe" theme)

Revision ID: a3c7e9f1b2d4
Revises: f6a9d2c4b8e1
Create Date: 2026-10-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a3c7e9f1b2d4"
down_revision: Union[str, None] = "f6a9d2c4b8e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("blogs", sa.Column("theme", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("blogs", "theme")
