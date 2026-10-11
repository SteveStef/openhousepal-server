"""Send guest emails from the agent's own Gmail / Outlook.

mail_connections: one per agent (OAuth tokens with send-only scope, on/off switch, status, daily
count). scheduled_emails.owner_id: which agent a queued guest email belongs to, so the scheduler can
send it from that agent's mailbox.

Revision ID: f1b6d3a9c4e2
Revises: e9a4c7f2b1d8
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa

revision = 'f1b6d3a9c4e2'
down_revision = 'e9a4c7f2b1d8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'mail_connections',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('provider', sa.String(), nullable=False),
        sa.Column('account_email', sa.String(), nullable=True),
        sa.Column('access_token', sa.Text(), nullable=True),
        sa.Column('refresh_token', sa.Text(), nullable=True),
        sa.Column('token_expiry', sa.DateTime(timezone=True), nullable=True),
        sa.Column('scopes', sa.String(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('status', sa.String(), nullable=False, server_default='ok'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_day', sa.String(), nullable=True),
        sa.Column('sent_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_sent', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_mail_connections_owner_id', 'mail_connections', ['owner_id'], unique=True)
    op.add_column('scheduled_emails', sa.Column('owner_id', sa.String(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True))
    op.create_index('ix_scheduled_emails_owner_id', 'scheduled_emails', ['owner_id'])


def downgrade():
    op.drop_index('ix_scheduled_emails_owner_id', table_name='scheduled_emails')
    op.drop_column('scheduled_emails', 'owner_id')
    op.drop_index('ix_mail_connections_owner_id', table_name='mail_connections')
    op.drop_table('mail_connections')
