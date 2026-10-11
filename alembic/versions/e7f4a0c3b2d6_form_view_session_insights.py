"""Form insights: what each visitor answered (even unfinished), the path they took through the
form with timestamps, where they came from (QR / link / website), their device, and when they
were last active - powers per-question drop-off, time-per-question and unfinished responses.

Revision ID: e7f4a0c3b2d6
Revises: d6e3f9b2a1c5
Create Date: 2026-09-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e7f4a0c3b2d6'
down_revision: Union[str, None] = 'd6e3f9b2a1c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('form_view_sessions', sa.Column('partial_answers', postgresql.JSONB(), nullable=True))
    op.add_column('form_view_sessions', sa.Column('path', postgresql.JSONB(), nullable=True))
    op.add_column('form_view_sessions', sa.Column('started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('form_view_sessions', sa.Column('last_activity_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('form_view_sessions', sa.Column('source', sa.String(), nullable=True))
    op.add_column('form_view_sessions', sa.Column('device', sa.String(), nullable=True))
    op.add_column('form_view_sessions', sa.Column('referrer', sa.String(), nullable=True))
    op.add_column('form_view_sessions', sa.Column('contact_name', sa.String(), nullable=True))
    op.add_column('form_view_sessions', sa.Column('contact_email', sa.String(), nullable=True))
    op.add_column('form_view_sessions', sa.Column('contact_phone', sa.String(), nullable=True))
    op.add_column('form_view_sessions', sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_form_view_sessions_form_activity', 'form_view_sessions', ['form_id', 'last_activity_at'])


def downgrade() -> None:
    op.drop_index('ix_form_view_sessions_form_activity', table_name='form_view_sessions')
    for col in ('dismissed_at', 'contact_phone', 'contact_email', 'contact_name', 'referrer', 'device',
                'source', 'last_activity_at', 'started_at', 'path', 'partial_answers'):
        op.drop_column('form_view_sessions', col)
