"""Bright MLS open house slots for a listing.

Bright keeps open houses in their own BrightOpenHouses resource - a listing can have several
(a Saturday and a Sunday, next weekend's too). The open house sync merges every row it sees into
Property.mls_open_houses, a JSON list of slots:

    {"key": <OpenHouseKey, or "date|start" when Bright doesn't send one>,
     "date": "YYYY-MM-DD", "start": "HH:MM:SS" | None, "end": "HH:MM:SS" | None}

The older single columns (mls_open_house_date / _start_time / _end_time) are still kept as "the
soonest upcoming slot" for anything that only reads one.

Everything here is pure (no DB, no Bright calls) so the merge rules can be unit-tested with fake
rows - see tests/test_mls_open_houses.py.
"""
from datetime import date, datetime, time
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from app.utils.mls_mapper import parse_dt

# Bright's OpenHouseStartTime/EndTime are real UTC ("...T15:00:00Z" = 11:00 AM in Exton in
# October). Every listing we sync is in Bright's region, which is all Eastern time.
MARKET_TZ = ZoneInfo("America/New_York")


def _clock(t: Optional[time]) -> Optional[str]:
    return t.strftime("%H:%M:%S") if t else None


def _local_clock(dt: Optional[datetime]) -> Optional[str]:
    """The open house's wall-clock time where the home is (Eastern), from Bright's UTC value."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(MARKET_TZ)
    return _clock(dt.time())


def slot_from_row(row: Dict[str, Any]) -> Optional[dict]:
    """One BrightOpenHouses row -> a stored slot, or None if it has no usable date.
    Times are converted from Bright's UTC to Eastern wall-clock time (see MARKET_TZ)."""
    oh_date = parse_dt(row.get("OpenHouseDate"))
    if oh_date is None:
        return None
    start_dt = parse_dt(row.get("OpenHouseStartTime"))
    end_dt = parse_dt(row.get("OpenHouseEndTime"))
    d = oh_date.date().isoformat()
    start = _local_clock(start_dt)
    key = str(row.get("OpenHouseKey") or "").strip() or f"{d}|{start or ''}"
    return {"key": key, "date": d, "start": start, "end": _local_clock(end_dt)}


def _same_slot(a: dict, b: dict) -> bool:
    return a.get("key") == b.get("key") or (a.get("date") == b.get("date") and (a.get("start") or "") == (b.get("start") or ""))


def _sorted(slots: Iterable[dict]) -> List[dict]:
    return sorted(slots, key=lambda s: (s.get("date") or "", s.get("start") or ""))


def drop_past(slots: Iterable[dict], today: date) -> List[dict]:
    t = today.isoformat()
    return [s for s in slots if isinstance(s, dict) and (s.get("date") or "") >= t]


def merge_open_house_rows(existing: Optional[list], rows: Iterable[Dict[str, Any]], today: date) -> List[dict]:
    """Applies BrightOpenHouses rows (in feed order - later rows win) onto a listing's stored slots.

    Simple on purpose (product rule): once we see an open house it stays on the listing until its
    day passes - no cancellation handling.
    - a row is upserted by its key (and replaces a slot at the same date+start);
    - past-dated slots are dropped at the end, so an edit to an old open house can never knock
      out an upcoming one.
    """
    slots = [dict(s) for s in (existing or []) if isinstance(s, dict) and s.get("date")]
    for row in rows:
        slot = slot_from_row(row)
        if slot is None:
            continue
        slots = [s for s in slots if not _same_slot(s, slot)]
        slots.append(slot)
    return _sorted(drop_past(slots, today))


def single_columns(slots: List[dict]) -> dict:
    """The legacy "soonest upcoming" columns for a (sorted, future-only) slot list."""
    first = slots[0] if slots else None
    if not first:
        return {"mls_open_house_date": None, "mls_open_house_start_time": None, "mls_open_house_end_time": None}
    return {
        "mls_open_house_date": date.fromisoformat(first["date"]),
        "mls_open_house_start_time": time.fromisoformat(first["start"]) if first.get("start") else None,
        "mls_open_house_end_time": time.fromisoformat(first["end"]) if first.get("end") else None,
    }


def upcoming_mls_open_houses(prop: Any, today: Optional[date] = None) -> List[dict]:
    """Every upcoming Bright open house for a listing, soonest first, as
    {"date": date, "start": time | None, "end": time | None}. Falls back to the single legacy
    columns for rows synced before the list existed."""
    today = today or date.today()
    out: List[dict] = []
    for s in getattr(prop, "mls_open_houses", None) or []:
        if not isinstance(s, dict):
            continue
        try:
            d = date.fromisoformat(str(s.get("date")))
            start = time.fromisoformat(s["start"]) if s.get("start") else None
            end = time.fromisoformat(s["end"]) if s.get("end") else None
        except (ValueError, TypeError):
            continue
        if d >= today:
            out.append({"date": d, "start": start, "end": end})
    if not out:
        d = getattr(prop, "mls_open_house_date", None)
        if d and d >= today:
            out.append({"date": d, "start": getattr(prop, "mls_open_house_start_time", None), "end": getattr(prop, "mls_open_house_end_time", None)})
    out.sort(key=lambda s: (s["date"], s["start"] or time.min))
    return out


def open_houses_payload(prop: Any, today: Optional[date] = None) -> List[dict]:
    """upcoming_mls_open_houses as plain JSON ({"date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM"})
    for public pages (Agent Hub, IDX)."""
    return [
        {"date": s["date"].isoformat(), "start": s["start"].strftime("%H:%M") if s["start"] else None,
         "end": s["end"].strftime("%H:%M") if s["end"] else None}
        for s in upcoming_mls_open_houses(prop, today)
    ]


def rebuild_open_house_slots(existing: Optional[list], rows: Iterable[Dict[str, Any]], today: date) -> List[dict]:
    """The daily pass: adds anything in Bright's full upcoming set the incremental feed missed,
    keeps every slot already on the listing, and drops slots whose day has passed. Additive only -
    an open house never disappears before its day."""
    return merge_open_house_rows(existing, rows, today)


def stored_slots(mls_open_houses: Optional[list], legacy_date: Optional[date], legacy_start: Optional[time],
                 legacy_end: Optional[time]) -> List[dict]:
    """What the sync merges onto: the stored list, or - for a row synced before the list existed -
    its single legacy slot."""
    if mls_open_houses:
        return [s for s in mls_open_houses if isinstance(s, dict)]
    if legacy_date:
        d, start = legacy_date.isoformat(), _clock(legacy_start)
        return [{"key": f"{d}|{start or ''}", "date": d, "start": start, "end": _clock(legacy_end)}]
    return []
