"""The agent's own listings and sales as small home cards in the chat (owner, 11/01: "it shouldn't just
be text" - a tiny photo, the address, the details and price; a long list shows the newest five and
"+N more"). The model calls show_homes; the homes come from the same copy of the MLS the Agent Hub
uses (website/content_adapters), never from what the model typed."""
import uuid
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Property, User
from app.services.email_service import tidy_caps

TOOL = {
    "name": "show_homes",
    "description": (
        "Show homes as property cards under your reply (photo, status, price, address, beds/baths/sq ft; 6 a page with page "
        "buttons; tapping one opens the full home with Next/Previous) - ALWAYS use it whenever your reply is about specific homes: "
        "the agent's own listings or sales (which='my_listings' or 'my_sales'; pass listing_keys to show just those, in that "
        "order) or ANY homes a lookup returned - search_properties, property details, matches (which='homes' + listing_keys). "
        "Then keep your text to one or two lines (a count, what stands out) - never write the homes out as a list."),
    "input_schema": {"type": "object", "properties": {
        "which": {"type": "string", "enum": ["my_listings", "my_sales", "homes"]},
        "listing_keys": {"type": "array", "items": {"type": "string"}},
        "title": {"type": "string"}}, "required": ["which"]},
}
NAME = TOOL["name"]
_STATUS = {"ACTIVE": "Active", "COMING SOON": "Coming soon", "PENDING": "Pending", "ACTIVE UNDER CONTRACT": "Under contract", "CLOSED": "Sold", "SOLD": "Sold"}


def _status(raw) -> str:
    s = str(raw or "").upper().replace("-BRIGHT", "").strip()
    return _STATUS.get(s, s.title() if s else "")


def _card(h: dict, sold: bool) -> dict:
    addr = str(h.get("address") or "").split(",")[0].strip()
    date = str(h.get("sold_date") or "")[:10] if sold else ""
    return {"listing_key": h.get("listing_key"), "address": addr, "city": h.get("city") or "",
            "price": float(h["price"]) if h.get("price") else None, "beds": h.get("bedrooms"), "baths": h.get("bathrooms"),
            "sqft": h.get("living_area"), "status": "Sold" if sold else _status(h.get("status")), "date": date or None,
            "photo": h.get("photo") if isinstance(h.get("photo"), str) and not str(h.get("photo")).startswith("data:") else None}


async def run(db: AsyncSession, user: User, args: dict) -> Tuple[dict, Optional[dict]]:
    from app.utils.auth import tier_has_mls
    from app.services.website import content_adapters as ca
    which = args.get("which") or "my_listings"
    keys = [str(k) for k in (args.get("listing_keys") or []) if k][:40]
    if which in ("my_listings", "my_sales") and (not user.mls_id or not user.is_admin and (not tier_has_mls(user.plan_tier) or not user.broker_authorized)):
        return {"error": "no_mls", "message": "Their listings and sales need an MLS plan with broker approval."}, None
    try:
        if which == "my_sales":
            homes = [_card(h, True) for h in await ca.get_recent_sales(db, user, limit=40)]
        elif which == "my_listings":
            homes = [_card(h, False) for h in await ca.get_active_listings(db, user, limit=40)]
        else:
            rows = (await db.execute(select(Property).where(Property.listing_key.in_(keys)))).scalars().all() if keys else []
            homes = [_card({"listing_key": p.listing_key, "address": tidy_caps(p.street_address or p.full_address), "city": tidy_caps(p.city),
                            "price": p.price, "bedrooms": p.bedrooms, "bathrooms": p.bathrooms, "living_area": p.living_area, "status": p.home_status,
                            "photo": ca._property_photo(p)}, False) for p in rows]
    except Exception:  # noqa: BLE001 - the reply still works without the cards
        await db.rollback()
        return {"error": "lookup_failed"}, None
    if keys:
        by = {h["listing_key"]: h for h in homes}
        homes = [by[k] for k in keys if k in by]
    if not homes:
        return {"shown": 0, "message": "No homes to show."}, None
    entry = {"tool": NAME, "record_type": "homes", "record_id": str(uuid.uuid4()),
             "title": str(args.get("title") or "")[:60] or None, "homes": homes, "which": which}
    # The note keeps it from narrating the cards ("I'll show them as cards", 11/02)
    return {"shown": len(homes), "homes": [f"{h['address']} {h['status']} {h['price'] or ''}" for h in homes[:10]],
            "note": "They now see these as cards under your reply - don't mention the cards; say something useful about the homes (a count, the newest, days on market)."}, entry


# Any reply that names homes a lookup returned gets them as cards (owner, 11/09: "shouldn't list all the
# properties in text ... combine these all on a nice property card" - for every question about a home).
# The lookups' homes are remembered as the turn runs; the finished reply's addresses pick which show.
import re as _re

_STREET = _re.compile(r"[^a-z0-9 ]+")


def _street_key(addr: str) -> str:
    s = _STREET.sub(" ", str(addr or "").split(",")[0].lower())
    words = s.split()
    # "602 holywell dr" -> "602 holywell": the number and the first name word are enough to spot it
    return " ".join(words[:2]) if len(words) >= 2 and words[0][:1].isdigit() else ""


def collect(result, seen: dict, depth: int = 0) -> None:
    """Remember every home (listing_key + address) a tool result carries."""
    if depth > 4 or len(seen) > 400:
        return
    if isinstance(result, dict):
        k, a = result.get("listing_key"), result.get("address") or result.get("street_address") or result.get("full_address")
        if k and a and isinstance(a, str):
            key = _street_key(a)
            if key:
                seen.setdefault(str(k), key)
        for v in result.values():
            if isinstance(v, (dict, list)):
                collect(v, seen, depth + 1)
    elif isinstance(result, list):
        for v in result[:200]:
            collect(v, seen, depth + 1)


def mentioned(text: str, seen: dict) -> list:
    """The listing keys whose street the reply names, in the order it names them."""
    low = " " + _STREET.sub(" ", (text or "").lower()) + " "
    low = " ".join(low.split())
    hits = []
    for k, street in seen.items():
        i = (" " + low + " ").find(" " + street + " ")
        if i >= 0:
            hits.append((i, k))
    return [k for _, k in sorted(hits)]


NUDGE = ("(App note: you wrote the homes out as a text list. Call show_homes(which='homes', listing_keys=[...]) with the "
         "listing_keys of the homes you meant, in your order, then reply in one or two short lines (how many, what stands "
         "out) - never list the addresses, prices or beds in text: the cards show all that.)")

SHORTEN = ("(App note: those homes already show as property cards under your reply (photo, price, beds, baths, size - "
           "tap one to open it). Write your reply again in one or two short lines - how many and what stands out, at most "
           "two homes named - with no list of addresses, prices or beds. Don't call show_homes again.)")
