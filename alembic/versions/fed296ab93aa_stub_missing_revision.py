"""Stub for missing revision fed296ab93aa

Revision ID: fed296ab93aa
Revises: bea6a6e83a5e
Create Date: 2026-04-26 10:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'fed296ab93aa'
down_revision: Union[str, None] = 'bea6a6e83a5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    pass

def downgrade() -> None:
    pass
