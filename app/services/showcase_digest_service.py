"""Showcase daily digest - ONE email a day (4:30 PM Eastern) instead of one per MLS change.

The MLS sync finds new matches and price drops all day long. Emailing each one as it happened meant
a busy agent (60 Showcases in one city) sent their buyers - and themselves - hundreds of emails a
day. Now the sync only queues each change (a ScheduledEmail row with status QUEUED, which the
every-minute email sender ignores), and at DIGEST_HOUR:DIGEST_MINUTE this rolls them up:

- each buyer gets one "Today in your Showcase" email with every new home + price drop,
- each agent gets one email (and one bell alert) covering ALL their clients' Showcases.

Real engagement - a like, comment, tour request, refine-search request - is NOT part of this; those
still alert the agent right away.

Only changes queued before today's cutoff are rolled up, and rows are claimed with a single
UPDATE ... WHERE status = QUEUED, so a restart, a second worker or a second run the same evening
can never send a second digest for the same changes."""
from __future__ import annotations

from collections import OrderedDict
import json
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config.logging import get_logger
from app.models.database import Collection, Notification, ScheduledEmail, User
from app.services.tasks_service import EASTERN
from app.services.agent_roundup_service import nice_street
from app.services.agent_email_service import agent_public_phone
from app.services.email_avatar_service import agent_email_vars
from urllib.parse import quote

logger = get_logger(__name__)

DIGEST_HOUR, DIGEST_MINUTE = 16, 30  # Eastern
QUEUED = "DIGEST"        # waiting for today's digest (never picked up by process_due_emails)
ROLLED_UP = "DIGESTED"   # folded into a digest that went out
# Both templates live in Mailgun (mg.openhousepal.com, made 2026-09-14) and document their own
# variable contract in an HTML comment at the top: counts arrive as ready-made label strings
# ("3 new homes", "" = show nothing) and each home as a flat object (is_price_drop, street_address,
# city, state, image, beds, baths, sqft, price | old_price + new_price + savings).
BUYER_TEMPLATE = "showcase_daily_digest"
# The one-time "your Showcase is ready" email (queue_showcase_ready) - its own template.
READY_TEMPLATE = "showcase_ready"
READY_HOMES = 3
# Same idea, when the agent builds it by hand and sends it ("Email it to them").
SHARED_TEMPLATE = "showcase_shared"
AGENT_TEMPLATE = "showcase_daily_digest_agent"
# Emails stay short - a few items, then "+ N more" and a button to see everything (user, 10/06).
MAX_HOMES = 4               # homes in one buyer email (big photos on phones); the rest are in their Showcase
MAX_SHOWCASES = 4           # client Showcases in the agent's briefing (its button opens all of them)
MAX_HOMES_PER_SHOWCASE = 2  # homes under each Showcase in the briefing (older template version only)
# The briefing lists each HOME once with the clients it matched (user, 10/22: one open house's 30
# sign-ins share a search, so a by-Showcase list showed the same 4 houses over and over).
MAX_HOME_GROUPS = 6         # homes in the briefing
MAX_CLIENT_NAMES = 8        # client names (each opens that Showcase) under a home, then "+ N more"
# One agent's buyer emails go out this many seconds apart, not all at 4:30 on the dot - a burst of
# near-identical emails from one sender (especially their own Gmail / Outlook) looks like spam.
SPREAD_SECONDS = 20


def digest_cutoff(now: datetime | None = None) -> datetime:
    """Today's 4:30 PM Eastern, as UTC."""
    local = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    return datetime.combine(local.date(), time(DIGEST_HOUR, DIGEST_MINUTE), EASTERN).astimezone(timezone.utc)


def is_digest_time(now: datetime | None = None) -> bool:
    return (now or datetime.now(timezone.utc)) >= digest_cutoff(now)


def _cap(name: str) -> str:
    """Guests often type their name in lowercase ("aiden") - greet them properly."""
    return name[:1].upper() + name[1:] if name else ""


def _money(v: Any) -> str | None:
    try:
        return f"${float(v):,.0f}" if v else None
    except (TypeError, ValueError):
        return None


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _num(v: Any) -> str:
    """3.0 -> "3", 2.5 -> "2.5", 2850 -> "2,850"."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    return f"{int(f):,}" if f == int(f) else f"{f:g}"


def _place(v: dict) -> tuple[str, str, str]:
    """(street, city, state) - saved separately by the sync; older queued rows only have the one-line
    "12 Oak St, Ardmore, PA 19003"."""
    if v.get("_street"):
        return str(v["_street"]), str(v.get("_city") or ""), str(v.get("_state") or "")
    parts = [p.strip() for p in str(v.get("property_address") or "").split(",")]
    state = parts[2].split(" ")[0] if len(parts) > 2 and parts[2] else ""
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else ""), state


def _total_label(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return "1 home" if n == 1 else f"All {n} homes" if n > 1 else ""



async def _sc_path(col) -> str:
    """The short /s/<code> link for a Showcase (services/showcase_links.py)."""
    from app.services.showcase_links import showcase_path
    return await showcase_path(col) or f"/showcase/{col.share_token}"

async def _spotlight_told_today(db: AsyncSession, email: str, now: datetime) -> set:
    """Listing keys this guest got a Spotlight alert email about today (Eastern)."""
    from app.models.database import PropertyWebsite, SpotlightAlertSubscription
    day_start = datetime.combine(now.astimezone(EASTERN).date(), datetime.min.time(), EASTERN).astimezone(timezone.utc)
    rows = (await db.execute(
        select(PropertyWebsite.listing_key).join(SpotlightAlertSubscription, SpotlightAlertSubscription.property_website_id == PropertyWebsite.id)
        .where(func.lower(SpotlightAlertSubscription.email) == email.lower(), SpotlightAlertSubscription.last_notified_at >= day_start)
    )).scalars().all()
    return {str(k) for k in rows if k}


def _homes(rows: list[dict], keep_key: bool = False) -> list[dict]:
    """One entry per home (in the order they came in), shaped the way the Mailgun templates read it.
    A home that was new AND dropped its price the same day shows once, as new, at its latest price;
    a home that dropped twice shows the price before the first drop -> the latest one.
    keep_key: each home also carries "_key" (its listing key) - the agent briefing groups by it."""
    homes: "OrderedDict[str, dict]" = OrderedDict()
    for v in rows:
        key = str(v.get("_listing_key") or v.get("property_address"))
        kind = v.get("_kind") or "new"
        h = homes.get(key)
        if h is None:
            street, city, state = _place(v)
            from app.services.agent_roundup_service import nice_street
            h = homes[key] = {"street_address": nice_street(street, city), "city": city.title() if city.isupper() else city, "state": state,
                              "image": v.get("property_image") or "", "beds": _num(v.get("property_beds")),
                              "baths": _num(v.get("property_baths")), "sqft": _num(v.get("property_sqft")),
                              "is_price_drop": False, "_new": False}
            if keep_key:
                h["_key"] = key
        h["price"] = v.get("property_price") or h.get("price") or ""
        if kind == "new":
            h["_new"], h["is_price_drop"] = True, False
            for k in ("old_price", "new_price", "savings", "_first_old", "_last_new"):
                h.pop(k, None)
        elif not h["_new"]:
            h["is_price_drop"] = True
            if h.get("_first_old") is None:
                h["_first_old"] = v.get("_old_price")
            if v.get("_new_price"):
                h["_last_new"] = v["_new_price"]
                h["price"] = _money(v["_new_price"]) or h["price"]
            h["old_price"] = _money(h["_first_old"]) or ""
            h["new_price"] = _money(h.get("_last_new")) or h["price"]
            try:
                saved = float(h["_first_old"]) - float(h["_last_new"])
                h["savings"] = _money(saved) if saved > 0 else ""
            except (KeyError, TypeError, ValueError):
                h["savings"] = ""
    out = []
    for h in homes.values():
        for k in ("_first_old", "_last_new", "_new"):
            h.pop(k, None)
        out.append(h)
    return out


def _sample(homes: list[dict], n: int) -> list[dict]:
    """The first n homes, but never all new when there's a price drop too (the section's heading
    names both, so both should show)."""
    picked = homes[:n]
    if len(homes) > n and not any(h["is_price_drop"] for h in picked):
        drop = next((h for h in homes if h["is_price_drop"]), None)
        if drop:
            picked = picked[:-1] + [drop]
    return picked


async def _engagement(db: AsyncSession, collection_ids: list[str]) -> dict:
    """Showcase id -> how many homes the client liked, passed on or looked at in it."""
    if not collection_ids:
        return {}
    from sqlalchemy import or_
    from app.models.database import PropertyInteraction
    rows = await db.execute(
        select(PropertyInteraction.collection_id, func.count(PropertyInteraction.id))
        .where(PropertyInteraction.collection_id.in_(collection_ids),
               or_(PropertyInteraction.liked.is_(True), PropertyInteraction.disliked.is_(True), PropertyInteraction.view_count > 0))
        .group_by(PropertyInteraction.collection_id))
    return {cid: n for cid, n in rows.all()}


def briefing_showcases(entries: list, absolute, engagement: dict | None = None) -> dict | None:
    """The briefing's Showcase section from [(Showcase, its claimed rows)] - pure, no database.
    home_groups: each HOME once, with the clients it matched (names open their Showcase), the
    homes that reached the most clients first. Under each home the named clients are the agent's
    starred Showcases first (with a star), then the clients doing the most in their Showcase
    (`engagement`: Showcase id -> homes they liked / passed / looked at), then the most recently
    active (user, 10/22). showcases: the older by-Showcase list (the previous template version
    reads it). new / drops count homes, not Showcases."""
    engagement = engagement or {}
    showcases = []
    groups: "OrderedDict[tuple, dict]" = OrderedDict()   # (listing key, price drop?) -> the home + its clients
    for col, crow in entries:
        homes = _homes(crow, keep_key=True)
        if not homes:
            continue
        who = " ".join(_cap(w) for w in (col.visitor_name or "").split())
        link = absolute(f"/showcases?showcase={col.id}")
        starred = bool(getattr(col, "favorited_at", None))
        seen = getattr(col, "last_visitor_activity_at", None)
        rank = (starred, engagement.get(col.id, 0), seen.timestamp() if seen else 0)
        for h in homes:
            g = groups.setdefault((h["_key"], h["is_price_drop"]), {**{k: v for k, v in h.items() if k != "_key"}, "_clients": []})
            same = next((c for c in g["_clients"] if who and c["name"] == who), None)
            if same is None:
                g["_clients"].append({"name": who or "A client", "link": link, "starred": starred, "_rank": rank})
            elif rank > same["_rank"]:   # one person with two Showcases: named once, linking the one that ranks higher
                same.update(link=link, starred=starred, _rank=rank)
        homes = [{k: v for k, v in h.items() if k != "_key"} for h in homes]
        new, drops, new_label, drop_label = _labels(homes, "new", "new")
        showcases.append({"visitor_name": who, "collection_name": nice_street(col.name), "collection_link": link,
                          "new_label": new_label, "drop_label": drop_label,
                          "properties": _sample(homes, MAX_HOMES_PER_SHOWCASE), "_n": new + drops})
    if not showcases:
        return None
    showcases.sort(key=lambda x: -x["_n"])
    for x in showcases:
        x.pop("_n")
    home_groups = sorted(groups.values(), key=lambda g: -len(g["_clients"]))
    for g in home_groups:
        cl = sorted(g.pop("_clients"), key=lambda c: c["_rank"], reverse=True)
        for c in cl:
            c.pop("_rank")
        n = len(cl)
        g["clients"] = cl[:MAX_CLIENT_NAMES]
        g["clients_more"] = f"+ {n - MAX_CLIENT_NAMES} more" if n > MAX_CLIENT_NAMES else ""
        if g["is_price_drop"]:
            g["clients_label"] = f"In {n} clients' Showcases" if n > 1 else "In 1 client's Showcase"
        else:
            g["clients_label"] = f"Matched {_plural(n, 'client', 'clients')}"
    drops_n = sum(1 for g in home_groups if g["is_price_drop"])
    return {"showcases": showcases, "home_groups": home_groups, "new": len(home_groups) - drops_n, "drops": drops_n}


def _one_home_line(h: dict) -> str:
    street = h.get("street_address") or "A home"
    if h.get("is_price_drop"):
        return f"{street} dropped to {h.get('new_price') or h.get('price')}"
    return f"{street} just hit the market for {h.get('price')}" if h.get("price") else f"{street} just hit the market"


def _labels(homes: list[dict], new_one: str, new_many: str) -> tuple[int, int, str, str]:
    """(new, drops, "3 new homes" or "", "1 price drop" or "") - the templates show a label only when
    it isn't empty, so a "0" never appears."""
    drops = sum(1 for h in homes if h["is_price_drop"])
    new = len(homes) - drops
    return new, drops, (_plural(new, new_one, new_many) if new else ""), (_plural(drops, "price drop", "price drops") if drops else "")


async def send_showcase_digests(db: AsyncSession, now: datetime | None = None) -> dict:
    """Roll up every change queued before today's cutoff. Safe to call any time - before the cutoff
    on a given day nothing new is eligible after the first run, and claimed rows never go out twice."""
    cutoff = digest_cutoff(now)
    claimed = (await db.execute(
        update(ScheduledEmail)
        .where(ScheduledEmail.status == QUEUED, ScheduledEmail.created_at < cutoff,
               # the agent's own rows wait for THEIR briefing time (send_agent_briefings)
               func.coalesce(ScheduledEmail.template_variables["_audience"].astext, "buyer") != "agent")
        .values(status=ROLLED_UP, sent_at=datetime.now(timezone.utc))
        .returning(ScheduledEmail.recipient_email, ScheduledEmail.template_variables, ScheduledEmail.id)
    )).all()
    buyer_rows: dict[tuple[str, str], list[dict]] = {}
    buyer_ids: dict[tuple[str, str], list[str]] = {}
    agent_rows: dict[str, list[dict]] = {}
    # Oldest change first, so "latest price" really is the latest (RETURNING has no order).
    for email, v, row_id in sorted(claimed, key=lambda r: (r[1] or {}).get("_seq") or 0):
        v = dict(v or {})
        if v.get("_audience") == "agent":
            agent_rows.setdefault(str(v.get("_owner_id")), []).append(v)
        else:
            buyer_rows.setdefault((email.lower(), str(v.get("_collection_id"))), []).append(v)
            buyer_ids.setdefault((email.lower(), str(v.get("_collection_id"))), []).append(row_id)

    col_ids = {cid for _, cid in buyer_rows} | {str(v.get("_collection_id")) for rows in agent_rows.values() for v in rows}
    cols = {c.id: c for c in (await db.execute(
        select(Collection).options(selectinload(Collection.owner)).where(Collection.id.in_(col_ids))
    )).scalars().all()}

    from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS  # noqa: F811

    def live(col: Collection | None) -> bool:
        return bool(col and col.status == "ACTIVE" and col.owner and owner_has_access(col.owner, SHOWCASE_ACCESS_TIERS))

    # One client with several Showcases from the same agent gets ONE email (user, 10/22): the busiest
    # Showcase leads, the others' homes join it (a home in both shows once) and the email links to
    # each of their Showcases. One whose search the agent just changed keeps its own email.
    client_link = {k[1]: next((v.get("collection_link") for v in reversed(r) if v.get("collection_link")), "") for k, r in buyer_rows.items()}
    also: dict[tuple[str, str], list[Collection]] = {}
    by_person: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for key, r in buyer_rows.items():
        col = cols.get(key[1])
        if live(col) and getattr(col, "notify_visitor", True) and col.visitor_email and not any(v.get("_kind") == "search" for v in r):
            by_person.setdefault((key[0], col.owner_id), []).append(key)
    for keys in by_person.values():
        if len(keys) < 2:
            continue
        keys.sort(key=lambda k: -len(buyer_rows[k]))
        first, rest = keys[0], keys[1:]
        for k in rest:
            buyer_rows[first].extend(buyer_rows.pop(k))
            buyer_ids.setdefault(first, []).extend(buyer_ids.pop(k, []))
        buyer_rows[first].sort(key=lambda v: v.get("_seq") or 0)
        also[first] = [cols[k[1]] for k in rest]

    from app.services.agent_email_service import absolute, email_agent

    today = datetime.now(EASTERN).strftime("%m/%d/%Y")
    buyers = weekly_sent = held_weekly = held_off = 0
    from app.services.client_email_service import WEEKLY_GAP, WEEKLY_TEMPLATE, last_auto_sent, refresh_status
    hold_ids: list[str] = []          # weekly people whose week isn't up: their changes wait for next week's email
    per_owner: dict[str, int] = {}    # spread each agent's emails out (a burst of identical emails looks like spam)
    run_at = datetime.now(timezone.utc)
    for (email, cid), rows in buyer_rows.items():
        col = cols.get(cid)
        if not live(col) or not getattr(col, "notify_visitor", True) or not col.visitor_email:
            continue
        # Already told today by that home's Spotlight alert (10 AM) - don't send the same price drop
        # again at 4:30. A new match is still news (the Spotlight alert never says "new for you").
        # The agent changed their search today (queue_search_changed) - its own block, not a home.
        search_rows = [v for v in rows if v.get("_kind") == "search"]
        rows = [v for v in rows if v.get("_kind") != "search"]
        told = await _spotlight_told_today(db, email, now or datetime.now(timezone.utc))
        if told:
            rows = [v for v in rows if not (v.get("_kind") == "drop" and str(v.get("_listing_key") or "") in told)]
        homes = _homes(rows)
        if not homes and not search_rows:
            continue
        # The engagement rules (client_email_service): people who stopped opening these get one
        # email a week - or none - instead of one a day.
        try:
            st = await refresh_status(db, col.owner_id, email)
        except Exception:  # noqa: BLE001 - the rules failing must not stop the digest
            logger.warning("email rules check failed", exc_info=True)
            st = {"mode": "daily"}
        # The agent's own Studio -> Email setting can only slow these down or stop them.
        from app.services.email_settings_service import client_rule
        rule = client_rule(col.owner, BUYER_TEMPLATE)
        if rule is not None and not rule["on"]:
            held_off += 1
            continue
        if rule is not None and rule.get("mode") == "weekly" and st["mode"] == "daily":
            st = {**st, "mode": "weekly"}
        if st["mode"] == "off":
            held_off += 1
            continue
        weekly = st["mode"] == "weekly"
        if weekly:
            last_sent = await last_auto_sent(db, col.owner_id, email)
            if last_sent and run_at - last_sent < WEEKLY_GAP:
                hold_ids.extend(buyer_ids.get((email, cid), []))
                held_weekly += 1
                continue
        new, drops, new_label, drop_label = _labels(homes, "new match", "new matches")
        both = " and ".join(x for x in (new_label, drop_label) if x)
        # This Showcase's own latest row (merged rows from the client's other Showcases come too).
        last = ([v for v in rows if str(v.get("_collection_id")) == cid] or rows or search_rows)[-1]
        agent_first = (str(last.get("agent_name") or "").split(" ")[0]) or "Your agent"
        search = search_change_vars(search_rows, agent_first)
        vars_ = {
            "recipient_name": _cap((col.visitor_name or "").strip().split(" ")[0]) or "there",   # "Hi Casey," - first name
            "collection_name": nice_street(col.name), "collection_link": last.get("collection_link") or "",
            "total_count": last.get("total_count") or len(homes), "today_date": today,
            # The button's small line, ready-made so it never reads "All 1 homes".
            "total_label": _total_label(last.get("total_count") or len(homes)),
            "agent_name": last.get("agent_name") or "", "agent_email": last.get("agent_email") or "",
            "agent_phone": last.get("agent_phone") or "", "Unsub": last.get("Unsub") or "",
            "new_label": new_label, "drop_label": drop_label, "properties": homes[:MAX_HOMES],
            "more_label": _more(len(homes) - MAX_HOMES, "more update", "more updates", "in your Showcase"),
            # Same style as the agent's briefing headline - leads with what's new. One home = name
            # it, like the old new-listing alert ("12 Oak St just hit the market for $950,000").
            "headline": _one_home_line(homes[0]) if len(homes) == 1 else f"{both} in your Showcase",
            **(await agent_email_vars(db, col.owner)),   # face, title, brokerage, email, phone
        }
        if weekly:
            vars_["headline"] = (f"This week: {_one_home_line(homes[0])}" if len(homes) == 1 else f"This week: {both} in your Showcase")
            subject = (f"Your week: {_one_home_line(homes[0])}" if len(homes) == 1 else f"Your week: {both} in your Showcase")
        else:
            subject = (_one_home_line(homes[0]) if len(homes) == 1 else f"{both[0].upper() + both[1:]} in your Showcase today") if homes else ""
        others = also.get((email, cid), [])
        if others:
            # Their other Showcases with this agent - links under the button ("Your other Showcases: ...").
            vars_["other_showcases"] = [{"name": nice_street(o.name), "link": client_link.get(o.id) or ""} for o in others if client_link.get(o.id)]
            vars_["headline"] = vars_["headline"].replace("in your Showcase", "in your Showcases")
            vars_["more_label"] = vars_["more_label"].replace("in your Showcase", "in your Showcases")
            subject = subject.replace("in your Showcase", "in your Showcases")
        if search:
            added = search.pop("_added")
            vars_.update(search)
            fits = f" - {_plural(added, 'new home', 'new homes')} for you" if added else ""
            # Leads with the search change; the homes (if any) follow under it.
            vars_["headline"] = f"{agent_first} updated your search" + (f", plus {both}" if both else "")
            subject = f"{agent_first} updated your Showcase search{fits}" if not homes else f"Your search was updated + {both}"
        n = per_owner.get(col.owner_id, 0)
        per_owner[col.owner_id] = n + 1
        db.add(ScheduledEmail(
            recipient_email=col.visitor_email, template_name=WEEKLY_TEMPLATE if weekly else BUYER_TEMPLATE, template_variables=vars_,
            subject=subject, status="PENDING", scheduled_for=run_at + timedelta(seconds=SPREAD_SECONDS * n), owner_id=col.owner_id,
        ))
        buyers += 1
        weekly_sent += 1 if weekly else 0
    if hold_ids:
        await db.execute(update(ScheduledEmail).where(ScheduledEmail.id.in_(hold_ids)).values(status=QUEUED, sent_at=None))

    await db.commit()
    await send_agent_briefings(db, now)
    logger.info("Showcase digests", extra={"event": "showcase_digest", "buyers": buyers, "changes": len(claimed),
                                           "weekly": weekly_sent, "held_weekly": held_weekly, "held_off": held_off})
    return {"buyers": buyers, "changes": len(claimed), "weekly": weekly_sent, "held_weekly": held_weekly, "held_off": held_off}


_BRIEFED: set[tuple[str, str]] = set()   # (user id, day) this process already handled - skips the DB claim


async def send_agent_briefings(db: AsyncSession, now: datetime | None = None) -> dict:
    """Each agent's daily briefing, at the time THEY picked in Studio -> Email (4:30 PM by default):
    follow-ups, what's waiting on them, each open house that's over (its guests - 10/30, was its
    own email), and their clients' Showcase news since their last briefing.
    Safe to call every minute - a per-agent per-day claim in system_settings means one a day, even
    across restarts / several workers, and the Showcase rows are claimed per agent."""
    from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS
    from app.services.agent_email_service import absolute, email_agent
    from app.services.email_settings_service import briefing_hm
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(EASTERN)
    local_day = local.date().isoformat()
    today = local.strftime("%m/%d/%Y")

    def live(col: Collection | None) -> bool:
        return bool(col and col.status == "ACTIVE" and col.owner and owner_has_access(col.owner, SHOWCASE_ACCESS_TIERS))

    agent_emails: list[tuple[User, str, dict]] = []
    wrapup_emails: list[tuple[User, dict]] = []   # briefing email turned off: each wrap-up on its own
    agents = 0
    # Deploy day: the old one-for-everyone 4:30 run may already have gone out today.
    legacy = (await db.execute(text("SELECT value FROM system_settings WHERE key = 'daily_briefing_last_sent'"))).scalar()
    legacy_sent_today = legacy == local_day
    people = (await db.execute(select(User).where(User.email.isnot(None)))).scalars().all()
    for user in people:
        if (user.id, local_day) in _BRIEFED:
            continue
        h, m = briefing_hm(user)
        if (local.hour, local.minute) < (h, m) or not owner_has_access(user):
            continue
        _BRIEFED.add((user.id, local_day))
        claimed_day = (await db.execute(text(
            "INSERT INTO system_settings (key, value) VALUES (:k, to_jsonb(CAST(:d AS text))) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value WHERE system_settings.value <> EXCLUDED.value "
            "RETURNING key"), {"k": f"briefing_day:{user.id}", "d": local_day})).first() is not None
        if not claimed_day:
            continue
        if legacy_sent_today:
            await db.commit()
            continue
        # Open house wrap-ups (owner, 10/30): each open house that's over rides in this briefing
        # instead of its own email. One still going waits for tomorrow's.
        from app.services.agent_roundup_service import claim_open_house_wrapups
        from app.services.notification_preferences_service import channel_enabled
        try:
            wrapups = await claim_open_house_wrapups(db, user.id, now)
        except Exception:  # noqa: BLE001
            logger.warning("briefing open house wrap-ups failed", exc_info=True, extra={"user_id": user.id})
            wrapups = []
        if wrapups and not channel_enabled(user, "SHOWCASE_DIGEST", "email"):
            # They turned the briefing email off - turning one off must never lose the other.
            wrapup_emails += [(user, w) for w in wrapups]
            wrapups = []
        rows_claimed = (await db.execute(
            update(ScheduledEmail)
            .where(ScheduledEmail.status == QUEUED, ScheduledEmail.created_at < now,
                   ScheduledEmail.template_variables["_audience"].astext == "agent",
                   ScheduledEmail.template_variables["_owner_id"].astext == user.id)
            .values(status=ROLLED_UP, sent_at=datetime.now(timezone.utc))
            .returning(ScheduledEmail.template_variables)
        )).scalars().all()
        rows = sorted((dict(v or {}) for v in rows_claimed), key=lambda v: v.get("_seq") or 0)
        sc = None
        if rows:
            per_col: "OrderedDict[str, list[dict]]" = OrderedDict()
            for v in rows:
                per_col.setdefault(str(v.get("_collection_id")), []).append(v)
            cols = {c.id: c for c in (await db.execute(
                select(Collection).options(selectinload(Collection.owner)).where(Collection.id.in_(list(per_col)))
            )).scalars().all()}
            entries = [(cols.get(cid), crow) for cid, crow in per_col.items()
                       if live(cols.get(cid)) and getattr(cols.get(cid), "notify_agent", True)]
            sc = briefing_showcases(entries, absolute, await _engagement(db, [c.id for c, _ in entries]))
        try:
            brief = await _briefing(db, user, now, showcase_access=owner_has_access(user, SHOWCASE_ACCESS_TIERS))
        except Exception:  # noqa: BLE001 - one agent's bad data must not stop everyone's email
            logger.warning("daily briefing sections failed", exc_info=True, extra={"user_id": user.id})
            brief = {"followups": [], "actions": [], "client_activity": [], "followups_label": "", "followup_count": 0}
        if not sc and not wrapups and not brief["followups"] and not brief["actions"] and not brief["client_activity"]:
            await db.commit()
            continue  # nothing to say today - no email
        keys = brief.pop("_keys", [])
        seen_row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"), {"k": f"briefing_seen:{user.id}"})).scalar()
        if not sc and not wrapups and not (set(keys) - set(seen_row or [])):
            await db.commit()
            continue  # only things they were already told about yesterday - no email
        v = {"recipient_name": user.first_name or "there", "today_date": today,
             "dashboard_link": absolute("/dash"), **brief}
        if sc:
            # Counted per HOME, not per Showcase ("4 new homes", not "132 new matches" for 4 houses x 33 clients).
            new_part = _plural(sc["new"], "new home", "new homes") if sc["new"] else ""
            drop_part = _plural(sc["drops"], "price drop", "price drops") if sc["drops"] else ""
            clients = _plural(len(sc["showcases"]), "client Showcase", "client Showcases")
            both = " and ".join(p for p in (new_part, drop_part) if p)
            # names the price drops too ("3 new matches and 1 price drop across 2 client Showcases")
            headline = f"{both} across {clients}"
            v.update({"collection_label": clients, "total_new_label": new_part, "total_drop_label": drop_part,
                      # home_groups = the current template; collections = the older version's by-Showcase list.
                      "home_groups": sc["home_groups"][:MAX_HOME_GROUPS],
                      "home_groups_more": _more(len(sc["home_groups"]) - MAX_HOME_GROUPS, "more home", "more homes", "in your Showcases"),
                      "showcases_link": absolute("/showcases"),
                      "collections": sc["showcases"][:MAX_SHOWCASES], "has_showcases": True,
                      "collections_more": _more(len(sc["showcases"]) - MAX_SHOWCASES, "more Showcase updated", "more Showcases updated", "")})
        elif brief["followup_count"]:
            # "for today" only when none are overdue - otherwise it says they're waiting.
            headline = (_plural(brief["followup_count"], "follow-up waiting on you", "follow-ups waiting on you") if brief.get("followup_overdue")
                        else _plural(brief["followup_count"], "follow-up for today", "follow-ups for today"))
        else:
            n = len(brief["actions"]) + len(brief["client_activity"])
            headline = _plural(n, "thing waiting on you", "things waiting on you")
        if wrapups:
            guests = sum(w["guest_count"] for w in wrapups)
            # The title says it too, so it doesn't read as a normal briefing (owner, 10/30)
            v.update({"open_houses": wrapups,
                      "open_houses_title": "Your open house" if len(wrapups) == 1 else "Your open houses",
                      "open_houses_went": "how your open house went" if len(wrapups) == 1 else "how your open houses went"})
            # The freshest news leads: "5 guests at your open house at 732 Buck Ln"
            oh_part = (f"{_plural(guests, 'guest', 'guests')} at your open house at {wrapups[0]['property_address']}"
                       if len(wrapups) == 1 else f"{_plural(guests, 'guest', 'guests')} at {len(wrapups)} open houses")
            headline = oh_part if not (sc or brief["followup_count"]) else f"{oh_part}, plus {headline}"
        v["headline"] = headline
        v["has_showcase_section"] = bool(sc or brief["client_activity"])
        # The bell's "Today's Rundown" (user, 10/24) - one a day, whenever the briefing goes out (not
        # only on Showcase news). Clicking it opens the Rundown window in place (TodaysRundown.tsx);
        # the link is only the fallback (Dash opens the Rundown from ?rundown=1).
        db.add(Notification(
            agent_id=user.id, type="SHOWCASE_DIGEST", reference_type="SHOWCASE_DIGEST",
            reference_id=f"digest-{local_day}", title="Today's Rundown",
            message=headline[0].upper() + headline[1:] + ".", link="/dash?rundown=1", is_read=False,
            created_at=datetime.now(timezone.utc),
        ))
        title = f"Your daily briefing + {v['open_houses_went']}" if wrapups else "Your daily briefing"
        agent_emails.append((user, f"{title}: {headline}", v))
        await db.execute(text(
            "INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"k": f"briefing_seen:{user.id}", "v": json.dumps(keys)})
        agents += 1
        await db.commit()

    # Only after the claims are saved - a failed save must not leave emails out that a retry repeats.
    for user, subject, variables in agent_emails:
        email_agent(user, "SHOWCASE_DIGEST", AGENT_TEMPLATE, subject, variables)
    if wrapup_emails:
        from app.services.agent_roundup_service import send_wrapup_email
        for user, w in wrapup_emails:
            send_wrapup_email(user, w)
    if agents:
        logger.info("Daily briefings", extra={"event": "daily_briefing", "agents": agents})
    return {"agents": agents}


_SHOWCASE_KINDS = {"tour_request", "comment"}           # -> the Showcase section's "waiting on you"
_SKIP_KINDS = {"new_match", "liked", "disliked"}        # new matches are the Showcase section itself; likes have their hourly email
MAX_FOLLOWUPS, MAX_ACTIONS = 5, 6


def _more(n: int, one: str, many: str, tail: str) -> str:
    """"+ 3 more updates in your Showcase" - or "" when nothing was left out."""
    if n <= 0:
        return ""
    return f"+ {n} {one if n == 1 else many}" + (f" {tail}" if tail else "")


async def _briefing(db: AsyncSession, user: User, now: datetime, showcase_access: bool, full: bool = False) -> dict:
    """The non-Showcase parts of the daily briefing - the same things the app flags with a red
    badge, each in exactly one section (nothing repeats between sections).
    full: the in-app Today's Rundown (rundown_service) - nothing capped (the email shows a few)."""
    from app.models.database import AgentTask, FormSubmission, SupportTicket
    from app.services.agent_email_service import absolute
    from app.services.tasks_service import compute_bucket, task_day
    from app.utils.auth import tier_has_mls

    # Follow-ups: today's + EVERY overdue one (same as Dash and the Follow-ups page - the user wants
    # nothing old to quietly drop off). Order matches Dash: overdue this week, then today, then the
    # older backlog last, so a backlog never pushes today's off the 5 shown.
    from app.services.tasks_service import followups_enabled
    tasks = (await db.execute(select(AgentTask).where(
        AgentTask.owner_id == user.id, AgentTask.completed_at.is_(None), AgentTask.due_at.isnot(None),
        AgentTask.due_at <= now + timedelta(days=1),
    ))).scalars().all() if followups_enabled(user) else []
    due = [(t, compute_bucket(t, now)) for t in tasks]
    overdue = [t for t, b in due if b == "overdue"]
    today_ = [t for t, b in due if b == "today"]
    week_ago = now - timedelta(days=7)
    recent_over = sorted((t for t in overdue if t.due_at >= week_ago), key=lambda t: t.due_at)
    older_over = sorted((t for t in overdue if t.due_at < week_ago), key=lambda t: t.due_at, reverse=True)
    followups = []
    for t in recent_over + sorted(today_, key=lambda t: t.due_at) + older_over:
        is_over = t in overdue
        d = task_day(t)
        when = f"Overdue · was due {d.strftime('%a, %b')} {d.day}" if is_over and d else "Today"
        followups.append({"title": t.title, "when": when, "overdue": is_over, "_key": f"task:{t.id}", "_link": f"/leads/tasks?task={t.id}"})
    # Two open follow-ups with the same title (one person, two different reasons - each card in the
    # app shows its own reason) read as a duplicate in an email that only shows titles: one line each.
    merged: "OrderedDict[str, dict]" = OrderedDict()
    for f in followups:
        m = merged.get(f["title"])
        if m is None:
            merged[f["title"]] = {**f, "_n": 1, "_keys": [f["_key"]]}
        else:
            m["_n"] += 1
            m["_keys"].append(f["_key"])
    followups = []
    for m in merged.values():
        n, keys = m.pop("_n"), m.pop("_keys")
        followups.append({**m, "title": f"{m['title']} ({n})" if n > 1 else m["title"], "_key": keys[0], "_all_keys": keys})
    label = " · ".join(x for x in (f"{len(today_)} due today" if today_ else "", f"{len(overdue)} overdue" if overdue else "") if x)

    actions, activity = [], []
    # Open house hosting requests waiting on this agent (the Opens menu's red badge).
    if tier_has_mls(user.plan_tier):
        try:
            from app.services.open_house_manager_service import get_action_counts
            c = await get_action_counts(db, user)
            if c.pending_as_listing_agent:
                actions.append({"badge": str(c.pending_as_listing_agent), "title": _plural(c.pending_as_listing_agent, "agent wants", "agents want") + " to host your listing",
                                "detail": "Approve, decline or suggest another time.", "link": absolute("/open-houses/my-listings")})
            waiting = c.countered_as_host + c.pending_incoming_broadcasts
            if waiting:
                actions.append({"badge": str(waiting), "title": _plural(waiting, "hosting opportunity needs", "hosting opportunities need") + " your answer",
                                "detail": "A new time was proposed, or your team needs a host.", "link": absolute("/discovery")})
        except Exception:  # noqa: BLE001
            logger.warning("briefing host counts failed", exc_info=True)
    # Leads that need a response (the Dashboard's "needs attention" list).
    try:
        from app.services.ai.ai_insights_service import get_daily_digest
        # A person who already has a follow-up in this email isn't listed again here ("Follow up
        # with Sam" + "Sam has new activity" is the same thing twice).
        followup_leads = {t.lead_id for t in overdue + today_ if t.lead_id}
        for item in (await get_daily_digest(db, user))["needs_attention"]:
            kind = item.get("kind")
            if kind in _SKIP_KINDS:
                continue
            m = re.match(r"lead-([0-9a-f-]{36})-", str(item.get("id") or ""))
            if m and m.group(1) in followup_leads:
                continue
            row = {"badge": "!", "title": item["title"], "detail": item.get("detail") or "", "link": absolute(item.get("link") or "/leads")}
            if kind in _SHOWCASE_KINDS:
                if showcase_access:
                    activity.append(row)
            else:
                actions.append(row)
    except Exception:  # noqa: BLE001
        logger.warning("briefing attention items failed", exc_info=True)
    # Form responses not opened yet (the Tools menu's red badge).
    unread_forms = (await db.execute(select(func.count()).select_from(FormSubmission).where(
        FormSubmission.owner_id == user.id, FormSubmission.is_reviewed.is_(False)))).scalar() or 0
    if unread_forms:
        actions.append({"badge": str(unread_forms), "title": _plural(unread_forms, "form response", "form responses") + " you haven't opened",
                        "detail": "", "link": absolute("/studio/forms")})
    # A support reply they haven't read (the Help icon's red dot).
    try:
        from app.api.support_routes import _is_unread_for_agent
        from sqlalchemy.orm import selectinload as _sl
        tickets = (await db.execute(select(SupportTicket).options(_sl(SupportTicket.messages))
                                    .where(SupportTicket.agent_id == user.id))).scalars().all()
        if any(_is_unread_for_agent(t) for t in tickets):
            actions.append({"badge": "1", "title": "Support replied to you", "detail": "", "link": absolute("/settings/help")})
    except Exception:  # noqa: BLE001
        logger.warning("briefing support check failed", exc_info=True)
    # Showcase clients asking to change their search (the Showcases menu's red badge).
    if showcase_access:
        from app.services.collections_service import CollectionsService
        refine = await CollectionsService.count_pending_refine_requests(db, str(user.id))
        if refine:
            activity.append({"badge": str(refine), "title": _plural(refine, "client wants", "clients want") + " their search changed",
                             "detail": "", "link": absolute("/showcases")})
        # Clients who changed their own search since yesterday's briefing - who, and what it is now.
        try:
            from app.models.database import Collection, Lead, LeadEvent
            evs = (await db.execute(
                select(LeadEvent, Collection).join(Lead, Lead.id == LeadEvent.lead_id)
                .join(Collection, Collection.id == LeadEvent.collection_id)
                .where(Lead.owner_id == user.id, LeadEvent.source_type == "showcase_search_changed",
                       LeadEvent.occurred_at >= now - timedelta(hours=24))
                .order_by(LeadEvent.occurred_at.desc())
            )).all()
            seen: set = set()
            for ev, col in evs:
                if col.id in seen:
                    continue  # newest change per Showcase
                seen.add(col.id)
                answers = (ev.raw_snapshot or {}).get("answers") or []
                detail = " · ".join(f"{a.get('label')}: {a.get('value')}" for a in answers if isinstance(a, dict))[:200]
                activity.append({"badge": "↻", "title": f"{col.visitor_name or 'A client'} changed their own search",
                                 "detail": detail, "link": absolute(f"/showcases?showcase={col.id}")})
        except Exception:  # noqa: BLE001
            logger.warning("briefing search changes failed", exc_info=True)

    if full:
        return {"followups": followups, "followup_count": len(overdue) + len(today_), "followup_overdue": len(overdue),
                "followups_label": label, "actions": actions, "client_activity": activity}
    more = len(actions) - MAX_ACTIONS
    return {
        "followups": followups[:MAX_FOLLOWUPS],
        "followups_more": f"+ {len(followups) - MAX_FOLLOWUPS} more" if len(followups) > MAX_FOLLOWUPS else "",
        "followups_label": label, "followup_count": len(overdue) + len(today_), "followup_overdue": len(overdue), "followups_link": absolute("/leads/tasks"),
        "actions": actions[:MAX_ACTIONS], "actions_more": f"+ {more} more on your Dashboard" if more > 0 else "",
        "client_activity": activity[:MAX_ACTIONS],
        # Everything this briefing is about - compared with the last one sent, so a day with
        # nothing new (the same old overdue follow-ups, the same unopened forms) sends no email.
        "_keys": sorted({k for f in followups for k in f.get("_all_keys", [f["_key"]])}
                        | {f"action:{a['title']}|{a.get('detail', '')}" for a in actions}
                        | {f"activity:{a['title']}|{a.get('detail', '')}" for a in activity}),
    }


async def queue_showcase_ready(db: AsyncSession, collection_id: str, from_agent: bool = False) -> bool:
    """The buyer's own Showcase link, emailed the moment their Showcase is first built (a form, the
    IDX "Create my free Showcase", the Guest Pass) - so closing the thank-you page doesn't lose it
    until the next 4:30 digest. Its own Mailgun template (showcase_ready): their link, what they can
    do on any home, and the first three homes it picked. Never raises.

    from_agent: the agent built it for them by hand ("Email it to them" on) - its own template
    (showcase_shared): "{Agent} picked homes for you", what the search looks for and how to change it."""
    import os
    from app.models.database import Property, collection_properties
    from app.services.blacklist_service import BlacklistService
    from app.services.website.site_service import resolve_owner_public_base_url
    from app.services.agent_roundup_service import nice_street
    try:
        col = (await db.execute(select(Collection).options(selectinload(Collection.preferences), selectinload(Collection.original_open_house_event))
                                .where(Collection.id == collection_id))).scalar_one_or_none()
        if col is None or col.status != "ACTIVE" or not col.visitor_email or not getattr(col, "notify_visitor", True):
            return False
        if await BlacklistService.is_blacklisted(db, col.visitor_email):
            return False
        owner = await db.get(User, col.owner_id)
        total = (await db.execute(
            select(func.count()).select_from(collection_properties).where(collection_properties.c.collection_id == col.id)
        )).scalar() or 0
        props = (await db.execute(
            select(Property).join(collection_properties, collection_properties.c.property_id == Property.id)
            .where(collection_properties.c.collection_id == col.id)
            # homes with a photo first - an email card with an empty photo box looks broken
            .order_by(Property.img_src.is_(None), Property.img_src == "", Property.mls_list_date.desc().nullslast())
            .limit(READY_HOMES)
        )).scalars().all()
        homes = [{
            "street_address": nice_street(p.street_address or "", p.city), "city": (p.city or "").title(), "state": p.state or "",
            "image": p.img_src or "", "beds": _num(p.bedrooms), "baths": _num(p.bathrooms), "sqft": _num(p.living_area),
            "price": f"${p.price:,.0f}" if p.price else "",
        } for p in props]
        frontend_url = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
        base = (await resolve_owner_public_base_url(db, owner)) if owner else frontend_url
        first = (col.visitor_name or "").split(" ")[0].strip().title()
        vars_ = {
            "first_name": _cap((col.visitor_name or "").split(" ")[0].strip()) or "there",
            "collection_link": f"{base}{await _sc_path(col)}",
            "total_count": total, "has_homes": bool(total), "homes": homes,
            "today_date": datetime.now(EASTERN).strftime("%m/%d/%Y"),
            "agent_name": (f"{owner.first_name or ''} {owner.last_name or ''}".strip() if owner else "") or "Your agent",
            "agent_email": owner.email if owner else "", "agent_phone": await agent_public_phone(db, owner),
            "Unsub": f"{frontend_url}/unsubscribe?email={quote(col.visitor_email or '', safe='')}",
            **(await agent_email_vars(db, owner)),   # face, title, brokerage, email, phone
        }
        template = READY_TEMPLATE
        subject = f"{first + ', your' if first else 'Your'} Showcase is ready" + (f" - {total} home{'s' if total != 1 else ''} picked for you" if total else "")
        if from_agent:
            from app.services.collections_service import _resolve_original_property_data
            agent_first = (owner.first_name or "").strip() if owner else ""
            agent_first = agent_first or vars_["agent_name"].split(" ")[0]
            built = None
            if col.preferences and col.preferences.show_match_score is not False:
                built = ((await _resolve_original_property_data(db, col)) or {}).get("address")
            vars_.update({
                "agent_first": agent_first,
                "homes_label": _plural(total, "home", "homes"),
                "search_summary": search_summary(col.preferences, built) if col.preferences else "",
                "search_link": f"{vars_['collection_link']}?search=1",
            })
            template = SHARED_TEMPLATE
            subject = "Some homes I picked out for you" if total else "Your home search is set up"
        db.add(ScheduledEmail(
            recipient_email=col.visitor_email, template_name=template, template_variables=vars_,
            subject=subject,
            status="PENDING", scheduled_for=datetime.now(timezone.utc), owner_id=col.owner_id,
        ))
        await db.commit()
        return True
    except Exception:  # noqa: BLE001 - the Showcase itself is already built
        logger.warning("showcase ready email failed", exc_info=True)
        await db.rollback()
        return False


def _k(v) -> str:
    """400000 -> "$400k", 1250000 -> "$1.25M"."""
    v = float(v)
    return f"${v / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M" if v >= 1_000_000 else f"${round(v / 1000):,}k"


def search_summary(prefs, built_around: str | None = None) -> str:
    """What a Showcase search is now, in one plain line for the buyer's email:
    "Built around 12 Oak St · Within 6 mi · $400k-$600k · 3+ beds"."""
    parts: list[str] = []
    if built_around:
        parts.append(f"Built around {nice_street(built_around)}")
    if getattr(prefs, "search_polygon", None):
        parts.append("The area your agent drew on the map")
    elif prefs.address and prefs.diameter:
        parts.append(f"Within {prefs.diameter:g} mi" + ("" if built_around else f" of {nice_street(prefs.address)}"))
    towns = [t for t in list(prefs.cities or []) + list(prefs.townships or []) if t]
    if towns:
        parts.append(", ".join(towns[:3]) + (f" +{len(towns) - 3} more" if len(towns) > 3 else ""))
    if prefs.school_districts:
        parts.append(", ".join(prefs.school_districts[:2]) + (" school district" if len(prefs.school_districts) == 1 else " school districts"))
    if prefs.min_price and prefs.max_price:
        parts.append(f"{_k(prefs.min_price)}-{_k(prefs.max_price)}")
    elif prefs.max_price:
        parts.append(f"Up to {_k(prefs.max_price)}")
    elif prefs.min_price:
        parts.append(f"{_k(prefs.min_price)}+")
    if prefs.min_beds:
        parts.append(f"{prefs.min_beds:g}+ beds")
    if prefs.min_baths:
        parts.append(f"{prefs.min_baths:g}+ baths")
    return " · ".join(parts)


async def showcase_home_ids(db: AsyncSession, collection_id: str) -> set:
    from app.models.database import collection_properties
    return set((await db.execute(select(collection_properties.c.property_id)
                                 .where(collection_properties.c.collection_id == collection_id))).scalars().all())


async def queue_search_changed(db: AsyncSession, collection_id: str, before_ids: set) -> bool:
    """The agent changed a client's Showcase search: queue it for that client's 4:30 email - what
    the search is now, how many homes it adds / drops, and a button to change it themselves.
    Several changes in one day fold into one note (first "before" vs last "after"). The client's
    own edits aren't queued - they just made them. Never raises."""
    import os
    from app.services.blacklist_service import BlacklistService
    from app.services.collections_service import _resolve_original_property_data
    from app.services.website.site_service import resolve_owner_public_base_url
    try:
        col = (await db.execute(select(Collection).options(selectinload(Collection.preferences), selectinload(Collection.owner),
                                                           selectinload(Collection.original_open_house_event))
                                .where(Collection.id == collection_id))).scalar_one_or_none()
        if col is None or col.status != "ACTIVE" or not col.visitor_email or not getattr(col, "notify_visitor", True) or not col.preferences:
            return False
        if await BlacklistService.is_blacklisted(db, col.visitor_email):
            return False
        after = await showcase_home_ids(db, col.id)
        built = None
        if col.preferences.show_match_score is not False:
            built = ((await _resolve_original_property_data(db, col)) or {}).get("address")
        owner = col.owner
        frontend_url = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
        base = (await resolve_owner_public_base_url(db, owner)) if owner else frontend_url
        db.add(ScheduledEmail(
            recipient_email=col.visitor_email, template_name=BUYER_TEMPLATE, subject="Your search was updated", status=QUEUED,
            scheduled_for=datetime.now(timezone.utc), owner_id=col.owner_id,
            template_variables={
                "collection_link": f"{base}{await _sc_path(col)}", "total_count": len(after),
                "agent_name": (f"{owner.first_name or ''} {owner.last_name or ''}".strip() if owner else "") or "Your agent",
                "agent_email": owner.email if owner else "", "agent_phone": await agent_public_phone(db, owner),
                "Unsub": f"{frontend_url}/unsubscribe?email={quote(col.visitor_email, safe='')}",
                "_audience": "buyer", "_collection_id": col.id, "_owner_id": col.owner_id, "_kind": "search", "_seq": __import__("time").time_ns(),
                "_before_ids": sorted(before_ids), "_after_ids": sorted(after),
                "_summary": search_summary(col.preferences, built),
            }))
        await db.commit()
        return True
    except Exception:  # noqa: BLE001 - the search itself is already saved
        logger.warning("queueing the search-changed note failed", exc_info=True)
        await db.rollback()
        return False


def search_change_vars(search_rows: list[dict], agent_first: str) -> dict:
    """Template variables for the "Your search was updated" block of the buyer's email."""
    if not search_rows:
        return {}
    before, after = set(search_rows[0].get("_before_ids") or []), set(search_rows[-1].get("_after_ids") or [])
    added, removed = len(after - before), len(before - after)
    counts = [
        f"{_plural(added, 'new home fits', 'new homes fit')} it now" if added else "",
        f"{removed} no longer {'fits' if removed == 1 else 'fit'}" if removed else "",
        f"{_plural(len(after), 'home', 'homes')} in all",
    ]
    link = str(search_rows[-1].get("collection_link") or "")
    return {
        "search_changed": True,
        "search_summary": search_rows[-1].get("_summary") or "",
        "search_counts": " · ".join(c for c in counts if c),
        "search_link": f"{link}{'&' if '?' in link else '?'}search=1" if link else "",
        "_added": added,
    }
