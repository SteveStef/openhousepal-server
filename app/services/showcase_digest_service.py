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
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config.logging import get_logger
from app.models.database import Collection, Notification, ScheduledEmail, User
from app.services.tasks_service import EASTERN
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
AGENT_TEMPLATE = "showcase_daily_digest_agent"
# Emails stay short - a few items, then "+ N more" and a button to see everything (user, 10/06).
MAX_HOMES = 4               # homes in one buyer email (big photos on phones); the rest are in their Showcase
MAX_SHOWCASES = 4           # client Showcases in the agent's briefing (its button opens all of them)
MAX_HOMES_PER_SHOWCASE = 2  # homes under each Showcase in the briefing
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


def _homes(rows: list[dict]) -> list[dict]:
    """One entry per home (in the order they came in), shaped the way the Mailgun templates read it.
    A home that was new AND dropped its price the same day shows once, as new, at its latest price;
    a home that dropped twice shows the price before the first drop -> the latest one."""
    homes: "OrderedDict[str, dict]" = OrderedDict()
    for v in rows:
        key = str(v.get("_listing_key") or v.get("property_address"))
        kind = v.get("_kind") or "new"
        h = homes.get(key)
        if h is None:
            street, city, state = _place(v)
            from app.services.agent_roundup_service import nice_street
            h = homes[key] = {"street_address": nice_street(street), "city": city.title() if city.isupper() else city, "state": state,
                              "image": v.get("property_image") or "", "beds": _num(v.get("property_beds")),
                              "baths": _num(v.get("property_baths")), "sqft": _num(v.get("property_sqft")),
                              "is_price_drop": False, "_new": False}
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
        .where(ScheduledEmail.status == QUEUED, ScheduledEmail.created_at < cutoff)
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
        homes = _homes(rows)
        if not homes:
            continue
        # The engagement rules (client_email_service): people who stopped opening these get one
        # email a week - or none - instead of one a day.
        try:
            st = await refresh_status(db, col.owner_id, email)
        except Exception:  # noqa: BLE001 - the rules failing must not stop the digest
            logger.warning("email rules check failed", exc_info=True)
            st = {"mode": "daily"}
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
        last = rows[-1]
        vars_ = {
            "recipient_name": _cap((col.visitor_name or "").strip().split(" ")[0]) or "there",   # "Hi Casey," - first name
            "collection_name": col.name, "collection_link": last.get("collection_link") or "",
            "total_count": last.get("total_count") or len(homes), "today_date": today,
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
            subject = (_one_home_line(homes[0]) if len(homes) == 1 else f"{both[0].upper() + both[1:]} in your Showcase today")
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

    # ---- the agent's daily briefing: follow-ups, what's waiting on them, their clients' Showcases.
    # Sent once a day to every agent with a live plan, skipped when all three are empty. The
    # system_settings claim below makes it once a day even across restarts / several workers (the
    # Showcase part has its own per-row claim above).
    showcase_by_owner: dict[str, dict] = {}
    for owner_id, rows in agent_rows.items():
        per_col: "OrderedDict[str, list[dict]]" = OrderedDict()
        for v in rows:
            per_col.setdefault(str(v.get("_collection_id")), []).append(v)
        showcases = []
        for cid, crow in per_col.items():
            col = cols.get(cid)
            if not live(col) or not getattr(col, "notify_agent", True):
                continue
            homes = _homes(crow)
            if homes:
                new, drops, new_label, drop_label = _labels(homes, "new", "new")
                showcases.append({"visitor_name": " ".join(_cap(w) for w in (col.visitor_name or "").split()),
                                  "collection_name": col.name, "collection_link": absolute(f"/showcases?showcase={col.id}"),
                                  "new_label": new_label, "drop_label": drop_label,
                                  "properties": _sample(homes, MAX_HOMES_PER_SHOWCASE), "_new": new, "_drops": drops})
        if showcases:
            showcases.sort(key=lambda s: -(s["_new"] + s["_drops"]))
            total_new = sum(s.pop("_new") for s in showcases)
            total_drops = sum(s.pop("_drops") for s in showcases)
            showcase_by_owner[owner_id] = {"showcases": showcases, "new": total_new, "drops": total_drops}

    agent_emails: list[tuple[User, str, dict]] = []
    agents = 0
    local_day = (now or datetime.now(timezone.utc)).astimezone(EASTERN).date().isoformat()
    first_run_today = is_digest_time(now) and (await db.execute(text(
        "INSERT INTO system_settings (key, value) VALUES ('daily_briefing_last_sent', to_jsonb(CAST(:d AS text))) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value WHERE system_settings.value <> EXCLUDED.value "
        "RETURNING key"), {"d": local_day})).first() is not None
    if first_run_today:
        people = (await db.execute(select(User).where(User.email.isnot(None)))).scalars().all()
        for user in people:
            if not owner_has_access(user):
                continue
            sc = showcase_by_owner.get(user.id)
            try:
                brief = await _briefing(db, user, now or datetime.now(timezone.utc), showcase_access=owner_has_access(user, SHOWCASE_ACCESS_TIERS))
            except Exception:  # noqa: BLE001 - one agent's bad data must not stop everyone's email
                logger.warning("daily briefing sections failed", exc_info=True, extra={"user_id": user.id})
                brief = {"followups": [], "actions": [], "client_activity": [], "followups_label": "", "followup_count": 0}
            if not sc and not brief["followups"] and not brief["actions"] and not brief["client_activity"]:
                continue  # nothing to say today - no email
            v = {"recipient_name": user.first_name or "there", "today_date": today,
                 "dashboard_link": absolute("/dash"), **brief}
            if sc:
                new_part = _plural(sc["new"], "new match", "new matches") if sc["new"] else ""
                drop_part = _plural(sc["drops"], "price drop", "price drops") if sc["drops"] else ""
                clients = _plural(len(sc["showcases"]), "client showcase", "client showcases")
                headline = f"{new_part or drop_part} across {clients}"
                both = " and ".join(p for p in (new_part, drop_part) if p)
                bell = (f"{both} across {len(sc['showcases'])} clients' Showcases" if len(sc["showcases"]) > 1
                        else f"{both} in {sc['showcases'][0]['visitor_name'] or 'a client'}'s Showcase")
                db.add(Notification(
                    agent_id=user.id, type="SHOWCASE_DIGEST", reference_type="SHOWCASE_DIGEST",
                    reference_id=f"digest-{local_day}", title="Today's Showcase update",
                    message=bell[0].upper() + bell[1:] + ".", link="/showcases", is_read=False,
                    created_at=datetime.now(timezone.utc),
                ))
                v.update({"collection_label": clients, "total_new_label": new_part, "total_drop_label": drop_part,
                          "collections": sc["showcases"][:MAX_SHOWCASES], "has_showcases": True,
                          "collections_more": _more(len(sc["showcases"]) - MAX_SHOWCASES, "more Showcase updated", "more Showcases updated", "")})
            elif brief["followup_count"]:
                headline = _plural(brief["followup_count"], "follow-up for today", "follow-ups for today")
            else:
                n = len(brief["actions"]) + len(brief["client_activity"])
                headline = _plural(n, "thing waiting on you", "things waiting on you")
            v["headline"] = headline
            v["has_showcase_section"] = bool(sc or brief["client_activity"])
            agent_emails.append((user, f"Your daily briefing: {headline}", v))
            agents += 1

    await db.commit()
    # Only after the claims are saved - a failed save must not leave emails out that a retry repeats.
    for user, subject, variables in agent_emails:
        email_agent(user, "SHOWCASE_DIGEST", AGENT_TEMPLATE, subject, variables)
    logger.info("Showcase digests", extra={"event": "showcase_digest", "buyers": buyers, "agents": agents, "changes": len(claimed),
                                           "weekly": weekly_sent, "held_weekly": held_weekly, "held_off": held_off})
    return {"buyers": buyers, "agents": agents, "changes": len(claimed), "weekly": weekly_sent, "held_weekly": held_weekly, "held_off": held_off}


_SHOWCASE_KINDS = {"tour_request", "comment"}           # -> the Showcase section's "waiting on you"
_SKIP_KINDS = {"new_match", "liked", "disliked"}        # new matches are the Showcase section itself; likes have their hourly email
MAX_FOLLOWUPS, MAX_ACTIONS = 5, 6


def _more(n: int, one: str, many: str, tail: str) -> str:
    """"+ 3 more updates in your Showcase" - or "" when nothing was left out."""
    if n <= 0:
        return ""
    return f"+ {n} {one if n == 1 else many}" + (f" {tail}" if tail else "")


async def _briefing(db: AsyncSession, user: User, now: datetime, showcase_access: bool) -> dict:
    """The non-Showcase parts of the daily briefing - the same things the app flags with a red
    badge, each in exactly one section (nothing repeats between sections)."""
    from app.models.database import AgentTask, FormSubmission, SupportTicket
    from app.services.agent_email_service import absolute
    from app.services.tasks_service import compute_bucket, task_day
    from app.utils.auth import tier_has_mls

    # Follow-ups: today's + overdue (overdue = the same 7-day window the calendar carries forward).
    from app.services.tasks_service import followups_enabled
    tasks = (await db.execute(select(AgentTask).where(
        AgentTask.owner_id == user.id, AgentTask.completed_at.is_(None), AgentTask.due_at.isnot(None),
        AgentTask.due_at >= now - timedelta(days=8), AgentTask.due_at <= now + timedelta(days=1),
    ))).scalars().all() if followups_enabled(user) else []
    due = [(t, compute_bucket(t, now)) for t in tasks]
    overdue = [t for t, b in due if b == "overdue"]
    today_ = [t for t, b in due if b == "today"]
    followups = []
    for t in sorted(overdue, key=lambda t: t.due_at) + sorted(today_, key=lambda t: t.due_at):
        is_over = t in overdue
        d = task_day(t)
        when = f"Overdue · was due {d.strftime('%a, %b')} {d.day}" if is_over and d else "Today"
        followups.append({"title": t.title, "when": when, "overdue": is_over})
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
        for item in (await get_daily_digest(db, user))["needs_attention"]:
            kind = item.get("kind")
            if kind in _SKIP_KINDS:
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

    more = len(actions) - MAX_ACTIONS
    return {
        "followups": followups[:MAX_FOLLOWUPS],
        "followups_more": f"+ {len(followups) - MAX_FOLLOWUPS} more" if len(followups) > MAX_FOLLOWUPS else "",
        "followups_label": label, "followup_count": len(followups), "followups_link": absolute("/leads/tasks"),
        "actions": actions[:MAX_ACTIONS], "actions_more": f"+ {more} more on your Dashboard" if more > 0 else "",
        "client_activity": activity[:MAX_ACTIONS],
    }


async def queue_showcase_ready(db: AsyncSession, collection_id: str) -> bool:
    """The buyer's own Showcase link, emailed the moment their Showcase is first built (a form, the
    IDX "Create my free Showcase", the Guest Pass) - so closing the thank-you page doesn't lose it
    until the next 4:30 digest. Its own Mailgun template (showcase_ready): their link, what they can
    do on any home, and the first three homes it picked. Never raises."""
    import os
    from app.models.database import Property, collection_properties
    from app.services.blacklist_service import BlacklistService
    from app.services.website.site_service import resolve_owner_public_base_url
    from app.services.agent_roundup_service import nice_street
    try:
        col = await db.get(Collection, collection_id)
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
            .where(collection_properties.c.collection_id == col.id).limit(READY_HOMES)
        )).scalars().all()
        homes = [{
            "street_address": nice_street(p.street_address or ""), "city": (p.city or "").title(), "state": p.state or "",
            "image": p.img_src or "", "beds": _num(p.bedrooms), "baths": _num(p.bathrooms), "sqft": _num(p.living_area),
            "price": f"${p.price:,.0f}" if p.price else "",
        } for p in props]
        frontend_url = os.getenv("FRONTEND_URL", os.getenv("CLIENT_URL", "http://localhost:3000")).rstrip("/")
        base = (await resolve_owner_public_base_url(db, owner)) if owner else frontend_url
        first = (col.visitor_name or "").split(" ")[0].strip().title()
        vars_ = {
            "first_name": _cap((col.visitor_name or "").split(" ")[0].strip()) or "there",
            "collection_link": f"{base}/showcase/{col.share_token}",
            "total_count": total, "has_homes": bool(total), "homes": homes,
            "today_date": datetime.now(EASTERN).strftime("%m/%d/%Y"),
            "agent_name": (f"{owner.first_name or ''} {owner.last_name or ''}".strip() if owner else "") or "Your agent",
            "agent_email": owner.email if owner else "", "agent_phone": await agent_public_phone(db, owner),
            "Unsub": f"{frontend_url}/unsubscribe?email={quote(col.visitor_email or '', safe='')}",
            **(await agent_email_vars(db, owner)),   # face, title, brokerage, email, phone
        }
        db.add(ScheduledEmail(
            recipient_email=col.visitor_email, template_name=READY_TEMPLATE, template_variables=vars_,
            subject=f"{first + ', your' if first else 'Your'} Showcase is ready" + (f" - {total} home{'s' if total != 1 else ''} picked for you" if total else ""),
            status="PENDING", scheduled_for=datetime.now(timezone.utc), owner_id=col.owner_id,
        ))
        await db.commit()
        return True
    except Exception:  # noqa: BLE001 - the Showcase itself is already built
        logger.warning("showcase ready email failed", exc_info=True)
        await db.rollback()
        return False
