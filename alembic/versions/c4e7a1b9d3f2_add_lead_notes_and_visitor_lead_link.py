"""add_lead_notes_and_visitor_lead_link

Revision ID: c4e7a1b9d3f2
Revises: b3d6f9a2c5e8
Create Date: 2026-07-30 23:30:00.000000

"""
import re
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4e7a1b9d3f2'
down_revision: Union[str, None] = 'b3d6f9a2c5e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _normalize_email(email):
    if not email:
        return None
    value = email.strip().lower()
    return value or None


def _normalize_phone(phone):
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return digits or None


def upgrade() -> None:
    op.create_table(
        'lead_notes',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('lead_id', sa.String(), nullable=False),
        sa.Column('author_id', sa.String(), nullable=True),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('open_house_event_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['author_id'], ['users.id']),
        sa.ForeignKeyConstraint(['open_house_event_id'], ['open_house_events.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_lead_notes_lead_id', 'lead_notes', ['lead_id'])

    op.add_column('open_house_visitors', sa.Column('lead_id', sa.String(), nullable=True))
    op.create_foreign_key('fk_open_house_visitors_lead_id', 'open_house_visitors', 'leads', ['lead_id'], ['id'])
    op.create_index('ix_open_house_visitors_lead_id', 'open_house_visitors', ['lead_id'])

    _backfill_visitor_links_and_notes()


def _backfill_visitor_links_and_notes() -> None:
    """
    One-time backfill so existing open house visitors get linked to their matching Lead, and
    any pre-existing single-blob notes (Lead.notes, OpenHouseVisitor.notes) survive as real
    LeadNote rows instead of being silently dropped once the UI stops reading those columns.
    Never creates a new Lead here - visitors that can't be matched are simply left unlinked.
    """
    conn = op.get_bind()

    visitors = conn.execute(sa.text(
        "SELECT v.id, v.email, v.phone, v.notes, v.created_at, v.open_house_event_id, "
        "e.agent_id "
        "FROM open_house_visitors v "
        "JOIN open_house_events e ON e.id = v.open_house_event_id"
    )).fetchall()

    for visitor in visitors:
        lead_id = None

        # 1) Most reliable: the LeadEvent already created for this visitor's sign-in.
        event_row = conn.execute(
            sa.text(
                "SELECT lead_id FROM lead_events WHERE source_type = 'open_house_visitor' "
                "AND source_id = :visitor_id LIMIT 1"
            ),
            {"visitor_id": visitor.id},
        ).fetchone()
        if event_row:
            lead_id = event_row.lead_id

        # 2) Fallback: match by normalized email, then phone, within the same owner.
        if not lead_id:
            norm_email = _normalize_email(visitor.email)
            norm_phone = _normalize_phone(visitor.phone)
            if norm_email:
                row = conn.execute(
                    sa.text("SELECT id FROM leads WHERE owner_id = :owner_id AND email = :email LIMIT 1"),
                    {"owner_id": visitor.agent_id, "email": norm_email},
                ).fetchone()
                if row:
                    lead_id = row.id
            if not lead_id and norm_phone:
                row = conn.execute(
                    sa.text("SELECT id FROM leads WHERE owner_id = :owner_id AND phone = :phone LIMIT 1"),
                    {"owner_id": visitor.agent_id, "phone": norm_phone},
                ).fetchone()
                if row:
                    lead_id = row.id

        if lead_id:
            conn.execute(
                sa.text("UPDATE open_house_visitors SET lead_id = :lead_id WHERE id = :id"),
                {"lead_id": lead_id, "id": visitor.id},
            )

        # Preserve any legacy visitor note as a real LeadNote, labeled with its open house.
        if lead_id and visitor.notes and visitor.notes.strip():
            conn.execute(
                sa.text(
                    "INSERT INTO lead_notes (id, lead_id, author_id, text, open_house_event_id, created_at) "
                    "VALUES (:id, :lead_id, NULL, :text, :open_house_event_id, :created_at)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "lead_id": lead_id,
                    "text": visitor.notes,
                    "open_house_event_id": visitor.open_house_event_id,
                    "created_at": visitor.created_at,
                },
            )

    # Preserve any legacy lead note the same way (general note, no open house tag).
    leads = conn.execute(sa.text(
        "SELECT id, notes, updated_at, created_at FROM leads WHERE notes IS NOT NULL AND trim(notes) != ''"
    )).fetchall()
    for lead in leads:
        conn.execute(
            sa.text(
                "INSERT INTO lead_notes (id, lead_id, author_id, text, open_house_event_id, created_at) "
                "VALUES (:id, :lead_id, NULL, :text, NULL, :created_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "lead_id": lead.id,
                "text": lead.notes,
                "created_at": lead.updated_at or lead.created_at,
            },
        )


def downgrade() -> None:
    op.drop_index('ix_open_house_visitors_lead_id', table_name='open_house_visitors')
    op.drop_constraint('fk_open_house_visitors_lead_id', 'open_house_visitors', type_='foreignkey')
    op.drop_column('open_house_visitors', 'lead_id')

    op.drop_index('ix_lead_notes_lead_id', table_name='lead_notes')
    op.drop_table('lead_notes')
