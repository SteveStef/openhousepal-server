"""add form_view_sessions table

Revision ID: 2a713142430e
Revises: 1c8f638a3b17
Create Date: 2026-08-06 19:43:11.565509

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a713142430e'
down_revision: Union[str, None] = '1c8f638a3b17'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Note: autogenerate also picked up unrelated pre-existing drift (a leads index and an
    # open_house_listing_notes constraint/index) not touched by this change - intentionally
    # excluded here so this migration only does what its message says.
    op.create_table('form_view_sessions',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('form_id', sa.String(), nullable=False),
    sa.Column('owner_id', sa.String(), nullable=False),
    sa.Column('session_key', sa.String(), nullable=False),
    sa.Column('furthest_step_index', sa.Integer(), nullable=False),
    sa.Column('furthest_question_id', sa.String(), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('submission_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['form_id'], ['agent_forms.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['submission_id'], ['form_submissions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('form_id', 'session_key', name='uq_form_view_sessions_form_session')
    )
    op.create_index(op.f('ix_form_view_sessions_form_id'), 'form_view_sessions', ['form_id'], unique=False)
    op.create_index(op.f('ix_form_view_sessions_owner_id'), 'form_view_sessions', ['owner_id'], unique=False)
    op.create_index(op.f('ix_form_view_sessions_session_key'), 'form_view_sessions', ['session_key'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_form_view_sessions_session_key'), table_name='form_view_sessions')
    op.drop_index(op.f('ix_form_view_sessions_owner_id'), table_name='form_view_sessions')
    op.drop_index(op.f('ix_form_view_sessions_form_id'), table_name='form_view_sessions')
    op.drop_table('form_view_sessions')
