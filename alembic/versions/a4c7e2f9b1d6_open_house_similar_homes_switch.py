"""Per-open-house Similar Homes switch: when off, the kit has no Similar Homes sheet and the Guest
Pass shows no similar homes (the picked homes stay saved, so turning it back on restores them).
A listing agent hosting their own listing gets it - and Showcases - off by default.

Revision ID: a4c7e2f9b1d6
Revises: e3a8c5f1d7b2
Create Date: 2026-10-14 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4c7e2f9b1d6'
down_revision: Union[str, None] = 'e3a8c5f1d7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('open_house_events', sa.Column('similar_homes', sa.Boolean(), nullable=False, server_default='true'))


def downgrade() -> None:
    op.drop_column('open_house_events', 'similar_homes')
