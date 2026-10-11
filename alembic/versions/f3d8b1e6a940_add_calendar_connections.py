"""two-way calendar sync: connections + event links

Revision ID: f3d8b1e6a940
Revises: e2c1a4b7d9f3
Create Date: 2026-09-08 20:00:00.000000

`calendar_connections` - one row per agent who connected an external calendar (Google now;
provider column leaves room for Microsoft). Holds the OAuth tokens + the id of the dedicated
"OpenHousePal" secondary calendar we create in their account (calendar.app.created scope: we can
only touch events on a calendar we made).

`calendar_event_links` - maps an OHP task / scheduled open house to the id of the event we
created for it in that calendar, so edits and deletes update the right event.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3d8b1e6a940"
down_revision: Union[str, None] = "e2c1a4b7d9f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "calendar_connections",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False, server_default="google"),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("calendar_id", sa.String(), nullable=True),
        sa.Column("account_email", sa.String(), nullable=True),
        sa.Column("scopes", sa.String(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "provider", name="uq_calendar_connections_owner_provider"),
    )
    op.create_index("ix_calendar_connections_owner_id", "calendar_connections", ["owner_id"], unique=False)

    op.create_table(
        "calendar_event_links",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),  # 'task' | 'open_house'
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("external_event_id", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["calendar_connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "kind", "source_id", name="uq_calendar_event_links_src"),
    )
    op.create_index("ix_calendar_event_links_connection_id", "calendar_event_links", ["connection_id"], unique=False)


def downgrade() -> None:
    op.drop_table("calendar_event_links")
    op.drop_table("calendar_connections")
