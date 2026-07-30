"""Add agent_forms and form_submissions tables

Revision ID: a3f1c9d02b7e
Revises: 91c0d7e8a4f2
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a3f1c9d02b7e'
down_revision: Union[str, None] = '91c0d7e8a4f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_forms',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('form_type', sa.String(), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('subtitle', sa.Text(), nullable=True),
        sa.Column('questions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('success_heading', sa.String(), nullable=False),
        sa.Column('success_message', sa.Text(), nullable=True),
        sa.Column('accent_color', sa.String(), nullable=True),
        sa.Column('is_enabled', sa.Boolean(), nullable=False),
        sa.Column('order_index', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id', 'form_type', name='uq_agent_forms_owner_type'),
        sa.UniqueConstraint('owner_id', 'slug', name='uq_agent_forms_owner_slug'),
    )
    op.create_index(op.f('ix_agent_forms_owner_id'), 'agent_forms', ['owner_id'])

    op.create_table(
        'form_submissions',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('form_id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('form_type', sa.String(), nullable=False),
        sa.Column('answers', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('contact_name', sa.String(), nullable=True),
        sa.Column('contact_email', sa.String(), nullable=True),
        sa.Column('contact_phone', sa.String(), nullable=True),
        sa.Column('property_address', sa.String(), nullable=True),
        sa.Column('is_reviewed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['form_id'], ['agent_forms.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_form_submissions_form_id'), 'form_submissions', ['form_id'])
    op.create_index(op.f('ix_form_submissions_owner_id'), 'form_submissions', ['owner_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_form_submissions_owner_id'), table_name='form_submissions')
    op.drop_index(op.f('ix_form_submissions_form_id'), table_name='form_submissions')
    op.drop_table('form_submissions')
    op.drop_index(op.f('ix_agent_forms_owner_id'), table_name='agent_forms')
    op.drop_table('agent_forms')
