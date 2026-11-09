"""The chat's Showcase card (owner, 11/08: "use this exact UI whenever it's referencing properties in a
Showcase, instead of listing them out as text - with the option to remove them or recommend them,
and Send"). Whenever a chat tool reads a client's Showcase (search_showcase_homes, get_property_matches,
get_showcase_homes), the reply carries one card per Showcase: every home with its photo, price, beds /
baths, what the client did with it, and - for a search - why it matched. The card's own buttons do the
work in the agent's browser through the app's own routes (recommend, remove, send), the same as the
Showcase page, so the model never has to list homes in text.

The rows also feed the "Remove homes" confirm card (ai_actions_catalog.remove_showcase_homes)."""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Collection, Property, PropertyComment, PropertyInteraction, PropertyTour, User, collection_properties
from app.services.email_service import tidy_caps

ROWS_MAX = 500
CARD_TOOLS = ("search_showcase_homes", "get_property_matches", "get_showcase_homes")


async def showcase_rows(db: AsyncSession, c: Collection) -> List[Dict[str, Any]]:
    """Every home in the Showcase right now (a removed one is gone), newest-added first."""
    from app.services.collections_service import _is_property_available, _ai_starred_ids
    from app.services.showcase_smart_filter import match_line
    rows = (await db.execute(
        select(Property, collection_properties.c.sold_filed_at, collection_properties.c.added_at, collection_properties.c.ai_match)
        .join(collection_properties, collection_properties.c.property_id == Property.id)
        .where(collection_properties.c.collection_id == c.id))).all()
    ix = {i.property_id: i for i in (await db.execute(select(PropertyInteraction).where(PropertyInteraction.collection_id == c.id))).scalars()}
    talk = {pid for (pid,) in (await db.execute(select(PropertyComment.property_id).where(PropertyComment.collection_id == c.id).distinct())).all()}
    toured = {pid for (pid,) in (await db.execute(select(PropertyTour.property_id).where(PropertyTour.collection_id == c.id).distinct())).all()}
    # the must-haves line per home (+ its sentence words) - the Send sheet's "Mention the must-haves" uses it
    smart = {p.id: match_line(m) for p, _f, _a, m in rows}
    await _ai_starred_ids(db, c.id, smart)
    out = []
    for p, filed, added, _m in rows:
        i = ix.get(p.id)
        tags = []
        if i and i.liked:
            tags.append("Loved")
        if i and i.disliked:
            tags.append("Passed")
        if p.id in talk:
            tags.append("Commented")
        if p.id in toured:
            tags.append("Tour ask")
        status = (p.home_status or "").replace("-BRIGHT", "").strip().title() or "Listed"
        out.append({
            "id": p.id, "key": p.listing_key, "street": tidy_caps(p.street_address or p.full_address or ""), "city": tidy_caps(p.city or ""),
            "state": p.state, "price": p.price, "beds": p.bedrooms, "baths": p.bathrooms, "sqft": p.living_area, "photo": p.img_src,
            "status": None if _is_property_available(p.home_status) else ("Sold/Removed folder" if filed else status), "tags": tags,
            "recommended": bool(i and i.agent_recommended_at and not i.disliked), "_added": added.isoformat() if added else "",
            "smart": smart.get(p.id),
        })
    out.sort(key=lambda h: h.pop("_added"), reverse=True)
    return out[:ROWS_MAX]


def collection_info(c: Collection) -> Dict[str, Any]:
    first, _, last = (c.visitor_name or "").partition(" ")
    return {"id": c.id, "name": f"{c.visitor_name}'s Showcase" if c.visitor_name else (c.name or "Showcase"), "client": first,
            "first": first, "last": last, "email": c.visitor_email, "phone": c.visitor_phone,
            "share_token": c.share_token, "short_code": getattr(c, "short_code", None), "is_public": bool(c.is_public), "status": c.status}


async def card_ref(db: AsyncSession, user: User, collection_id: str, matched: Optional[Dict[str, List[str]]] = None,
                   looked_for: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    c = (await db.execute(select(Collection).where(Collection.id == collection_id, Collection.owner_id == user.id))).scalar_one_or_none()
    if c is None:
        return None
    return {"tool": "showcase_card", "record_type": "showcase_homes", "record_id": str(uuid.uuid4()), "collection": collection_info(c),
            "homes": await showcase_rows(db, c), "matched": matched or {}, "looked_for": looked_for or [], "searched": matched is not None}


def matched_from(result: Dict[str, Any]) -> Dict[str, List[str]]:
    """search_showcase_homes' hits (and one-level homes) -> property id: why it matched."""
    out: Dict[str, List[str]] = {}
    for m in (result.get("matches") or []) + (result.get("one_level_homes") or []):
        pid = m.get("property_id") or m.get("id")
        if pid:
            out[str(pid)] = [str(x) for x in (m.get("found") or [])][:2]
    return out


MARK_TOOL = {
    "name": "mark_showcase_matches",
    "description": "After search_showcase_homes: mark matches on this reply's Showcase card that aren't real (the listing says 'pool table', "
                   "'community pool', 'no pool', 'could add a bedroom', a lower-level in-law suite...). The card shows them apart, greyed, "
                   "with your few-word reason - so never list them in text; just say how many aren't real.",
    "input_schema": {"type": "object", "properties": {
        "collection_id": {"type": "string"},
        "not_real": {"type": "array", "items": {"type": "object", "properties": {
            "listing_key": {"type": "string"}, "why": {"type": "string", "description": "3-6 words, e.g. 'a pool table, not a pool'"}},
            "required": ["listing_key", "why"]}},
    }, "required": ["not_real"]},
}


def mark_doubtful(cards: Dict[str, dict], args: dict) -> Dict[str, Any]:
    items = [x for x in (args.get("not_real") or []) if isinstance(x, dict) and x.get("listing_key")][:60]
    cid = str(args.get("collection_id") or "")
    card = cards.get(cid) or (next(iter(cards.values())) if len(cards) == 1 else None)
    if card is None:
        return {"error": "no_card", "message": "Call search_showcase_homes first in this reply."}
    by_key = {h["key"]: h["id"] for h in card["homes"]}
    marked = 0
    for x in items:
        pid = by_key.get(str(x["listing_key"]))
        if pid:
            card.setdefault("doubtful", {})[pid] = str(x.get("why") or "")[:80]
            marked += 1
    return {"marked": marked, "note": "Shown apart on the card with your reasons. Say only how many aren't real matches - don't list them."}


# A street address the way listings write it ("606 Longchamps Dr", "37 Sagewood Dr #14").
_ADDRESS = re.compile(r"\b\d{1,5}\s+(?:[NSEW]\.?\s+)?[A-Z][A-Za-z']+(?:\s+[A-Z][A-Za-z']+){0,2}\s+(?:Rd|Road|Ln|Lane|Dr|Drive|St|Street|Ave|Avenue|Ct|Court|Way|Pl|Place|Cir|Circle|Blvd|Pike|Trl|Trail|Ter|Xing|Pkwy|Run|Rdg|Hl|Hill)\b")


def lists_homes(text: str) -> bool:
    """More than 2 different street addresses in a reply = a text list of homes."""
    return len(set(m.group(0).lower() for m in _ADDRESS.finditer(text or ""))) > 2


LIST_NUDGE = ("(App note: the homes are already on the Showcase card under your reply, so don't list them. If some matches aren't real "
              "(a pool table, a community pool, room for a pool...), call mark_showcase_matches with them now - the card shows them "
              "greyed with your reason. Then reply again in 2-3 short lines: how many you checked, how many are real matches (name at "
              "most 2), how many aren't, and that they can recommend, send or remove any of them on the card.)")


CARD_NOTE = ("These homes ALREADY show under your reply as {name}'s Showcase card - photos, {what}, each with Recommend, Send and "
             "Remove buttons, plus Select for several at once. NEVER list homes in text: name at most 2 homes in the whole reply. Reply "
             "in 2-3 short lines: what you checked and how many {count_word}, the 1-2 strongest with a few quoted words if there are "
             "any, and that they can recommend, send or remove any of them right on the card. If some matches aren't real (e.g. 'no "
             "pool', a pool table, a community pool), call mark_showcase_matches with them and say only how many - never list them. To take homes out, use remove_showcase_homes "
             "(off_market true for 'the sold / under contract ones' - never judge that from a short list).")
