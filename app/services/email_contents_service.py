"""What one client email had in it (owner, 11/18) - the homes, which were new matches and which were
price changes, how many - for the dropdown under every email row (Showcase mailbox, a lead's Emails
card, the Emails page) and the row's photo. Works for unopened emails too.

Where it comes from, best first:
  saved    client_emails.about - the homes recorded when the email went out (since 11/13).
  copy     the queued copy of the same email (scheduled_emails.template_variables) - older digests,
           Showcase invites.
  subject  the subject line itself: "25 Spring Valley Rd dropped to $1,090,000", "12 Oak St just hit
           the market for $950,000", "3 new matches and 1 price drop in your Showcase today".
Photos missing from what was saved are looked up from the agent's own homes by street address.
Never raises: a row with nothing found just has no contents."""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Iterable, Optional

from sqlalchemy import func, select

from app.models.database import ClientEmail, Property, ScheduledEmail

logger = logging.getLogger(__name__)

DROP_RE = re.compile(r"^(?:Your week:\s*|This week:\s*)?(?P<street>.+?) dropped to (?P<price>\$[\d,]+)", re.I)
NEW_RE = re.compile(r"^(?:Your week:\s*|This week:\s*)?(?P<street>.+?) just hit the market(?: for (?P<price>\$[\d,]+))?", re.I)
NEW_N_RE = re.compile(r"(\d+)\s+new (?:match|matches|home|homes)\b", re.I)
DROP_N_RE = re.compile(r"(\d+)\s+price (?:drop|drops)\b", re.I)
PICKED_N_RE = re.compile(r"(\d+)\s+homes?\s+picked|picked\s+(\d+)\s+homes?|shared\s+(\d+)\s+homes?", re.I)
# One named home: a tour confirmation, a reply to their comment, a Spotlight sign-up, an open house follow-up.
ONE_HOME_RES = (
    re.compile(r"^Your tour of (?P<street>.+?) is confirmed", re.I),
    re.compile(r"commented on a property:\s*(?P<street>.+)$", re.I),
    re.compile(r"^You're on the list for (?P<street>.+)$", re.I),
    re.compile(r"open house at (?P<street>.+)$", re.I),
    re.compile(r"homes you might like near (?P<street>.+)$", re.I),
)
SPOTLIGHT_RE = re.compile(r"^(?P<what>Sold|Under contract|Price reduced|Price updated|New open house):\s*(?P<street>.+)$", re.I)
# Digest / invite templates whose homes are matches the client hasn't seen ("new"), vs. the ones that
# just list homes (a thank-you after an open house names the home they toured).
MATCH_TEMPLATES = {"showcase_daily_digest", "showcase_weekly_digest"}
PICK_TEMPLATES = {"showcase_ready", "showcase_shared"}
COPY_WINDOW = timedelta(minutes=30)
REBUILD_MAX = 25   # one small query each - a long activity list rebuilds only its newest ones


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _street_key(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").split(",")[0].strip().lower())


def _kind(h: dict, template: str) -> str:
    if h.get("kind"):
        return str(h["kind"])
    if h.get("is_price_drop") in (True, "true", "True", 1):
        return "price_drop"
    if template in MATCH_TEMPLATES:
        return "new"
    if template in PICK_TEMPLATES:
        return "picked"
    return "home"


def _from_subject(rec: ClientEmail) -> list[dict]:
    subj = (rec.subject or "").strip()
    m = DROP_RE.match(subj)
    if m:
        return [{"street_address": m.group("street").strip(), "new_price": m.group("price"), "price": m.group("price"), "kind": "price_drop"}]
    m = NEW_RE.match(subj)
    if m:
        row = {"street_address": m.group("street").strip(), "kind": "new"}
        if m.group("price"):
            row["price"] = m.group("price")
        return [row]
    for rx in ONE_HOME_RES:
        m = rx.search(subj)
        if m:
            return [{"street_address": m.group("street").strip(" .-"), "kind": "home"}]
    m = SPOTLIGHT_RE.match(subj)
    if m and rec.template == "spotlight_listing_update":
        what = m.group("what").lower()
        kind = "price_drop" if what == "price reduced" else "sold" if what == "sold" else "pending" if what == "under contract" else "price_change" if what == "price updated" else "open_house"
        return [{"street_address": m.group("street").strip(), "kind": kind}]
    return []


def _counts(rec: ClientEmail, homes: list[dict]) -> tuple[int, int, int]:
    """(new matches, price changes, homes in all) - the subject knows the full numbers when only the
    first few homes were kept."""
    subj = rec.subject or ""
    new = sum(1 for h in homes if h.get("kind") == "new")
    drops = sum(1 for h in homes if h.get("kind") in ("price_drop", "price_change"))
    m, d = NEW_N_RE.search(subj), DROP_N_RE.search(subj)
    if m:
        new = max(new, int(m.group(1)))
    if d:
        drops = max(drops, int(d.group(1)))
    total = max(len(homes), new + drops)
    p = PICKED_N_RE.search(subj)
    if p:
        total = max(total, int(next(g for g in p.groups() if g)))
    return new, drops, total


def _summary(rec: ClientEmail, homes: list[dict], new: int, drops: int, total: int) -> str:
    street = homes[0]["street_address"] if homes else ""
    if rec.template == "tour_confirmed":
        return f"Tour confirmation{' for ' + street if street else ''}"
    if rec.template == "agent_comment_response":
        return f"Your reply to their comment{' on ' + street if street else ''}"
    if rec.template == "spotlight_alert_confirm":
        return f"Signed up for updates{' on ' + street if street else ''}"
    if rec.template == "visitor_confirmation":
        return (f"Open house follow-up{' for ' + street if street else ''}"
                + (f" · {_plural(total, 'home', 'homes')} picked for them" if total > 1 else ""))
    one = homes[0] if len(homes) == 1 and total <= 1 else None
    if one is not None:
        k = one.get("kind")
        street = one.get("street_address") or "A home"
        if k == "price_drop":
            was = f" from {one['old_price']}" if one.get("old_price") else ""
            to = one.get("new_price") or one.get("price")
            return f"Price drop: {street}{was}{' to ' + to if to else ''}"
        if k == "price_change":
            return f"Price change: {street}"
        if k == "new":
            return f"New match: {street}{' at ' + one['price'] if one.get('price') else ''}"
        if k == "sold":
            return f"Sold: {street}"
        if k == "pending":
            return f"Under contract: {street}"
        if k == "open_house":
            return f"New open house: {street}"
    parts = []
    if new:
        parts.append(_plural(new, "new match", "new matches"))
    if drops:
        parts.append(_plural(drops, "price drop", "price drops"))
    if parts:
        return " · ".join(parts)
    if total and rec.template in PICK_TEMPLATES:
        return _plural(total, "home picked for them", "homes picked for them")
    if total:
        return _plural(total, "home", "homes")
    return ""


def _photo(homes: list[dict]) -> Optional[str]:
    """The row's picture: a new match first, then a price change, then any home."""
    for kinds in (("new",), ("price_drop", "price_change"), None):
        for h in homes:
            if (kinds is None or h.get("kind") in kinds) and isinstance(h.get("image"), str) and h["image"].startswith("http"):
                return h["image"]
    return None


async def _copies(db, recs: list[ClientEmail]) -> dict[str, dict]:
    """rec.id -> the queued copy's variables, for emails that didn't save their homes."""
    need = [r for r in recs if not r.about and r.created_at is not None]
    if not need:
        return {}
    try:
        lo = min(r.created_at for r in need) - COPY_WINDOW
        hi = max(r.created_at for r in need) + COPY_WINDOW
        owners = {r.owner_id for r in need}
        to = {r.recipient_email for r in need}
        templates = {r.template for r in need}
        at = func.coalesce(ScheduledEmail.sent_at, ScheduledEmail.scheduled_for)
        rows = (await db.execute(select(ScheduledEmail.owner_id, func.lower(ScheduledEmail.recipient_email), ScheduledEmail.template_name,
                                        ScheduledEmail.subject, at, ScheduledEmail.template_variables).where(
            ScheduledEmail.owner_id.in_(owners), func.lower(ScheduledEmail.recipient_email).in_(to),
            ScheduledEmail.template_name.in_(templates), at >= lo, at <= hi))).all()
    except Exception:  # noqa: BLE001
        logger.warning("email contents: copy lookup failed", exc_info=True)
        return {}
    out: dict[str, dict] = {}
    for r in need:
        best, gap = None, None
        for owner, email, tpl, subj, when, vars_ in rows:
            if owner != r.owner_id or email != r.recipient_email or tpl != r.template or when is None:
                continue
            if (subj or "") != (r.subject or "") and (subj or "")[:60] != (r.subject or "")[:60]:
                continue
            g = abs((when - r.created_at).total_seconds())
            if g <= COPY_WINDOW.total_seconds() and (gap is None or g < gap):
                best, gap = vars_, g
        if best:
            out[r.id] = best
    return out


async def _street_photos(db, owner_ids: Iterable[str], streets: set[str]) -> dict[str, str]:
    """street key -> photo, from homes in this agent's Showcases / any stored home with that street."""
    if not streets:
        return {}
    try:
        from app.models.database import Collection, collection_properties
        mine = (select(Property.street_address, Property.img_src)
                .join(collection_properties, collection_properties.c.property_id == Property.id)
                .join(Collection, Collection.id == collection_properties.c.collection_id)
                .where(Collection.owner_id.in_(list(owner_ids)), Property.img_src.isnot(None), Property.img_src != "",
                       func.lower(func.split_part(Property.street_address, ",", 1)).in_(list(streets))).limit(400))
        found: dict[str, str] = {}
        for s, img in (await db.execute(mine)).all():
            found.setdefault(_street_key(s), img)
        left = [s for s in streets if s not in found]
        if left:
            anyp = (select(Property.street_address, Property.img_src)
                    .where(Property.img_src.isnot(None), Property.img_src != "",
                           func.lower(func.split_part(Property.street_address, ",", 1)).in_(left)).limit(200))
            for s, img in (await db.execute(anyp)).all():
                found.setdefault(_street_key(s), img)
        return found
    except Exception:  # noqa: BLE001
        logger.warning("email contents: photo lookup failed", exc_info=True)
        return {}


async def _rebuilt_matches(db, recs: list[ClientEmail]) -> dict[str, list[dict]]:
    """Older Showcase updates that kept no homes: the homes that came into this client's Showcases in
    the day (a week, for a weekly update) before it went out - the ones it would have shown."""
    out: dict[str, list[dict]] = {}
    try:
        from app.models.database import Collection, collection_properties
        from app.services.agent_roundup_service import nice_street
        for r in recs:
            sent = r.sent_at or r.created_at
            if sent is None:
                continue
            back = timedelta(days=7, hours=12) if r.template == "showcase_weekly_digest" else timedelta(hours=26)
            rows = (await db.execute(
                select(Property).join(collection_properties, collection_properties.c.property_id == Property.id)
                .join(Collection, Collection.id == collection_properties.c.collection_id)
                .where(Collection.owner_id == r.owner_id, func.lower(func.trim(Collection.visitor_email)) == r.recipient_email,
                       collection_properties.c.added_at.isnot(None), collection_properties.c.added_at > sent - back,
                       collection_properties.c.added_at <= sent)
                .order_by(collection_properties.c.added_at.desc()).limit(8))).scalars().all()
            seen, homes = set(), []
            for p in rows:
                if p.id in seen:
                    continue
                seen.add(p.id)
                homes.append({k: v for k, v in {
                    "street_address": nice_street(p.street_address or "", p.city), "city": (p.city or "").title(), "state": p.state,
                    "image": p.img_src, "price": f"${p.price:,.0f}" if p.price else None, "beds": p.bedrooms, "baths": p.bathrooms,
                    "sqft": p.living_area, "kind": "new"}.items() if v not in (None, "")})
            if homes:
                out[r.id] = homes
    except Exception:  # noqa: BLE001
        logger.warning("email contents: rebuild failed", exc_info=True)
    return out


def _clean(rows: list[dict], template: str) -> list[dict]:
    out = []
    for h in rows or []:
        if not isinstance(h, dict) or not (h.get("street_address") or h.get("address")):
            continue
        row = {k: v for k, v in h.items() if not str(k).startswith("_") and v not in (None, "")}
        from app.services.agent_roundup_service import nice_street
        row["street_address"] = nice_street(h.get("street_address") or h.get("address"))
        row["kind"] = _kind(h, template)
        out.append(row)
    return out


async def contents_for(db, recs: list[ClientEmail]) -> dict[str, dict]:
    """rec.id -> {homes, new_count, drop_count, home_count, summary, photo, source}."""
    out: dict[str, dict] = {}
    if not recs:
        return out
    try:
        from app.services.client_email_service import about_from
        copies = await _copies(db, recs)
        draft: dict[str, tuple[list[dict], str]] = {}
        for r in recs:
            if r.about:
                draft[r.id] = (_clean(list(r.about), r.template), "saved")
            elif r.id in copies:
                homes = _clean(about_from(copies[r.id] or {}), r.template)
                draft[r.id] = (homes, "copy") if homes else (_clean(_from_subject(r), r.template), "subject")
            else:
                draft[r.id] = (_clean(_from_subject(r), r.template), "subject")
        # Older Showcase updates with no homes kept: rebuild the new matches from their Showcases.
        bare = [r for r in recs if r.template in MATCH_TEMPLATES and not draft[r.id][0]][:REBUILD_MAX]
        for rid, homes in (await _rebuilt_matches(db, bare)).items():
            draft[rid] = (homes, "rebuilt")
        streets = {_street_key(h["street_address"]) for homes, _ in draft.values() for h in homes if not h.get("image")}
        photos = await _street_photos(db, {r.owner_id for r in recs}, {s for s in streets if s})
        for r in recs:
            homes, source = draft[r.id]
            for h in homes:
                if not h.get("image"):
                    img = photos.get(_street_key(h["street_address"]))
                    if img:
                        h["image"] = img
            new, drops, total = _counts(r, homes)
            out[r.id] = {"homes": homes, "new_count": new, "drop_count": drops, "home_count": total,
                         "summary": _summary(r, homes, new, drops, total), "photo": _photo(homes),
                         "source": source if homes else ("subject" if (new or drops) else None)}
    except Exception:  # noqa: BLE001 - a nice-to-have; never breaks the email list
        logger.warning("email contents failed", exc_info=True)
    return out
