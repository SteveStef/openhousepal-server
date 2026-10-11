"""add_leads_and_lead_events

Revision ID: b3d6f9a2c5e8
Revises: a1b2c3d4e5f6
Create Date: 2026-07-30 00:00:00.000000

"""
import re
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b3d6f9a2c5e8'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
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
        'leads',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('email', sa.String(), nullable=True),
        sa.Column('phone', sa.String(), nullable=True),
        sa.Column('represented', sa.String(), nullable=False, server_default='unknown'),
        sa.Column('status', sa.String(), nullable=False, server_default='new'),
        sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('last_contacted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_leads_owner_id', 'leads', ['owner_id'])
    op.create_index('ix_leads_email', 'leads', ['email'])
    op.create_index('ix_leads_phone', 'leads', ['phone'])

    op.create_table(
        'lead_events',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('lead_id', sa.String(), nullable=False),
        sa.Column('source_type', sa.String(), nullable=False),
        sa.Column('source_id', sa.String(), nullable=True),
        sa.Column('summary', sa.String(), nullable=False),
        sa.Column('property_address', sa.String(), nullable=True),
        sa.Column('open_house_event_id', sa.String(), nullable=True),
        sa.Column('collection_id', sa.String(), nullable=True),
        sa.Column('form_id', sa.String(), nullable=True),
        sa.Column('raw_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['open_house_event_id'], ['open_house_events.id']),
        sa.ForeignKeyConstraint(['collection_id'], ['collections.id']),
        sa.ForeignKeyConstraint(['form_id'], ['agent_forms.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_lead_events_lead_id', 'lead_events', ['lead_id'])

    _backfill_existing_contacts()


def _backfill_existing_contacts() -> None:
    """
    One-time backfill so agents see their existing open house visitors, form submissions,
    and showcase contacts inside /leads immediately, instead of only future activity.
    Mirrors app.services.leads_service.upsert_lead's dedup-by-email-or-phone logic, but as
    plain SQL since Alembic data migrations shouldn't import the ORM app code.
    """
    conn = op.get_bind()

    # lead_id lookup keyed by (owner_id, normalized email or phone)
    leads_by_key: dict[tuple[str, str], str] = {}

    def get_or_create_lead(owner_id, name, email, phone, represented, occurred_at):
        norm_email = _normalize_email(email)
        norm_phone = _normalize_phone(phone)
        if not norm_email and not norm_phone:
            return None

        key = None
        for candidate in (norm_email, norm_phone):
            if candidate and (owner_id, candidate) in leads_by_key:
                key = (owner_id, candidate)
                break

        if key:
            lead_id = leads_by_key[key]
            update_fields = []
            params = {"id": lead_id}
            if name:
                update_fields.append("name = COALESCE(name, :name)")
                params["name"] = name
            if norm_email:
                update_fields.append("email = COALESCE(email, :email)")
                params["email"] = norm_email
            if norm_phone:
                update_fields.append("phone = COALESCE(phone, :phone)")
                params["phone"] = norm_phone
            if represented:
                update_fields.append("represented = CASE WHEN represented = 'unknown' THEN :represented ELSE represented END")
                params["represented"] = represented
            if update_fields:
                conn.execute(sa.text(f"UPDATE leads SET {', '.join(update_fields)} WHERE id = :id"), params)
        else:
            lead_id = str(uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT INTO leads (id, owner_id, name, email, phone, represented, status, tags, created_at) "
                    "VALUES (:id, :owner_id, :name, :email, :phone, :represented, 'new', '[]', :created_at)"
                ),
                {
                    "id": lead_id,
                    "owner_id": owner_id,
                    "name": name,
                    "email": norm_email,
                    "phone": norm_phone,
                    "represented": represented or 'unknown',
                    "created_at": occurred_at,
                },
            )
            for candidate in (norm_email, norm_phone):
                if candidate:
                    leads_by_key[(owner_id, candidate)] = lead_id

        return lead_id

    def add_event(lead_id, source_type, source_id, summary, occurred_at, **fk_fields):
        if not lead_id:
            return
        fields = {"open_house_event_id": None, "collection_id": None, "form_id": None, "property_address": None}
        fields.update(fk_fields)
        conn.execute(
            sa.text(
                "INSERT INTO lead_events (id, lead_id, source_type, source_id, summary, property_address, "
                "open_house_event_id, collection_id, form_id, occurred_at) "
                "VALUES (:id, :lead_id, :source_type, :source_id, :summary, :property_address, "
                ":open_house_event_id, :collection_id, :form_id, :occurred_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "lead_id": lead_id,
                "source_type": source_type,
                "source_id": source_id,
                "summary": summary,
                "property_address": fields["property_address"],
                "open_house_event_id": fields["open_house_event_id"],
                "collection_id": fields["collection_id"],
                "form_id": fields["form_id"],
                "occurred_at": occurred_at,
            },
        )

    # 1) Open house visitors (joined to their open house event for owner_id + address)
    rows = conn.execute(sa.text(
        "SELECT v.id, v.full_name, v.email, v.phone, v.has_agent, v.created_at, "
        "e.agent_id, e.address, e.id as open_house_event_id "
        "FROM open_house_visitors v "
        "JOIN open_house_events e ON e.id = v.open_house_event_id "
        "ORDER BY v.created_at ASC"
    )).fetchall()
    for row in rows:
        represented = 'yes' if row.has_agent == 'YES' else ('no' if row.has_agent == 'NO' else None)
        lead_id = get_or_create_lead(row.agent_id, row.full_name, row.email, row.phone, represented, row.created_at)
        add_event(
            lead_id, 'open_house_visitor', row.id,
            f"Signed in at open house - {row.address or 'Open House'}", row.created_at,
            open_house_event_id=row.open_house_event_id, property_address=row.address,
        )

    # 2) Form submissions
    rows = conn.execute(sa.text(
        "SELECT s.id, s.owner_id, s.contact_name, s.contact_email, s.contact_phone, s.property_address, "
        "s.created_at, f.title, f.id as form_id "
        "FROM form_submissions s JOIN agent_forms f ON f.id = s.form_id "
        "ORDER BY s.created_at ASC"
    )).fetchall()
    for row in rows:
        lead_id = get_or_create_lead(row.owner_id, row.contact_name, row.contact_email, row.contact_phone, None, row.created_at)
        add_event(
            lead_id, 'form_submission', row.id,
            f"Submitted {row.title} form", row.created_at,
            form_id=row.form_id, property_address=row.property_address,
        )

    # 3) Showcase (collection) visitors
    rows = conn.execute(sa.text(
        "SELECT id, owner_id, name, visitor_name, visitor_email, visitor_phone, created_at "
        "FROM collections WHERE visitor_email IS NOT NULL OR visitor_phone IS NOT NULL "
        "ORDER BY created_at ASC"
    )).fetchall()
    for row in rows:
        lead_id = get_or_create_lead(row.owner_id, row.visitor_name, row.visitor_email, row.visitor_phone, None, row.created_at)
        add_event(
            lead_id, 'collection_visitor', row.id,
            f"Showcase created - {row.name}", row.created_at,
            collection_id=row.id,
        )

    # 4) Property tour (showing) requests
    rows = conn.execute(sa.text(
        "SELECT t.id, c.owner_id, t.visitor_name, t.visitor_email, t.visitor_phone, t.created_at, t.collection_id, "
        "p.street_address, p.city, p.state, p.zipcode "
        "FROM property_tours t "
        "JOIN collections c ON c.id = t.collection_id "
        "LEFT JOIN properties p ON p.id = t.property_id "
        "ORDER BY t.created_at ASC"
    )).fetchall()
    for row in rows:
        address = ", ".join(filter(None, [row.street_address, row.city, f"{row.state} {row.zipcode or ''}".strip()])) if row.street_address else None
        lead_id = get_or_create_lead(row.owner_id, row.visitor_name, row.visitor_email, row.visitor_phone, None, row.created_at)
        add_event(
            lead_id, 'property_tour', row.id,
            f"Requested a showing - {address or 'a property'}", row.created_at,
            collection_id=row.collection_id, property_address=address,
        )

    # 5) Property comments
    rows = conn.execute(sa.text(
        "SELECT cm.id, c.owner_id, cm.visitor_name, cm.visitor_email, cm.created_at, cm.collection_id, "
        "p.street_address, p.city, p.state, p.zipcode "
        "FROM property_comments cm "
        "JOIN collections c ON c.id = cm.collection_id "
        "LEFT JOIN properties p ON p.id = cm.property_id "
        "WHERE cm.visitor_email IS NOT NULL OR cm.visitor_name IS NOT NULL "
        "ORDER BY cm.created_at ASC"
    )).fetchall()
    for row in rows:
        address = ", ".join(filter(None, [row.street_address, row.city, f"{row.state} {row.zipcode or ''}".strip()])) if row.street_address else None
        lead_id = get_or_create_lead(row.owner_id, row.visitor_name, row.visitor_email, None, None, row.created_at)
        add_event(
            lead_id, 'property_comment', row.id,
            f"Commented on a property{' - ' + address if address else ''}", row.created_at,
            collection_id=row.collection_id, property_address=address,
        )


def downgrade() -> None:
    op.drop_index('ix_lead_events_lead_id', table_name='lead_events')
    op.drop_table('lead_events')
    op.drop_index('ix_leads_phone', table_name='leads')
    op.drop_index('ix_leads_email', table_name='leads')
    op.drop_index('ix_leads_owner_id', table_name='leads')
    op.drop_table('leads')
