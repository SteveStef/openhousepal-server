"""add open_house_signin_views table

Revision ID: d8e21f6c4a13
Revises: c1a7f9e4d2b3
Create Date: 2026-08-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8e21f6c4a13'
down_revision: Union[str, None] = 'c1a7f9e4d2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('open_house_signin_views',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('open_house_event_id', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['open_house_event_id'], ['open_house_events.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_open_house_signin_views_open_house_event_id'), 'open_house_signin_views', ['open_house_event_id'], unique=False)
    op.create_index(op.f('ix_open_house_signin_views_created_at'), 'open_house_signin_views', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_open_house_signin_views_created_at'), table_name='open_house_signin_views')
    op.drop_index(op.f('ix_open_house_signin_views_open_house_event_id'), table_name='open_house_signin_views')
    op.drop_table('open_house_signin_views')
