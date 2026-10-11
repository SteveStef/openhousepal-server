"""design studio v2 - design_projects.campaign_id becomes a real FK to form_campaigns

Revision ID: d9f3a2b5c714
Revises: c1d4e9f2a857
Create Date: 2026-08-23 00:00:03.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d9f3a2b5c714"
down_revision: Union[str, None] = "c1d4e9f2a857"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # campaign_id was always an unwired placeholder String column - never actually set by any code
    # path, so there's nothing to backfill/clean before adding the real constraint.
    op.execute("UPDATE design_projects SET campaign_id = NULL WHERE campaign_id IS NOT NULL AND campaign_id NOT IN (SELECT id FROM form_campaigns)")
    op.create_foreign_key(
        "fk_design_projects_campaign_id", "design_projects", "form_campaigns",
        ["campaign_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_design_projects_campaign_id", "design_projects", type_="foreignkey")
