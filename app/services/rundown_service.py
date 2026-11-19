"""Today's Rundown - the agent's daily briefing, in the app (user, 10/24).

One source of truth with the daily briefing email (showcase_digest_service.send_agent_briefings):
the same "things to do" (_briefing) and the same Showcase news (the agent's queued digest rows,
grouped per home by briefing_showcases) - just nothing capped, plus everything that happened today
(the day's notifications, grouped) and the day's numbers. Before the briefing has gone out today it
is "so far today"; after, it is what the briefing said plus anything since.

When the agent hasn't opened it in the app for a while, it also carries "Since you last looked"
(build_catchup, owner 11/18): the days between their last look and today, up to a week back.

Read only - building the Rundown never claims digest rows or sends anything."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config.logging import get_logger
from app.models.database import Collection, Notification, ScheduledEmail, User
from app.services.tasks_service import EASTERN

logger = get_logger(__name__)

# Notification type -> the Rundown section it belongs in ("what happened today").
_GROUPS: list[tuple[str, str, set[str]]] = [
    ("leads", "Leads & sign-ins", {"OPEN_HOUSE_SIGN_IN", "PORTAL_LEAD", "LEAD", "PROPERTY_INQUIRY", "OPEN_HOUSE_SECOND_VISIT",
                                   "OPEN_HOUSE_DISCLOSURE", "OPEN_HOUSE_FORM_REQUEST", "LEAD_INTEREST"}),
    ("showcases", "Showcases", {"PROPERTY_INTERACTION", "PROPERTY_COMMENT", "REFINE_SEARCH_REQUEST", "SHOWCASE_SEARCH_CHANGED",
                                "TOUR_REQUEST", "PROPERTY_SYNC_UPDATE"}),
    ("forms", "Forms & invites", {"FORM_SUBMISSION", "INVITE_RSVP"}),
    ("emails", "Emails", {"EMAIL_ACTIVITY", "EMAIL_STATUS", "MAILBOX_DISCONNECTED"}),
    ("hosting", "Hosting & team", {"HOST_REQUEST_RECEIVED", "HOST_REQUEST_APPROVED", "HOST_REQUEST_DECLINED", "HOST_REQUEST_COUNTERED",
                                   "HOST_REQUEST_COUNTER_RESPONSE", "HOST_REQUEST_WITHDRAWN", "DIRECT_HOST_REQUEST_RECEIVED",
                                   "TEAM_BROADCAST_RECEIVED", "TEAM_BROADCAST_RESPONSE", "TEAM_BROADCAST_CLAIMED", "TEAM_INVITE_RECEIVED",
                                   "TEAM_INVITE_ACCEPTED", "TEAM_INVITE_DECLINED", "TEAM_NEW_LISTING"}),
    ("reviews", "Reviews", {"NEW_REVIEW"}),
    ("followups", "Follow-up reminders", {"TASK_REMINDER"}),
]
_GROUP_OF = {t: key for key, _, types in _GROUPS for t in types}
_GROUP_LABEL = {key: label for key, label, _ in _GROUPS} | {"other": "Everything else"}
RUNDOWN_TYPE = "SHOWCASE_DIGEST"   # the bell's "Today's Rundown" (older ones say "Today's Showcase update")
MAX_TIMELINE = 120
BRIEFING_EMAIL = "showcase_daily_digest_agent%"   # the daily briefing email (and its _v2 template)


# Once the agent has looked at today's Rundown - opened it in the app, or clicked something in today's
# briefing email - the big Dash card steps aside for a small "Oct 26 Briefing" button in the header
# (owner, 10/26). Anything that happens after that look lights the button up with how many.
# Just OPENING the email no longer counts (owner, 11/18): iPhone Mail loads every email on its own in
# the background, so an "open" can happen without the agent ever looking.
async def seen_state(db: AsyncSession, user: User, now: datetime | None = None, bell: Notification | None = None,
                     bell_known: bool = False) -> dict[str, Any]:
    from app.models.database import EmailLog
    now = now or datetime.now(timezone.utc)
    start = day_start(now)
    local_day = now.astimezone(EASTERN).date().isoformat()
    looks: list[tuple[datetime, str]] = []
    row = await _seen_row(db, user)
    if row.get("day") == local_day and row.get("at"):
        try:
            looks.append((datetime.fromisoformat(row["at"]), "app"))
        except ValueError:
            pass
    clicked = (await db.execute(select(func.max(EmailLog.clicked_at)).where(
        EmailLog.owner_id == user.id, EmailLog.audience == "agent", EmailLog.template.like(BRIEFING_EMAIL),
        EmailLog.created_at >= start))).scalar()
    if clicked:
        looks.append((clicked, "email"))
    if not looks:
        return {"seen": False, "seen_at": None, "via": None, "new_since": 0}
    at, via = max(looks, key=lambda t: t[0])
    new = await _count(db, select(func.count(Notification.id)).where(
        Notification.agent_id == user.id, Notification.created_at > at, Notification.created_at >= start,
        Notification.type != RUNDOWN_TYPE))
    if not bell_known:
        bell = (await db.execute(select(Notification).where(
            Notification.agent_id == user.id, Notification.type == RUNDOWN_TYPE, Notification.reference_id == f"digest-{local_day}",
        ).limit(1))).scalars().first()
    if bell is not None and bell.created_at and bell.created_at > at:
        new += 1   # the briefing itself went out after they looked
    return {"seen": True, "seen_at": at, "via": via, "new_since": new}


async def _seen_row(db: AsyncSession, user: User) -> dict:
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"), {"k": f"rundown_seen:{user.id}"})).scalar()
    return row if isinstance(row, dict) else {}


async def catchup_since(db: AsyncSession, user: User, now: datetime | None = None) -> datetime | None:
    """When they last opened the Rundown in the app BEFORE today - everything after that and before
    today's midnight is "Since you last looked" (owner, 11/18). Only opening it in the app counts: the
    email is the short version. None = nothing to catch up on (or never opened it at all)."""
    now = now or datetime.now(timezone.utc)
    row = await _seen_row(db, user)
    raw = row.get("prev") if row.get("day") == now.astimezone(EASTERN).date().isoformat() else row.get("at")
    try:
        since = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None
    return since if since and since < day_start(now) else None


async def mark_seen(db: AsyncSession, user: User, now: datetime | None = None) -> None:
    import json
    now = now or datetime.now(timezone.utc)
    local_day = now.astimezone(EASTERN).date().isoformat()
    old = await _seen_row(db, user)
    # The first look of a new day keeps the last look before it - the catch-up stays up until midnight.
    prev = old.get("prev") if old.get("day") == local_day else old.get("at")
    value = {"day": local_day, "at": now.isoformat(), "prev": prev}
    await db.execute(text(
        "INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"k": f"rundown_seen:{user.id}", "v": json.dumps(value)})
    await db.commit()


async def todays_status(db: AsyncSession, user: User, now: datetime | None = None) -> dict[str, Any]:
    """The header button's few facts - cheap enough to ask every minute (no Rundown build)."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(EASTERN)
    return {"date": local.date().isoformat(), **await seen_state(db, user, now)}


def day_start(now: datetime | None = None) -> datetime:
    """Midnight Eastern today, as UTC."""
    local = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    return datetime.combine(local.date(), time.min, EASTERN).astimezone(timezone.utc)


def _rel(link: str | None) -> str:
    """The briefing builds full links for the email - the app wants paths."""
    from app.services.agent_email_service import app_url
    if not link:
        return ""
    base = app_url()
    if link.startswith(base):
        return link[len(base):] or "/"
    return link


async def _count(db: AsyncSession, stmt) -> int:
    try:
        return int((await db.execute(stmt)).scalar() or 0)
    except Exception:  # noqa: BLE001 - one number failing never blanks the Rundown
        logger.warning("rundown count failed", exc_info=True)
        await db.rollback()
        return 0


async def _showcase_news(db: AsyncSession, user: User, start: datetime, end: datetime | None = None) -> dict | None:
    """The briefing's Showcase section: the agent's digest rows still waiting for their next
    briefing, plus the ones today's briefing already rolled up. Read only.
    With `end` (the catch-up): only the news earlier briefings rolled up between start and end.
    Still-waiting news reaches back 10 days, not 2 (owner, 11/18): an agent on the once-a-week email
    has all week's news waiting for Monday, and the Dash showed only the last 2 days of it."""
    from app.services.showcase_digest_service import QUEUED, ROLLED_UP, _engagement, briefing_showcases
    from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS
    if not owner_has_access(user, SHOWCASE_ACCESS_TIERS):
        return None
    which = ((ScheduledEmail.status == ROLLED_UP) & (ScheduledEmail.sent_at >= start) & (ScheduledEmail.sent_at < end)) if end else or_(
        (ScheduledEmail.status == QUEUED) & (ScheduledEmail.created_at >= start - timedelta(days=10)),
        (ScheduledEmail.status == ROLLED_UP) & (ScheduledEmail.sent_at >= start))
    rows = (await db.execute(
        select(ScheduledEmail.template_variables).where(
            ScheduledEmail.template_variables["_audience"].astext == "agent",
            ScheduledEmail.template_variables["_owner_id"].astext == user.id,
            ScheduledEmail.created_at >= start - timedelta(days=10),
            which,
        )
    )).scalars().all()
    rows = sorted((dict(v or {}) for v in rows), key=lambda v: v.get("_seq") or 0)
    if not rows:
        return None
    per_col: dict[str, list[dict]] = {}
    for v in rows:
        per_col.setdefault(str(v.get("_collection_id")), []).append(v)
    cols = {c.id: c for c in (await db.execute(
        select(Collection).options(selectinload(Collection.owner)).where(Collection.id.in_(list(per_col)))
    )).scalars().all()}
    entries = [(cols[cid], crow) for cid, crow in per_col.items()
               if cid in cols and cols[cid].status == "ACTIVE" and getattr(cols[cid], "notify_agent", True)]
    if not entries:
        return None
    return briefing_showcases(entries, lambda p: p, await _engagement(db, [c.id for c, _ in entries]))


MAX_STAT_PEOPLE = 30


def _win(col, start: datetime, end: datetime | None):
    """start <= col (< end, for the catch-up's days)."""
    return (col >= start) & (col < end) if end is not None else col >= start


def _who(name: str | None, fallback: str = "A client") -> str:
    return " ".join(w[:1].upper() + w[1:] for w in (name or "").split()) or fallback


async def _stat_people(db: AsyncSession, user: User, start: datetime, notes: list, links: dict, sc: dict | None,
                       end: datetime | None = None) -> dict[str, list[dict]]:
    """Who is behind each of the day's numbers (owner, 10/30: "3 homes viewed" has to say WHO and take
    you to that exact person / home). stat key -> rows {who, what, at, link}, newest first. A link
    to a Showcase home carries &glow=<home> so the Showcases page scrolls to it and lights it up.
    Each list is its own query - one failing leaves that tile with just its page link."""
    from app.models.database import FormSubmission, GuestPassEvent, Lead, LeadEvent, OpenHouseEvent, OpenHouseVisitor, Property, PropertyInteraction
    from app.services.agent_roundup_service import nice_street
    out: dict[str, list[dict]] = {}

    def win(col):
        return _win(col, start, end)

    async def rows(key: str, build) -> None:
        try:
            out[key] = (await build())[:MAX_STAT_PEOPLE]
        except Exception:  # noqa: BLE001
            logger.warning("rundown stat people failed", exc_info=True, extra={"stat": key})
            await db.rollback()

    def street(p) -> str:
        return nice_street(p.street_address or p.unparsed_address or "", p.city or "") if p else "a home"

    not_self_lead = or_(Lead.email.is_(None), func.lower(Lead.email) != func.lower(user.email or ""))

    async def new_leads():
        res = await db.execute(select(Lead).where(Lead.owner_id == user.id, win(Lead.created_at), Lead.hidden_at.is_(None), not_self_lead)
                               .order_by(Lead.created_at.desc()).limit(MAX_STAT_PEOPLE))
        return [{"who": _who(l.name, l.email or "New lead"), "what": "New lead", "at": l.created_at, "link": f"/leads/{l.id}"}
                for l in res.scalars().all()]

    async def lead_events(kinds: list[str], what: str):
        res = await db.execute(select(LeadEvent, Lead).join(Lead, Lead.id == LeadEvent.lead_id)
                               .where(Lead.owner_id == user.id, LeadEvent.source_type.in_(kinds), win(LeadEvent.occurred_at),
                                      ~LeadEvent.source_id.in_(select(OpenHouseVisitor.id).where(OpenHouseVisitor.owner_test.is_(True))))
                               .order_by(LeadEvent.occurred_at.desc()).limit(MAX_STAT_PEOPLE))
        return [{"who": _who(l.name, l.email or "A visitor"), "what": e.summary or what, "at": e.occurred_at, "link": f"/leads/{l.id}"}
                for e, l in res.all()]

    async def forms():
        res = await db.execute(select(FormSubmission).where(FormSubmission.owner_id == user.id, win(FormSubmission.created_at),
                                                            FormSubmission.owner_test.isnot(True))
                               .order_by(FormSubmission.created_at.desc()).limit(MAX_STAT_PEOPLE))
        return [{"who": _who(s.contact_name, s.contact_email or "Someone"),
                 "what": f"Answered your {(s.form_type or 'form').replace('_', ' ')} form" + (f" · {s.property_address}" if s.property_address else ""),
                 "at": s.created_at, "link": f"/studio/forms?tab=results&formId={s.form_id}&submission={s.id}"}
                for s in res.scalars().all()]

    async def guest_pass():
        res = await db.execute(select(GuestPassEvent, OpenHouseVisitor, OpenHouseEvent)
                               .join(OpenHouseVisitor, OpenHouseVisitor.id == GuestPassEvent.visitor_id)
                               .join(OpenHouseEvent, OpenHouseEvent.id == GuestPassEvent.open_house_event_id)
                               .where(OpenHouseEvent.agent_id == user.id, GuestPassEvent.kind == "session", win(GuestPassEvent.created_at))
                               .order_by(GuestPassEvent.created_at.desc()).limit(MAX_STAT_PEOPLE))
        return [{"who": _who(v.full_name, "A guest"), "what": "Opened their Guest Pass", "at": g.created_at,
                 "link": f"/leads/{v.lead_id}" if v.lead_id else "/open-houses"} for g, v, _ in res.all()]

    async def emails():
        return [{"who": n.title, "what": n.message or "", "at": n.created_at, "link": links.get(n.id) or n.link or "/activity"}
                for n in notes if n.type == "EMAIL_ACTIVITY"]

    async def likes():
        return [{"who": n.title, "what": n.message or n.property_address or "", "at": n.created_at, "link": links.get(n.id) or n.link or "/showcases"}
                for n in notes if n.type == "PROPERTY_INTERACTION" and (n.message or "").startswith("Liked")]

    async def browsing():
        res = await db.execute(select(Collection).where(Collection.owner_id == user.id, win(Collection.last_visitor_activity_at))
                               .order_by(Collection.last_visitor_activity_at.desc()).limit(MAX_STAT_PEOPLE))
        return [{"who": _who(c.visitor_name), "what": f"Looking at their Showcase · {nice_street(c.name)}", "at": c.last_visitor_activity_at,
                 "link": f"/showcases?showcase={c.id}"} for c in res.scalars().all()]

    async def viewed():
        res = await db.execute(select(PropertyInteraction, Collection, Property)
                               .join(Collection, Collection.id == PropertyInteraction.collection_id)
                               .join(Property, Property.id == PropertyInteraction.property_id)
                               .where(Collection.owner_id == user.id, win(PropertyInteraction.last_viewed_at))
                               .order_by(PropertyInteraction.last_viewed_at.desc()).limit(MAX_STAT_PEOPLE))
        return [{"who": _who(c.visitor_name), "what": f"Viewed {street(p)}", "at": i.last_viewed_at,
                 "link": f"/showcases?showcase={c.id}&glow={p.id}"} for i, c, p in res.all()]

    def matches(drop: bool) -> list[dict]:
        items = []
        for g in (sc or {}).get("home_groups") or []:
            if bool(g.get("is_price_drop")) != drop:
                continue
            what = (f"Price drop: {g.get('street_address')} · now {g.get('new_price') or g.get('price')}" if drop
                    else f"New match: {g.get('street_address')}" + (f" · {g.get('price')}" if g.get("price") else ""))
            key = g.get("listing_key") or ""
            for c in g.get("clients") or []:
                link = c.get("link") or "/showcases"
                items.append({"who": c.get("name") or "A client", "what": what, "at": None,
                              "link": f"{link}&glow={key}" if key and "?" in link else link})
        return items

    await rows("new_leads", new_leads)
    await rows("sign_ins", lambda: lead_events(["open_house_visitor"], "Signed in at your open house"))
    await rows("forms", forms)
    await rows("tours", lambda: lead_events(["property_tour", "property_page_tour_request", "property_website_tour_request"], "Asked for a tour"))
    await rows("guest_pass", guest_pass)
    await rows("emails", emails)
    await rows("likes", likes)
    await rows("active_clients", browsing)
    await rows("homes_viewed", viewed)
    out["new_homes"] = matches(False)[:MAX_STAT_PEOPLE]
    out["price_drops"] = matches(True)[:MAX_STAT_PEOPLE]
    return out


def _headline(sc: dict | None, brief: dict, happened: int) -> str:
    """Same wording as the briefing email's headline, with a calm line for a quiet day."""
    from app.services.showcase_digest_service import _plural
    if sc and (sc["new"] or sc["drops"]):
        new_part = _plural(sc["new"], "new home", "new homes") if sc["new"] else ""
        drop_part = _plural(sc["drops"], "price drop", "price drops") if sc["drops"] else ""
        both = " and ".join(p for p in (new_part, drop_part) if p)
        return f"{both} across {_plural(len(sc['showcases']), 'client Showcase', 'client Showcases')}"
    if brief.get("followup_count"):
        n = brief["followup_count"]
        return (_plural(n, "follow-up waiting on you", "follow-ups waiting on you") if brief.get("followup_overdue")
                else _plural(n, "follow-up for today", "follow-ups for today"))
    waiting = len(brief.get("actions") or []) + len(brief.get("client_activity") or [])
    if waiting:
        return _plural(waiting, "thing waiting on you", "things waiting on you")
    if happened:
        return _plural(happened, "thing happened today", "things happened today")
    return "A quiet day so far"


async def _happenings(db: AsyncSession, user: User, notes: list) -> tuple[list[dict], dict]:
    """Bell items as Rundown rows (each with its group key), plus the exact links (for the numbers)."""
    from app.models.database import OpenHouseVisitor
    from app.services.notification_links import exact_links, showcase_links
    try:
        links = await exact_links(db, user.id, notes)
        showcases = await showcase_links(db, user.id, notes)
    except Exception:  # noqa: BLE001
        logger.warning("rundown exact links failed", exc_info=True)
        await db.rollback()
        links, showcases = {}, {}
    # Open house guests say whether they have an agent, like the briefing email (owner, 11/01).
    visitor_ids = [n.reference_id for n in notes if n.reference_type == "VISITOR" and n.reference_id]
    agent_of: dict[str, str] = {}
    if visitor_ids:
        for vid, has in (await db.execute(select(OpenHouseVisitor.id, OpenHouseVisitor.has_agent)
                                          .where(OpenHouseVisitor.id.in_(visitor_ids)))).all():
            agent_of[vid] = {"YES": "Has an agent", "NO": "No agent"}.get(str(has or "").upper(), "")
    done_of = await _done_marks(db, user, notes)
    items = [{
        "id": n.id, "type": n.type, "title": n.title, "message": n.message, "address": n.property_address,
        "link": links.get(n.id) or n.link or "", "showcase_link": showcases.get(n.id), "is_read": bool(n.is_read), "at": n.created_at,
        "agent_status": agent_of.get(n.reference_id) if n.reference_type == "VISITOR" else None,
        "done": done_of.get(n.id),
        "_group": _GROUP_OF.get(n.type, "other"),
    } for n in notes]
    return items, links


async def _done_marks(db: AsyncSession, user: User, notes: list) -> dict[str, str]:
    """What you already took care of gets a green check (owner, 11/19: a follow-up reminder for a text already
    sent looked like it still needed doing): a follow-up reminder whose follow-up is checked off -> "Done";
    a tour request you confirmed -> "Confirmed" (marked done -> "Done"). Never fatal."""
    from app.models.database import AgentTask, PropertyTour
    out: dict[str, str] = {}
    try:
        task_ids = {n.reference_id for n in notes if n.type == "TASK_REMINDER" and n.reference_type == "TASK" and n.reference_id}
        if task_ids:
            done = {tid for tid, at in (await db.execute(select(AgentTask.id, AgentTask.completed_at).where(
                AgentTask.id.in_(task_ids), AgentTask.owner_id == user.id))).all() if at is not None}
            out.update({n.id: "Done" for n in notes if n.type == "TASK_REMINDER" and n.reference_id in done})
        tour_ids = {n.reference_id for n in notes if n.type == "TOUR_REQUEST" and n.reference_type == "TOUR" and n.reference_id}
        if tour_ids:
            state = {tid: ("Done" if finished else "Confirmed") for tid, cdate, finished in (await db.execute(
                select(PropertyTour.id, PropertyTour.confirmed_date, PropertyTour.is_completed).where(PropertyTour.id.in_(tour_ids)))).all()
                if finished or cdate}
            out.update({n.id: state[n.reference_id] for n in notes if n.type == "TOUR_REQUEST" and n.reference_id in state})
    except Exception:  # noqa: BLE001
        logger.warning("rundown done marks failed", exc_info=True)
        await db.rollback()
    return out


def _grouped(items: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it["_group"], []).append({k: v for k, v in it.items() if k != "_group"})
    order = [k for k, _, _ in _GROUPS] + ["other"]
    return [{"key": k, "label": _GROUP_LABEL[k], "items": groups[k]} for k in order if groups.get(k)]


async def _numbers(db: AsyncSession, user: User, start: datetime, end: datetime | None, notes: list, links: dict,
                   sc: dict | None, showcase_access: bool) -> list[dict]:
    """The tiles - today's (end None), or the catch-up's days (start..end). The catch-up leaves out
    "Clients browsing" and "Homes viewed": those only keep each client's / home's LATEST visit, so
    someone who also browsed today would vanish from the earlier days."""
    from app.models.database import FormSubmission, GuestPassEvent, Lead, LeadEvent, OpenHouseEvent, OpenHouseVisitor, PropertyInteraction
    # The agent's own test sign-ins / form answers / a lead with their own email never count (11/13).
    not_self_lead = or_(Lead.email.is_(None), func.lower(Lead.email) != func.lower(user.email or ""))
    own_tests = select(OpenHouseVisitor.id).where(OpenHouseVisitor.owner_test.is_(True))
    lead_events = lambda kinds: (select(func.count(LeadEvent.id)).join(Lead, Lead.id == LeadEvent.lead_id)  # noqa: E731
                                 .where(Lead.owner_id == user.id, LeadEvent.source_type.in_(kinds), _win(LeadEvent.occurred_at, start, end)))
    if end is None:
        emails = sum(1 for n in notes if n.type == "EMAIL_ACTIVITY")
        likes = sum(1 for n in notes if n.type == "PROPERTY_INTERACTION" and (n.message or "").startswith("Liked"))
    else:   # the catch-up's list is capped - count these for real
        bell = lambda *w: select(func.count(Notification.id)).where(  # noqa: E731
            Notification.agent_id == user.id, _win(Notification.created_at, start, end), *w)
        emails = await _count(db, bell(Notification.type == "EMAIL_ACTIVITY"))
        likes = await _count(db, bell(Notification.type == "PROPERTY_INTERACTION", Notification.message.like("Liked%")))
    stats = [
        {"key": "new_leads", "label": "New leads", "link": "/leads",
         "value": await _count(db, select(func.count(Lead.id)).where(Lead.owner_id == user.id, _win(Lead.created_at, start, end), not_self_lead))},
        {"key": "sign_ins", "label": "Sign-ins", "link": "/leads",
         "value": await _count(db, lead_events(["open_house_visitor"]).where(~LeadEvent.source_id.in_(own_tests)))},
        {"key": "forms", "label": "Form responses", "link": "/studio/forms",
         "value": await _count(db, select(func.count(FormSubmission.id)).where(FormSubmission.owner_id == user.id, _win(FormSubmission.created_at, start, end),
                                                                               FormSubmission.owner_test.isnot(True)))},
        {"key": "tours", "label": "Tours asked for", "link": "/leads",
         "value": await _count(db, lead_events(["property_tour", "property_page_tour_request", "property_website_tour_request"]))},
        {"key": "guest_pass", "label": "Guest Pass visits", "link": "/open-houses",
         "value": await _count(db, select(func.count(GuestPassEvent.id)).join(OpenHouseEvent, OpenHouseEvent.id == GuestPassEvent.open_house_event_id)
                                .where(OpenHouseEvent.agent_id == user.id, GuestPassEvent.kind == "session", _win(GuestPassEvent.created_at, start, end)))},
        {"key": "emails", "label": "Emails opened", "link": "/activity", "value": emails},
    ]
    if showcase_access:
        news = [
            {"key": "new_homes", "label": "New matches", "link": "/showcases", "value": (sc or {}).get("new", 0)},
            {"key": "price_drops", "label": "Price drops", "link": "/showcases", "value": (sc or {}).get("drops", 0)},
        ]
        if end is None:
            news += [
                {"key": "active_clients", "label": "Clients browsing", "link": "/showcases",
                 "value": await _count(db, select(func.count(Collection.id)).where(Collection.owner_id == user.id, Collection.last_visitor_activity_at >= start))},
                {"key": "homes_viewed", "label": "Homes viewed", "link": "/showcases",
                 "value": await _count(db, select(func.count(PropertyInteraction.id)).join(Collection, Collection.id == PropertyInteraction.collection_id)
                                        .where(Collection.owner_id == user.id, PropertyInteraction.last_viewed_at >= start))},
            ]
        stats[3:3] = news + [{"key": "likes", "label": "Likes", "link": "/showcases", "value": likes}]
    people = await _stat_people(db, user, start, notes, links, sc, end=end)
    for s in stats:
        s["people"] = people.get(s["key"], [])
    return stats


CATCHUP_DAYS = 7      # the catch-up reaches back a week at most
MAX_CATCHUP = 300     # bell items listed across those days; the rest are on the Activity page


def _day_label(d, today) -> str:
    if (today - d).days == 1:
        return "Yesterday"
    return f"{d.strftime('%A, %b')} {d.day}"


async def build_catchup(db: AsyncSession, user: User, now: datetime, showcase_access: bool) -> dict | None:
    """"Since you last looked" (owner, 11/18): when the agent didn't open the Rundown for a day or more,
    today's Rundown also shows what happened between their last look and today's midnight - the
    days' numbers, each day's bell items, and the Showcase news earlier briefings rolled up. Only the
    app counts as a look (the email is the short version). Once seen it stays up until midnight, like
    everything else. None when there's nothing to catch up on."""
    since = await catchup_since(db, user, now)
    if since is None:
        return None
    start = day_start(now)
    floor = start - timedelta(days=CATCHUP_DAYS)
    capped = since < floor
    begin = floor if capped else since
    in_window = (Notification.agent_id == user.id, _win(Notification.created_at, begin, start), Notification.type != RUNDOWN_TYPE)
    local_date = func.date(func.timezone(EASTERN.key, Notification.created_at))
    per_day = {d: (int(total), int(unread or 0)) for d, total, unread in (await db.execute(
        select(local_date, func.count(Notification.id), func.count(Notification.id).filter(Notification.is_read.is_(False)))
        .where(*in_window).group_by(local_date))).all()}
    notes = (await db.execute(select(Notification).where(*in_window)
                              .order_by(Notification.created_at.desc()).limit(MAX_CATCHUP))).scalars().all()
    try:
        sc = await _showcase_news(db, user, begin, start) if showcase_access else None
    except Exception:  # noqa: BLE001
        logger.warning("rundown catch-up showcase news failed", exc_info=True, extra={"user_id": user.id})
        await db.rollback()
        sc = None
    items, links = await _happenings(db, user, list(notes))
    stats = await _numbers(db, user, begin, start, list(notes), links, sc, showcase_access)
    if not per_day and not any(s["value"] for s in stats):
        return None
    today = now.astimezone(EASTERN).date()
    by_day: dict = {}
    for it in items:
        by_day.setdefault(it["at"].astimezone(EASTERN).date(), []).append(it)
    days = []
    for d in sorted(per_day, reverse=True):
        shown = by_day.get(d, [])
        total, unread = per_day[d]
        days.append({"date": d.isoformat(), "label": _day_label(d, today), "count": total, "unread": unread,
                     "more": max(0, total - len(shown)), "groups": _grouped(shown)})
    homes = []
    for g in (sc or {}).get("home_groups", [])[:40]:
        homes.append({k: g.get(k) for k in ("street_address", "city", "state", "image", "beds", "baths", "sqft", "price",
                                             "is_price_drop", "old_price", "new_price", "savings", "clients", "clients_more", "clients_label")})
    since_local = since.astimezone(EASTERN)
    first = begin.astimezone(EASTERN).date()
    if capped:
        range_label = f"Your last {CATCHUP_DAYS} days"
    elif (today - since_local.date()).days == 1:
        range_label = f"Yesterday after {since_local.strftime('%I:%M %p').lstrip('0')}"
    else:
        range_label = f"{_day_label(first, today)} to yesterday"
    return {
        "since": since, "from": begin, "capped": capped, "days_away": (today - since_local.date()).days,
        "range_label": range_label,
        "stats": stats,
        "count": sum(t for t, _ in per_day.values()),
        "unread": sum(u for _, u in per_day.values()),
        "days": days,
        "showcase": {"new": sc["new"], "drops": sc["drops"], "clients": len(sc["showcases"]), "homes": homes} if sc else None,
    }


async def build_rundown(db: AsyncSession, user: User, now: datetime | None = None) -> dict[str, Any]:
    from app.services.email_settings_service import briefing_hm, briefing_label
    from app.services.showcase_digest_service import _briefing
    from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS

    now = now or datetime.now(timezone.utc)
    start = day_start(now)
    local = now.astimezone(EASTERN)
    local_day = local.date().isoformat()
    showcase_access = owner_has_access(user, SHOWCASE_ACCESS_TIERS)

    # --- the briefing itself: did it go out today, and when does it --------------------------------
    h, m = briefing_hm(user)
    ran = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"), {"k": f"briefing_day:{user.id}"})).scalar()
    bell = (await db.execute(select(Notification).where(
        Notification.agent_id == user.id, Notification.type == RUNDOWN_TYPE, Notification.reference_id == f"digest-{local_day}",
    ).order_by(Notification.created_at.desc()).limit(1))).scalars().first()
    briefing = {
        "time_label": briefing_label(user),
        "sent": bell is not None,
        "sent_at": bell.created_at if bell else None,
        "ran_today": ran == local_day,
        "upcoming": (local.hour, local.minute) < (h, m),
        "notification_id": bell.id if bell else None,
    }

    # --- things to do: exactly the briefing's sections, uncapped ------------------------------------
    try:
        brief = await _briefing(db, user, now, showcase_access=showcase_access, full=True)
    except Exception:  # noqa: BLE001
        logger.warning("rundown briefing sections failed", exc_info=True, extra={"user_id": user.id})
        await db.rollback()
        brief = {"followups": [], "actions": [], "client_activity": [], "followup_count": 0, "followup_overdue": 0, "followups_label": ""}
    followups = [{"title": f["title"], "when": f["when"], "overdue": f["overdue"], "link": f.get("_link") or "/leads/tasks"}
                 for f in brief["followups"]]
    todo = [{**a, "link": _rel(a.get("link"))} for a in brief["actions"]]
    client_asks = [{**a, "link": _rel(a.get("link"))} for a in brief["client_activity"]]

    # --- Showcase news (the briefing's home cards) ---------------------------------------------------
    try:
        sc = await _showcase_news(db, user, start)
    except Exception:  # noqa: BLE001
        logger.warning("rundown showcase news failed", exc_info=True, extra={"user_id": user.id})
        await db.rollback()
        sc = None
    homes = []
    if sc:
        for g in sc["home_groups"][:40]:
            homes.append({k: g.get(k) for k in ("street_address", "city", "state", "image", "beds", "baths", "sqft", "price",
                                                 "is_price_drop", "old_price", "new_price", "savings", "clients", "clients_more", "clients_label")})

    # --- everything that happened today (the day's bell items, grouped) -----------------------------
    notes = (await db.execute(select(Notification).where(
        Notification.agent_id == user.id, Notification.created_at >= start, Notification.type != RUNDOWN_TYPE,
    ).order_by(Notification.created_at.desc()).limit(MAX_TIMELINE))).scalars().all()
    # The list stops at MAX_TIMELINE - the real total says how many more are on the Activity page (11/18).
    happened_total = len(notes) if len(notes) < MAX_TIMELINE else await _count(db, select(func.count(Notification.id)).where(
        Notification.agent_id == user.id, Notification.created_at >= start, Notification.type != RUNDOWN_TYPE))
    items, links = await _happenings(db, user, list(notes))
    happened = _grouped(items)
    unread_today = sum(1 for n in notes if not n.is_read) + (1 if bell is not None and not bell.is_read else 0)

    # --- the day's numbers ---------------------------------------------------------------------------
    stats = await _numbers(db, user, start, None, list(notes), links, sc, showcase_access)

    # --- since you last looked: the days in between (owner, 11/18) ------------------------------------
    try:
        catchup = await build_catchup(db, user, now, showcase_access)
    except Exception:  # noqa: BLE001 - the catch-up failing never blanks today's Rundown
        logger.warning("rundown catch-up failed", exc_info=True, extra={"user_id": user.id})
        await db.rollback()
        catchup = None

    # Leads without an agent opening / clicking the same email again - a quiet "worth a check-in"
    # with a one-tap follow-up (lead_interest_service, owner 11/13).
    try:
        from app.services import lead_interest_service
        interest = await lead_interest_service.signals(db, user.id, now=now, limit=6)
    except Exception:  # noqa: BLE001
        logger.warning("rundown interest signals failed", exc_info=True)
        await db.rollback()
        interest = []

    to_do_count = len(followups) + len(todo) + len(client_asks)
    return {
        "date": local_day,
        "date_label": f"{local.strftime('%A, %B')} {local.day}",
        "generated_at": now,
        "first_name": user.first_name or "",
        "headline": _headline(sc, brief, len(notes)),
        "briefing": briefing,
        "stats": stats,
        "to_do_count": to_do_count,
        "followups": followups,
        "followups_label": brief.get("followups_label") or "",
        "followup_overdue": brief.get("followup_overdue") or 0,
        "actions": todo,
        "client_activity": client_asks,
        "interest": interest,
        "showcase": {"new": sc["new"], "drops": sc["drops"], "clients": len(sc["showcases"]), "homes": homes} if sc else None,
        "happened": happened,
        "happened_count": len(notes),
        "happened_total": happened_total,
        "catchup": catchup,
        "unread_today": unread_today,
        "seen": await seen_state(db, user, now, bell=bell, bell_known=True),
    }


async def mark_today_read(db: AsyncSession, user: User, now: datetime | None = None) -> int:
    """"Mark all as read": every notification from today (Eastern), the Rundown's own included - and
    the catch-up's days, when there are any (11/18)."""
    start = day_start(now)
    since = await catchup_since(db, user, now)
    if since is not None:
        start = max(since, start - timedelta(days=CATCHUP_DAYS))
    res = await db.execute(
        update(Notification)
        .where(Notification.agent_id == user.id, Notification.is_read.is_(False), Notification.created_at >= start)
        .values(is_read=True, read_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return res.rowcount or 0
