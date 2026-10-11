"""Where a visitor came from before they reached out on a Spotlight or a listing page.

Links we hand out carry a small tag - `?src=` (+ `oh=` for an open house): the per-home QR codes
on an open house's Similar Homes sheet, the Agent Hub's listing cards, the email signature. The
public page keeps it for the visit (sessionStorage, frontend lib/visitSource.ts) and sends it with
the message / tour request as `came_from`: {src, oh, ref}. `ref` is the referring site's host when
there was no tag (someone clicked through from Google, Facebook...). An empty {} means the page
was opened straight from a typed / pasted link.

Stored on the LeadEvent's raw_snapshot["came_from"]; described for the lead's Activity, the
follow-up card and the calendar alert by describe_came_from()."""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

# src tag -> what the agent reads
_SRC_TEXT = {
    "hub": "Clicked it on your Agent Hub",
    "email": "Clicked it in your email signature",
    "site": "Found it on your website",
    "share": "Opened a link you shared",
}
_HOST_RE = re.compile(r"^[a-z0-9.-]{1,120}$")
_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def with_src(url: Optional[str], src: str) -> Optional[str]:
    """Add the ?src= tag to a link we hand out (None stays None)."""
    if not url or "src=" in url:
        return url
    base, _, frag = url.partition("#")
    return f"{base}{'&' if '?' in base else '?'}src={src}" + (f"#{frag}" if frag else "")


def clean_came_from(raw) -> Optional[dict]:
    """Keep only the known, well-formed bits of what the browser sent (it's public input)."""
    if not isinstance(raw, dict):
        return None
    out = {}
    src = str(raw.get("src") or "").strip().lower()[:24]
    if src in _SRC_TEXT or src in ("similar_qr", "signin", "pass"):
        out["src"] = src
    oh = str(raw.get("oh") or "").strip()
    if oh and _ID_RE.match(oh):
        out["oh"] = oh
    ref = str(raw.get("ref") or "").strip().lower()
    ref = ref[4:] if ref.startswith("www.") else ref
    if ref and _HOST_RE.match(ref) and "openhousepal" not in ref:
        out["ref"] = ref
    return out


async def _open_house_label(db: AsyncSession, came_from: dict, owner_id: Optional[str]) -> tuple:
    """("847 Lesley Rd", " (Sep 7)") for the open house in came_from["oh"], or (None, "")."""
    from app.services.tasks_service import display_address
    if not came_from.get("oh"):
        return None, ""
    from app.models.database import OpenHouseEvent
    oh = await db.get(OpenHouseEvent, came_from["oh"])
    if oh is None or (owner_id is not None and oh.agent_id != owner_id):
        return None, ""
    day = getattr(oh, "scheduled_date", None) or (oh.created_at.date() if getattr(oh, "created_at", None) else None)
    return display_address(oh.address, oh.city, oh.state)[1] or None, (f" ({day:%b} {day.day})" if day else "")


async def describe_came_from(db: AsyncSession, came_from, owner_id: Optional[str] = None,
                             home: Optional[str] = None) -> Optional[str]:
    """One line for the agent. None when nothing was recorded (touch points from before tracking).
    `home` = the listing they reached out about (named in the Similar Homes QR line)."""
    if not isinstance(came_from, dict):
        return None
    src = came_from.get("src")
    if src == "pass":
        where, when = await _open_house_label(db, came_from, owner_id)
        return (f"Opened it from their Guest Pass after signing in at your {where} open house{when}" if where
                else "Opened it from their Guest Pass after signing in at your open house")
    if src == "signin":
        from app.services.tasks_service import display_address
        where = when = None
        if came_from.get("oh"):
            from app.models.database import OpenHouseEvent
            oh = await db.get(OpenHouseEvent, came_from["oh"])
            if oh is not None and (owner_id is None or oh.agent_id == owner_id):
                where = display_address(oh.address, oh.city, oh.state)[1] or None
                day = getattr(oh, "scheduled_date", None) or (oh.created_at.date() if getattr(oh, "created_at", None) else None)
                when = f" ({day:%b} {day.day})" if day else ""
        return (f"Signed in at your {where} open house{when}, which took them to your Spotlight" if where
                else "Signed in at your open house, which took them to your Spotlight")
    if src == "similar_qr":
        from app.services.tasks_service import display_address
        where = when = None
        if came_from.get("oh"):
            from app.models.database import OpenHouseEvent
            oh = await db.get(OpenHouseEvent, came_from["oh"])
            if oh is not None and (owner_id is None or oh.agent_id == owner_id):
                where = display_address(oh.address, oh.city, oh.state)[1] or None
                day = getattr(oh, "scheduled_date", None) or (oh.created_at.date() if getattr(oh, "created_at", None) else None)
                when = f" ({day:%b} {day.day})" if day else ""
        home = display_address(home)[0] if home else None  # "8008 GRAY HAVEN RD" -> "8008 Gray Haven Rd"
        qr = f"the QR under {home}" if home else "a home's QR"
        return (f"Scanned {qr} on your Similar Homes sheet from your {where} open house{when}" if where
                else f"Scanned {qr} on one of your open houses' Similar Homes sheets")
    if src in _SRC_TEXT:
        return _SRC_TEXT[src]
    if came_from.get("ref"):
        return f"Came from {came_from['ref']}"
    return "Opened the link directly"
