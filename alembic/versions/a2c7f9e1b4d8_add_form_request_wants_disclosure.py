"""add_form_request_wants_disclosure

Revision ID: a2c7f9e1b4d8
Revises: e1f5a7c3d9b4
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a2c7f9e1b4d8'
down_revision: Union[str, None] = 'e1f5a7c3d9b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('open_house_form_requests', sa.Column('wants_disclosure', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('open_house_form_requests', 'wants_disclosure')
