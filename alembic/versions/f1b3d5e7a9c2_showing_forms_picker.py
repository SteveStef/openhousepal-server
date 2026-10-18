"""Old standard Schedule a Showing forms -> today's questions, so the day-and-time picker (which
crosses out the agent's busy times) runs for them too. Same rule as forms_routes.upgrade_legacy_showing
(which also upgrades on the agent's next forms load); this does every agent at once on deploy.
Self-contained on purpose - a snapshot of today's questions, no app imports.

Revision ID: f1b3d5e7a9c2
Revises: e6a8c0d2f4b5
Create Date: 2026-10-18 19:00:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1b3d5e7a9c2'
down_revision: Union[str, None] = 'e6a8c0d2f4b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SHOWING_QUESTIONS = json.loads(r'''[
 {
  "id": "property_of_interest",
  "type": "short_text",
  "label": "What property are you interested in seeing?",
  "required": true,
  "locked": true
 },
 {
  "id": "preferred_times",
  "type": "contact_group",
  "label": "What 2 dates and times would work for you?",
  "required": true,
  "locked": true,
  "fields": [
   {
    "id": "preferred_date_1",
    "type": "date",
    "label": "First choice - date",
    "required": true
   },
   {
    "id": "preferred_time_1",
    "type": "time",
    "label": "First choice - time",
    "required": true
   },
   {
    "id": "preferred_date_2",
    "type": "date",
    "label": "Second choice - date",
    "required": false
   },
   {
    "id": "preferred_time_2",
    "type": "time",
    "label": "Second choice - time",
    "required": false
   }
  ]
 },
 {
  "id": "contact_info",
  "type": "contact_group",
  "label": "Your Contact Information",
  "required": true,
  "locked": true,
  "fields": [
   {
    "id": "full_name",
    "type": "short_text",
    "label": "Name",
    "required": true
   },
   {
    "id": "phone",
    "type": "phone",
    "label": "Phone number",
    "required": true
   },
   {
    "id": "email",
    "type": "email",
    "label": "Email",
    "required": true
   }
  ]
 }
]''')
LEGACY_IDS = {"showing_details", "property_of_interest", "full_name", "phone", "email", "availability",
              "preferred_times", "contact_info", "message", "showing_note"} | {
    f"preferred_{k}_{n}" for k in ("date", "time") for n in (1, 2, 3)}
FLOW_IDS = {"property_of_interest", "preferred_times", "contact_info"}


def _upgraded(questions):
    asked = [q for q in (questions or []) if q.get("type") != "statement"]
    ids = {q.get("id") for q in asked}
    if "property_of_interest" in ids and ids <= FLOW_IDS:
        return None
    every = set(ids)
    for q in asked:
        every |= {f.get("id") for f in (q.get("fields") or [])}
    if not asked or "property_of_interest" not in every or not every <= LEGACY_IDS:
        return None
    return SHOWING_QUESTIONS


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, questions FROM agent_forms WHERE form_type = 'showing'")).fetchall()
    for form_id, questions in rows:
        if isinstance(questions, str):
            questions = json.loads(questions)
        new = _upgraded(questions)
        if new is not None:
            conn.execute(sa.text("UPDATE agent_forms SET questions = CAST(:q AS jsonb) WHERE id = :i"),
                         {"q": json.dumps(new), "i": form_id})


def downgrade() -> None:
    pass
