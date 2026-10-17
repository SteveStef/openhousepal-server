"""Per-agent email settings (which client emails go out, from which address, invite timing, briefing
time), the agent's QR-code center logo choice, and a per-open-house follow-ups switch.

Revision ID: b7d2e4f6a8c1
Revises: a4c7e2f9b1d6
Create Date: 2026-10-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b7d2e4f6a8c1'
down_revision: Union[str, None] = 'a4c7e2f9b1d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('email_settings', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('users', sa.Column('qr_logo', sa.String(), nullable=True))
    op.add_column('client_emails', sa.Column('error', sa.String(), nullable=True))
    op.add_column('calendar_event_links', sa.Column('body_hash', sa.String(), nullable=True))
    op.add_column('cma_reports', sa.Column('generation_error', sa.String(), nullable=True))
    # 'on' = normal; 'no_calendar' = follow-ups made, kept off the agent's calendar; 'off' = none at all
    op.add_column('open_house_events', sa.Column('followups_mode', sa.String(), nullable=False, server_default='on'))


def downgrade() -> None:
    op.drop_column('open_house_events', 'followups_mode')
    op.drop_column('cma_reports', 'generation_error')
    op.drop_column('calendar_event_links', 'body_hash')
    op.drop_column('client_emails', 'error')
    op.drop_column('users', 'qr_logo')
    op.drop_column('users', 'email_settings')
