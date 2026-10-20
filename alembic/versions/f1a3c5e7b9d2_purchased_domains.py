"""Domains bought inside OpenHousePal (user, 10/20 - "people can buy domains in our site and it
auto sets up the DNS to their IDX website"). Registered at Name.com, paid yearly through PayPal -
see app/services/domain_purchase_service.py.

Revision ID: f1a3c5e7b9d2
Revises: d5e7f9a1b3c4
Create Date: 2026-10-20 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = 'f1a3c5e7b9d2'
down_revision: Union[str, None] = 'd5e7f9a1b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'purchased_domains',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('owner_id', sa.String(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('domain_name', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='CHECKOUT'),
        sa.Column('price_usd', sa.Float(), nullable=False),
        sa.Column('renewal_price_usd', sa.Float(), nullable=False),
        sa.Column('cost_usd', sa.Float(), nullable=True),
        sa.Column('paypal_subscription_id', sa.String(), nullable=True),
        sa.Column('namecom_order_id', sa.String(), nullable=True),
        sa.Column('auto_renew', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('registered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_renewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('registrant', postgresql.JSONB(), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_purchased_domains_owner_id', 'purchased_domains', ['owner_id'])
    op.create_index('ix_purchased_domains_domain_name', 'purchased_domains', ['domain_name'])
    op.create_index('ix_purchased_domains_status', 'purchased_domains', ['status'])
    op.create_index('ix_purchased_domains_paypal_subscription_id', 'purchased_domains', ['paypal_subscription_id'], unique=True)


def downgrade() -> None:
    op.drop_table('purchased_domains')
