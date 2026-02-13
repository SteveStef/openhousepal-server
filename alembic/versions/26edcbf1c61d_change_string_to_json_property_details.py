"""change_string_to_json_property_details

Revision ID: 26edcbf1c61d
Revises: 7b17a9801021
Create Date: 2026-02-05 00:52:24.248022

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '26edcbf1c61d'
down_revision: Union[str, None] = '7b17a9801021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use batch_alter_table for SQLite compatibility
    with op.batch_alter_table('property_details', schema=None) as batch_op:
        batch_alter_cols = [
            'architectural_style', 'construction_materials', 'roof_type',
            'foundation_details', 'structure_type', 'levels', 'topography'
        ]
        for col in batch_alter_cols:
            batch_op.alter_column(col,
                existing_type=sa.VARCHAR(),
                type_=sa.JSON(),
                existing_nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('property_details', schema=None) as batch_op:
        batch_alter_cols = [
            'architectural_style', 'construction_materials', 'roof_type',
            'foundation_details', 'structure_type', 'levels', 'topography'
        ]
        for col in batch_alter_cols:
            batch_op.alter_column(col,
                existing_type=sa.JSON(),
                type_=sa.VARCHAR(),
                existing_nullable=True)