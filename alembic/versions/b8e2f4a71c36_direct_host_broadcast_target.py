"""direct host request - add target_agent_id to team broadcasts

Revision ID: b8e2f4a71c36
Revises: a3f7c8e1d924
Create Date: 2026-08-23 00:00:01.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8e2f4a71c36"
down_revision: Union[str, None] = "a3f7c8e1d924"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "open_house_team_broadcasts",
        sa.Column("target_agent_id", sa.String(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
    )
    op.create_index("ix_open_house_team_broadcasts_target_agent_id", "open_house_team_broadcasts", ["target_agent_id"])


def downgrade() -> None:
    op.drop_index("ix_open_house_team_broadcasts_target_agent_id", table_name="open_house_team_broadcasts")
    op.drop_column("open_house_team_broadcasts", "target_agent_id")
