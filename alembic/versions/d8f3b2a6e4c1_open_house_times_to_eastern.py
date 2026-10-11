"""Bright open house times were stored as UTC clock times - convert them to Eastern

Bright sends OpenHouseStartTime/EndTime in real UTC ("2026-10-04T15:00:00Z" = 11:00 AM EDT), but
the sync used to keep just the clock part, so every Bright open house showed 4-5 hours late
(3:00 PM instead of 11:00 AM). The sync now converts (mls_open_houses.MARKET_TZ); this converts
what's already stored: each slot in properties.mls_open_houses plus the single soonest-slot
columns. Data only. Run once - running it again would shift the times a second time.

Revision ID: d8f3b2a6e4c1
Revises: b6e2d9f4c8a1
Create Date: 2026-10-04
"""
import json
from datetime import date, datetime, time, timezone
from typing import Optional, Sequence, Union
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision: str = "d8f3b2a6e4c1"
down_revision: Union[str, None] = "b6e2d9f4c8a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EASTERN = ZoneInfo("America/New_York")


def _to_eastern(day: Optional[date], t: Optional[time]) -> Optional[time]:
    if day is None or t is None:
        return t
    return datetime.combine(day, t, tzinfo=timezone.utc).astimezone(EASTERN).time().replace(tzinfo=None)


def _slot_clock(day: str, hhmmss: Optional[str]) -> Optional[str]:
    if not day or not hhmmss:
        return hhmmss
    try:
        t = time.fromisoformat(hhmmss)
        return _to_eastern(date.fromisoformat(day), t).strftime("%H:%M:%S")
    except ValueError:
        return hhmmss


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, mls_open_houses, mls_open_house_date, mls_open_house_start_time, mls_open_house_end_time "
        "FROM properties WHERE mls_open_houses IS NOT NULL OR mls_open_house_start_time IS NOT NULL OR mls_open_house_end_time IS NOT NULL"
    )).fetchall()
    for r in rows:
        slots = r.mls_open_houses
        if isinstance(slots, str):
            slots = json.loads(slots)
        new_slots = None
        if isinstance(slots, list):
            new_slots = [
                {**s, "start": _slot_clock(s.get("date"), s.get("start")), "end": _slot_clock(s.get("date"), s.get("end"))}
                if isinstance(s, dict) else s
                for s in slots
            ]
        conn.execute(
            sa.text(
                "UPDATE properties SET mls_open_houses = CAST(:slots AS jsonb), mls_open_house_start_time = :st, mls_open_house_end_time = :et "
                "WHERE id = :k"
            ),
            {
                "slots": json.dumps(new_slots) if new_slots is not None else None,
                "st": _to_eastern(r.mls_open_house_date, r.mls_open_house_start_time),
                "et": _to_eastern(r.mls_open_house_date, r.mls_open_house_end_time),
                "k": r.id,
            },
        )


def downgrade() -> None:
    pass
