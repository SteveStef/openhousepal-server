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
        "Show homes as small cards under your reply (photo, address, beds/baths/sq ft, price) - ALWAYS use it when you list or "
        "talk about the agent's own listings or sales (which='my_listings' or 'my_sales'; pass listing_keys to show just "
        "those, in that order), or specific homes from search_properties (which='homes' + listing_keys). Long lists are fine - "
        "the chat shows the newest five and a '+N more' button. Then keep your text short (a count or a one-line summary) "
        "instead of repeating every address."),
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
             "title": str(args.get("title") or "")[:60] or None, "homes": homes}
    return {"shown": len(homes), "homes": [f"{h['address']} {h['status']} {h['price'] or ''}" for h in homes[:10]]}, entry
