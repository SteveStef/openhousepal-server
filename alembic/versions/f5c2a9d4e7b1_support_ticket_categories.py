"""support ticket categories (support / bug / feature), submission details, screenshots

Revision ID: f5c2a9d4e7b1
Revises: c8e2a4f6b1d9
Create Date: 2026-10-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'f5c2a9d4e7b1'
down_revision = 'c8e2a4f6b1d9'
branch_labels = None
depends_on = None


def upgrade():
    # Existing tickets were all plain questions to support, so they backfill as 'support'.
    op.add_column('support_tickets', sa.Column('category', sa.String(), nullable=False, server_default='support'))
    op.create_index('ix_support_tickets_category', 'support_tickets', ['category'])
    op.add_column('support_tickets', sa.Column('details', postgresql.JSONB(), nullable=True))
    op.add_column('support_ticket_messages', sa.Column('attachments', postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column('support_ticket_messages', 'attachments')
    op.drop_column('support_tickets', 'details')
    op.drop_index('ix_support_tickets_category', table_name='support_tickets')
    op.drop_column('support_tickets', 'category')
