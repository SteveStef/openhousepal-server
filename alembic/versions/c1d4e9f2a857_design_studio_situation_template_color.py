"""design studio v2 - rename template_id to style_id, add template_id/situation/color_mode

Revision ID: c1d4e9f2a857
Revises: b8e2f4a71c36
Create Date: 2026-08-23 00:00:02.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d4e9f2a857"
down_revision: Union[str, None] = "b8e2f4a71c36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # template_id used to store what the app now calls "Style" (editorial/modern_luxury/minimal/bold) -
    # free the name up for the new, real Template (layout variant) concept.
    op.alter_column("design_projects", "template_id", new_column_name="style_id")

    op.add_column("design_projects", sa.Column("template_id", sa.String(), nullable=True))
    op.add_column("design_projects", sa.Column("color_mode", sa.String(), nullable=True))
    op.execute(
        """
        UPDATE design_projects SET color_mode = CASE style_id
            WHEN 'modern_luxury' THEN 'dark'
            WHEN 'bold' THEN 'brand'
            ELSE 'light'
        END WHERE color_mode IS NULL
        """
    )
    op.add_column("design_projects", sa.Column("situation", sa.String(), nullable=True))
    op.create_index(op.f("ix_design_projects_situation"), "design_projects", ["situation"])


def downgrade() -> None:
    op.drop_index(op.f("ix_design_projects_situation"), table_name="design_projects")
    op.drop_column("design_projects", "situation")
    op.drop_column("design_projects", "color_mode")
    op.drop_column("design_projects", "template_id")
    op.alter_column("design_projects", "style_id", new_column_name="template_id")
