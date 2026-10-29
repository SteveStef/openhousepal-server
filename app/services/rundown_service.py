"""Today's Rundown - the agent's daily briefing, in the app (user, 10/24).

One source of truth with the daily briefing email (showcase_digest_service.send_agent_briefings):
the same "things to do" (_briefing) and the same Showcase news (the agent's queued digest rows,
grouped per home by briefing_showcases) - just nothing capped, plus everything that happened today
(the day's notifications, grouped) and the day's numbers. Before the briefing has gone out today it
is "so far today"; after, it is what the briefing said plus anything since.

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
                                   "OPEN_HOUSE_DISCLOSURE", "OPEN_HOUSE_FORM_REQUEST"}),
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


# Once the agent has looked at today's Rundown - opened it in the app, or opened / clicked today's
# briefing email - the big Dash card steps aside for a small "Oct 26 Briefing" button in the header
# (owner, 10/26). Anything that happens after that look lights the button up with how many.
async def seen_state(db: AsyncSession, user: User, now: datetime | None = None, bell: Notification | None = None,
                     bell_known: bool = False) -> dict[str, Any]:
    from app.models.database import EmailLog
    now = now or datetime.now(timezone.utc)
    start = day_start(now)
    local_day = now.astimezone(EASTERN).date().isoformat()
    looks: list[tuple[datetime, str]] = []
    row = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"), {"k": f"rundown_seen:{user.id}"})).scalar()
    if isinstance(row, dict) and row.get("day") == local_day and row.get("at"):
        try:
            looks.append((datetime.fromisoformat(row["at"]), "app"))
        except ValueError:
            pass
    opened = (await db.execute(select(func.max(func.greatest(EmailLog.opened_at, EmailLog.clicked_at))).where(
        EmailLog.owner_id == user.id, EmailLog.audience == "agent", EmailLog.template.like(BRIEFING_EMAIL),
        EmailLog.created_at >= start))).scalar()
    if opened:
        looks.append((opened, "email"))
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


async def mark_seen(db: AsyncSession, user: User, now: datetime | None = None) -> None:
    import json
    now = now or datetime.now(timezone.utc)
    value = {"day": now.astimezone(EASTERN).date().isoformat(), "at": now.isoformat()}
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


async def _showcase_news(db: AsyncSession, user: User, start: datetime) -> dict | None:
    """The briefing's Showcase section: the agent's digest rows still waiting for their next
    briefing, plus the ones today's briefing already rolled up. Read only."""
    from app.services.showcase_digest_service import QUEUED, ROLLED_UP, _engagement, briefing_showcases
    from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS
    if not owner_has_access(user, SHOWCASE_ACCESS_TIERS):
        return None
    rows = (await db.execute(
        select(ScheduledEmail.template_variables).where(
            ScheduledEmail.template_variables["_audience"].astext == "agent",
            ScheduledEmail.template_variables["_owner_id"].astext == user.id,
            ScheduledEmail.created_at >= start - timedelta(days=2),
            or_(ScheduledEmail.status == QUEUED, (ScheduledEmail.status == ROLLED_UP) & (ScheduledEmail.sent_at >= start)),
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


async def build_rundown(db: AsyncSession, user: User, now: datetime | None = None) -> dict[str, Any]:
    from app.models.database import FormSubmission, GuestPassEvent, Lead, LeadEvent, OpenHouseEvent, PropertyInteraction
    from app.services.email_settings_service import briefing_hm, briefing_label
    from app.services.notification_links import exact_links, showcase_links
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
    try:
        links = await exact_links(db, user.id, list(notes))
        showcases = await showcase_links(db, user.id, list(notes))
    except Exception:  # noqa: BLE001
        logger.warning("rundown exact links failed", exc_info=True)
        links, showcases = {}, {}
    groups: dict[str, list[dict]] = {}
    for n in notes:
        key = _GROUP_OF.get(n.type, "other")
        groups.setdefault(key, []).append({
            "id": n.id, "type": n.type, "title": n.title, "message": n.message, "address": n.property_address,
            "link": links.get(n.id) or n.link or "", "showcase_link": showcases.get(n.id), "is_read": bool(n.is_read), "at": n.created_at,
        })
    order = [k for k, _, _ in _GROUPS] + ["other"]
    happened = [{"key": k, "label": _GROUP_LABEL[k], "items": groups[k]} for k in order if groups.get(k)]
    unread_today = sum(1 for n in notes if not n.is_read) + (1 if bell is not None and not bell.is_read else 0)

    # --- the day's numbers ---------------------------------------------------------------------------
    lead_events = lambda kinds: (select(func.count(LeadEvent.id)).join(Lead, Lead.id == LeadEvent.lead_id)  # noqa: E731
                                 .where(Lead.owner_id == user.id, LeadEvent.source_type.in_(kinds), LeadEvent.occurred_at >= start))
    likes = sum(1 for n in notes if n.type == "PROPERTY_INTERACTION" and (n.message or "").startswith("Liked"))
    stats = [
        {"key": "new_leads", "label": "New leads", "link": "/leads",
         "value": await _count(db, select(func.count(Lead.id)).where(Lead.owner_id == user.id, Lead.created_at >= start))},
        {"key": "sign_ins", "label": "Sign-ins", "link": "/leads",
         "value": await _count(db, lead_events(["open_house_visitor"]))},
        {"key": "forms", "label": "Form responses", "link": "/studio/forms",
         "value": await _count(db, select(func.count(FormSubmission.id)).where(FormSubmission.owner_id == user.id, FormSubmission.created_at >= start))},
        {"key": "tours", "label": "Tours asked for", "link": "/leads",
         "value": await _count(db, lead_events(["property_tour", "property_page_tour_request", "property_website_tour_request"]))},
        {"key": "guest_pass", "label": "Guest Pass visits", "link": "/open-houses",
         "value": await _count(db, select(func.count(GuestPassEvent.id)).join(OpenHouseEvent, OpenHouseEvent.id == GuestPassEvent.open_house_event_id)
                                .where(OpenHouseEvent.agent_id == user.id, GuestPassEvent.kind == "session", GuestPassEvent.created_at >= start))},
        {"key": "emails", "label": "Emails opened", "link": "/activity",
         "value": sum(1 for n in notes if n.type == "EMAIL_ACTIVITY")},
    ]
    if showcase_access:
        stats[3:3] = [
            {"key": "new_homes", "label": "New matches", "link": "/showcases", "value": (sc or {}).get("new", 0)},
            {"key": "price_drops", "label": "Price drops", "link": "/showcases", "value": (sc or {}).get("drops", 0)},
            {"key": "active_clients", "label": "Clients browsing", "link": "/showcases",
             "value": await _count(db, select(func.count(Collection.id)).where(Collection.owner_id == user.id, Collection.last_visitor_activity_at >= start))},
            {"key": "homes_viewed", "label": "Homes viewed", "link": "/showcases",
             "value": await _count(db, select(func.count(PropertyInteraction.id)).join(Collection, Collection.id == PropertyInteraction.collection_id)
                                    .where(Collection.owner_id == user.id, PropertyInteraction.last_viewed_at >= start))},
            {"key": "likes", "label": "Likes", "link": "/showcases", "value": likes},
        ]

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
        "showcase": {"new": sc["new"], "drops": sc["drops"], "clients": len(sc["showcases"]), "homes": homes} if sc else None,
        "happened": happened,
        "happened_count": len(notes),
        "unread_today": unread_today,
        "seen": await seen_state(db, user, now, bell=bell, bell_known=True),
    }


async def mark_today_read(db: AsyncSession, user: User, now: datetime | None = None) -> int:
    """"Mark all as read": every notification from today (Eastern), the Rundown's own included."""
    res = await db.execute(
        update(Notification)
        .where(Notification.agent_id == user.id, Notification.is_read.is_(False), Notification.created_at >= day_start(now))
        .values(is_read=True, read_at=datetime.now(timezone.utc))
    )
    await db.commit()
    return res.rowcount or 0
