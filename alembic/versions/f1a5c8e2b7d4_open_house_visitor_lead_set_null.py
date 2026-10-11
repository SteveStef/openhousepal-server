"""open house visitor lead fk set null on delete

Revision ID: f1a5c8e2b7d4
Revises: e4b7c9d1a2f3
Create Date: 2026-08-03
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f1a5c8e2b7d4"
down_revision: Union[str, None] = "e4b7c9d1a2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("fk_open_house_visitors_lead_id", "open_house_visitors", type_="foreignkey")
    op.create_foreign_key(
        "fk_open_house_visitors_lead_id", "open_house_visitors", "leads", ["lead_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_open_house_visitors_lead_id", "open_house_visitors", type_="foreignkey")
    op.create_foreign_key("fk_open_house_visitors_lead_id", "open_house_visitors", "leads", ["lead_id"], ["id"])
