"""Client email tracking + engagement rules.

client_emails: every email sent to an agent's client, with open / click tracking (the id is the
tracking token). client_email_prefs: per agent + client, daily / weekly / off for automatic emails
and why (client_email_service).

Revision ID: a8d3f1c6b2e9
Revises: f1b6d3a9c4e2
Create Date: 2026-10-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = 'a8d3f1c6b2e9'
down_revision = 'f1b6d3a9c4e2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'client_emails',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('recipient_email', sa.String(), nullable=False),
        sa.Column('template', sa.String(), nullable=False),
        sa.Column('subject', sa.String(), nullable=False, server_default=''),
        sa.Column('kind', sa.String(), nullable=False, server_default='personal'),
        sa.Column('via', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='sending'),
        sa.Column('links', JSONB(), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('open_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('clicked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('click_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_client_emails_owner_id', 'client_emails', ['owner_id'])
    op.create_index('ix_client_emails_owner_recipient_sent', 'client_emails', ['owner_id', 'recipient_email', 'sent_at'])
    op.create_table(
        'client_email_prefs',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('email', sa.String(), nullable=False),
        sa.Column('mode', sa.String(), nullable=False, server_default='daily'),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('manual', sa.String(), nullable=True),
        sa.Column('reset_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_weekly_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('owner_id', 'email', name='uq_client_email_prefs_owner_email'),
    )
    op.create_index('ix_client_email_prefs_owner_id', 'client_email_prefs', ['owner_id'])


def downgrade():
    op.drop_table('client_email_prefs')
    op.drop_table('client_emails')
