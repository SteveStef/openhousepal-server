"""add_form_request_completed_at

Revision ID: e1f5a7c3d9b4
Revises: d8a4c1f9b2e7
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e1f5a7c3d9b4'
down_revision: Union[str, None] = 'd8a4c1f9b2e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('open_house_form_requests', sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('open_house_form_requests', 'completed_at')
