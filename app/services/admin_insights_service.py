"""Admin Insights - site-wide numbers: how OpenHousePal is being used by agents and by their guests.

One builder per Insights page (Overview, Sign-ins, Showcases, Emails, Tools, Guests). Each takes a
period in days (0 = all time) and returns headline numbers, a day-by-day (or week-by-week) series
and a per-agent table.

Privacy, same rule as the Activity tracker (admin_activity_service): agents are named, their clients
and guests never are - only counts. Times are Eastern."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from sqlalchemy import and_, distinct, exists, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import database as m
from app.services import admin_activity_service as act

TZ = "America/New_York"
PERIODS = (7, 30, 90, 365, 0)

# What a guest does on a Showcase (LeadEvent.source_type) - all of these mean "the client used it".
# Not collection_visitor: that's written when a Showcase is MADE for someone, not something they did.
SHOWCASE_GUEST_EVENTS = ("showcase_view", "showcase_like", "showcase_dislike", "showcase_refine", "showcase_search_changed",
                         "property_comment", "property_tour")
SHOWCASE_EMAIL_TEMPLATES = ("showcase_daily_digest", "showcase_weekly_digest", "showcase_ready", "showcase_shared")

ORIGIN_LABELS = {"open_house": "Open house sign-ins", "showcase": "Showcases", "forms": "Forms & invites",
                 "listings": "Spotlight & listing pages", "website": "Website & blog", "portal": "Zillow & portals",
                 "review": "Reviews", "other": "Added by hand / other"}

TEMPLATE_LABELS = {
    "showcase_daily_digest": "Showcase daily update", "showcase_weekly_digest": "Showcase weekly update",
    "showcase_ready": "Showcase ready", "showcase_shared": "Showcase shared", "visitor_confirmation": "Sign-in thank you",
    "agent_comment_response": "Agent replied to a comment", "tour_confirmed": "Tour confirmed",
    "spotlight_alert_confirm": "Spotlight alert sign-up", "spotlight_listing_update": "Spotlight listing update",
    "showcase_daily_digest_agent": "Daily briefing (agent)", "liked_homes_roundup": "Liked homes round-up (agent)",
    "open_house_recap": "Open house recap (agent)", "agent_welcome": "Welcome (agent)", "trial_ending_soon": "Trial ending (agent)",
}


# ---------------------------------------------------------------- helpers

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _since(days: int, now: datetime) -> Optional[datetime]:
    return now - timedelta(days=days) if days else None


def _in(col, since: Optional[datetime]):
    return col >= since if since is not None else true()


def _unit(days: int) -> str:
    return "day" if days and days <= 90 else "week"


def _bucket(col, unit: str):
    return func.date_trunc(unit, func.timezone(TZ, col))


def pct(a: float, b: float) -> Optional[float]:
    return round(100.0 * a / b, 1) if b else None


def _avg(xs: list) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


def _median(xs: list) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return float(s[n // 2]) if n % 2 else round((s[n // 2 - 1] + s[n // 2]) / 2, 1)


def _name(u) -> str:
    return f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email


def _buckets(days: int, now: datetime, first: Optional[date]) -> list[date]:
    """Every bucket start (Eastern) from the start of the period (or the first data) to today."""
    unit = _unit(days)
    today = now.astimezone(_eastern()).date()
    start = (today - timedelta(days=days - 1)) if days else (first or today)
    if unit == "week":
        start = start - timedelta(days=start.weekday())
    out, d = [], start
    while d <= today:
        out.append(d)
        d += timedelta(days=1 if unit == "day" else 7)
    return out


def _eastern():
    from zoneinfo import ZoneInfo
    return ZoneInfo(TZ)


async def _count_series(db: AsyncSession, col, unit: str, since, *where, joins: Iterable = ()) -> dict[date, int]:
    b = _bucket(col, unit)
    q = select(b.label("b"), func.count()).select_from(col.class_ if hasattr(col, "class_") else None)
    for jm, on in joins:
        q = q.join(jm, on)
    q = q.where(col.isnot(None), _in(col, since), *where).group_by(b)
    return {r[0].date(): r[1] for r in (await db.execute(q)).all()}


def _merge_series(buckets: list[date], **series: dict[date, int]) -> list[dict]:
    return [{"d": d.isoformat(), **{k: s.get(d, 0) for k, s in series.items()}} for d in buckets]


async def _first_day(db: AsyncSession, *cols) -> Optional[date]:
    firsts = []
    for c in cols:
        v = (await db.execute(select(func.min(func.timezone(TZ, c))))).scalar()
        if v:
            firsts.append(v.date())
    return min(firsts) if firsts else None


async def _by_owner(db: AsyncSession, q) -> dict[str, int]:
    return {uid: n for uid, n in (await db.execute(q)).all() if uid}


async def _agent_map(db: AsyncSession) -> dict[str, Any]:
    return {u.id: u for u in await act._agents(db)}


def _agent_row(u) -> dict:
    return {"id": u.id, "name": _name(u), "plan_tier": u.plan_tier, "subscription_status": u.subscription_status}


def _meta(days: int, now: datetime) -> dict:
    return {"days": days, "unit": _unit(days), "generated_at": now}


# ---------------------------------------------------------------- overview

async def overview(db: AsyncSession, days: int) -> dict:
    now = _now()
    since = _since(days, now)
    unit = _unit(days)
    agents = await _agent_map(db)
    ids = list(agents)

    # Agents in the app: one row per agent per Eastern day (agent_activity_tracker).
    active_rows = (await db.execute(select(m.AgentActiveDay.user_id, m.AgentActiveDay.day).where(m.AgentActiveDay.user_id.in_(ids)))).all() if ids else []
    today = now.astimezone(_eastern()).date()

    def active_within(n: int) -> int:
        return len({u for u, d in active_rows if (today - d).days < n})

    first = min((d for _, d in active_rows), default=None)
    buckets = _buckets(days, now, min([x for x in (first, await _first_day(db, m.User.created_at)) if x], default=None))
    step = 1 if unit == "day" else 7
    active_series = {b: len({u for u, d in active_rows if b <= d < b + timedelta(days=step)}) for b in buckets}
    joined = await _count_series(db, m.User.created_at, unit, since, m.User.id.in_(ids))

    signins = await _count_series(db, m.OpenHouseVisitor.created_at, unit, since)
    showcases = await _count_series(db, m.Collection.created_at, unit, since)
    leads = await _count_series(db, m.Lead.created_at, unit, since)
    guest = await _count_series(db, m.LeadEvent.occurred_at, unit, since, m.LeadEvent.source_type.in_(SHOWCASE_GUEST_EVENTS))

    ce_q = select(func.count(), func.count(m.ClientEmail.opened_at), func.count(m.ClientEmail.clicked_at)).where(
        m.ClientEmail.status == "sent", _in(m.ClientEmail.sent_at, since))
    ce_sent, ce_open, ce_click = (await db.execute(ce_q)).one()
    emails_total = (await db.execute(select(func.count()).select_from(m.EmailLog).where(_in(m.EmailLog.created_at, since)))).scalar() or 0

    statuses = [(u.subscription_status or "").upper() for u in agents.values()]
    plans: dict[str, int] = {}
    for u in agents.values():
        plans[u.plan_tier or "NONE"] = plans.get(u.plan_tier or "NONE", 0) + 1
    return {
        **_meta(days, now),
        "agents": {
            "total": len(agents), "joined": sum(joined.values()),
            "active_today": active_within(1), "active_7": active_within(7), "active_30": active_within(30),
            "active_in_period": len({u for u, d in active_rows if not since or d >= since.astimezone(_eastern()).date()}),
            "paying": statuses.count("ACTIVE"), "trial": statuses.count("TRIAL"),
            "cancelled": sum(1 for s in statuses if s in ("CANCELLED", "EXPIRED", "SUSPENDED")),
            "plans": plans,
        },
        "headline": {
            "signins": sum(signins.values()), "showcases": sum(showcases.values()), "leads": sum(leads.values()),
            "guest_showcase_actions": sum(guest.values()), "emails": emails_total,
            "client_emails": ce_sent, "client_open_rate": pct(ce_open, ce_sent), "client_click_rate": pct(ce_click, ce_sent),
        },
        "series": _merge_series(buckets, active=active_series, joined=joined, signins=signins, showcases=showcases, leads=leads),
    }


# ---------------------------------------------------------------- sign-ins

async def signins(db: AsyncSession, days: int) -> dict:
    now = _now()
    since = _since(days, now)
    unit = _unit(days)
    agents = await _agent_map(db)
    V, E, W = m.OpenHouseVisitor, m.OpenHouseEvent, m.OpenHouseSignInView
    join_e = [(E, E.id == V.open_house_event_id)]

    rows = (await db.execute(
        select(E.agent_id, V.signin_seconds, V.signin_mode, V.signin_device, V.has_agent, V.lead_id, func.timezone(TZ, V.created_at))
        .join(E, E.id == V.open_house_event_id).where(_in(V.created_at, since))
    )).all()
    views = await _by_owner(db, select(E.agent_id, func.count()).select_from(W).join(E, E.id == W.open_house_event_id)
                            .where(_in(W.created_at, since)).group_by(E.agent_id))
    tracked_views = (await db.execute(select(func.count(), func.count(W.visitor_id)).where(_in(W.created_at, since), W.mode.isnot(None)))).one()

    secs = [r[1] for r in rows if r[1]]
    by_mode: dict[str, list] = {}
    for r in rows:
        by_mode.setdefault(r[2] or "unknown", []).append(r[1])
    devices: dict[str, int] = {}
    for r in rows:
        devices[r[3] or "unknown"] = devices.get(r[3] or "unknown", 0) + 1
    hours = [0] * 24
    weekdays = [0] * 7
    for r in rows:
        hours[r[6].hour] += 1
        weekdays[r[6].weekday()] += 1
    buckets_s = [(0, 30, "Under 30 sec"), (30, 60, "30-60 sec"), (60, 120, "1-2 min"), (120, 300, "2-5 min"), (300, 10 ** 6, "Over 5 min")]

    # Open houses that happened in the period (scheduled date, else created) - and how many got sign-ins.
    today = now.astimezone(_eastern()).date()
    start_day = since.astimezone(_eastern()).date() if since else None
    vis_n = select(V.open_house_event_id.label("oh"), func.count().label("n")).group_by(V.open_house_event_id).subquery()
    oh_rows = (await db.execute(
        select(E.agent_id, E.scheduled_date, E.created_at, func.coalesce(vis_n.c.n, 0))
        .outerjoin(vis_n, vis_n.c.oh == E.id).where(E.is_deleted.is_(False) | E.is_deleted.is_(None))
    )).all()
    held: dict[str, list] = {}
    for uid, sched, created, n in oh_rows:
        d = sched or (created.astimezone(_eastern()).date() if created else today)
        if d <= today and (start_day is None or d >= start_day):
            held.setdefault(uid, []).append(n)
    showcases_from_oh = (await db.execute(select(func.count()).select_from(m.Collection).where(
        m.Collection.original_open_house_event_id.isnot(None), _in(m.Collection.created_at, since)))).scalar() or 0

    first = await _first_day(db, V.created_at, W.created_at)
    buckets = _buckets(days, now, first)
    series = _merge_series(
        buckets,
        signins=await _count_series(db, V.created_at, unit, since),
        views=await _count_series(db, W.created_at, unit, since),
    )

    total = len(rows)
    all_views = sum(views.values())
    per_agent: dict[str, dict] = {}
    for r in rows:
        a = per_agent.setdefault(r[0], {"signins": 0, "secs": []})
        a["signins"] += 1
        if r[1]:
            a["secs"].append(r[1])
    table = []
    for uid, u in agents.items():
        a = per_agent.get(uid, {"signins": 0, "secs": []})
        h = held.get(uid, [])
        table.append({**_agent_row(u), "open_houses": len(h), "with_signins": sum(1 for n in h if n), "signins": a["signins"],
                      "views": views.get(uid, 0), "completion": min(100.0, pct(a["signins"], views.get(uid, 0)) or 0) if views.get(uid) else None,
                      "avg_seconds": _avg(a["secs"]), "per_open_house": _avg(h) if h else None})
    table.sort(key=lambda r: (-r["signins"], -r["open_houses"], r["name"]))

    all_held = [n for h in held.values() for n in h]
    forms = await _forms(db, since, agents)
    return {
        **_meta(days, now),
        "kpis": {
            "signins": total, "views": all_views,
            "completion": min(100.0, pct(total, all_views) or 0) if all_views else None,
            "avg_seconds": _avg(secs), "median_seconds": _median(secs), "timed": len(secs),
            "open_houses": len(all_held), "open_houses_with_signins": sum(1 for n in all_held if n),
            "per_open_house": _avg(all_held), "has_agent": pct(sum(1 for r in rows if r[4] == "YES"), total),
            "showcases_made": showcases_from_oh, "showcase_rate": pct(showcases_from_oh, total),
            "tracked_views": tracked_views[0], "tracked_finished": tracked_views[1],
        },
        "modes": [{"key": k, "count": len(v), "avg_seconds": _avg([s for s in v if s])} for k, v in sorted(by_mode.items(), key=lambda kv: -len(kv[1]))],
        "devices": [{"key": k, "count": n} for k, n in sorted(devices.items(), key=lambda kv: -kv[1])],
        "durations": [{"label": lbl, "count": sum(1 for s in secs if lo <= s < hi)} for lo, hi, lbl in buckets_s],
        "hours": hours, "weekdays": weekdays,
        "series": series, "agents": table, "forms": forms,
    }


async def _forms(db: AsyncSession, since, agents: dict) -> dict:
    """Agent Hub forms (Typeform-style): opened, started, finished, and how long finishing took."""
    S, F = m.FormViewSession, m.AgentForm
    rows = (await db.execute(
        select(S.owner_id, F.form_type, S.started_at, S.completed_at).join(F, F.id == S.form_id).where(_in(S.created_at, since))
    )).all()
    by_type: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
    for uid, ftype, started, done in rows:
        for bucket in (by_type.setdefault(ftype or "other", {"views": 0, "started": 0, "finished": 0, "secs": []}),
                       by_agent.setdefault(uid, {"views": 0, "started": 0, "finished": 0, "secs": []})):
            bucket["views"] += 1
            bucket["started"] += 1 if started else 0
            bucket["finished"] += 1 if done else 0
            if started and done:
                s = (done - started).total_seconds()
                if 1 <= s <= 30 * 60:
                    bucket["secs"].append(s)

    def out(b: dict) -> dict:
        return {"views": b["views"], "started": b["started"], "finished": b["finished"],
                "finish_rate": pct(b["finished"], b["views"]), "avg_seconds": _avg(b["secs"])}
    total = {"views": 0, "started": 0, "finished": 0, "secs": []}
    for b in by_type.values():
        for k in ("views", "started", "finished"):
            total[k] += b[k]
        total["secs"] += b["secs"]
    return {
        "totals": out(total),
        "types": [{"key": k, **out(v)} for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]["views"])],
        "agents": sorted([{**_agent_row(agents[uid]), **out(v)} for uid, v in by_agent.items() if uid in agents], key=lambda r: -r["views"]),
    }


# ---------------------------------------------------------------- showcases

def _used_clause():
    """The client opened their Showcase at least once (any visitor action on it)."""
    C, L, P = m.Collection, m.LeadEvent, m.PropertyInteraction
    return or_(
        C.last_visitor_activity_at.isnot(None),
        exists().where(L.collection_id == C.id, L.source_type.in_(SHOWCASE_GUEST_EVENTS)),
        exists().where(P.collection_id == C.id, or_(P.view_count > 0, P.liked.is_(True), P.disliked.is_(True))),
    )


async def showcases(db: AsyncSession, days: int) -> dict:
    now = _now()
    since = _since(days, now)
    unit = _unit(days)
    agents = await _agent_map(db)
    C, L = m.Collection, m.LeadEvent
    used = _used_clause()
    act7 = C.last_visitor_activity_at >= now - timedelta(days=7)
    act30 = C.last_visitor_activity_at >= now - timedelta(days=30)

    q = select(C.owner_id, C.source, C.status, C.inactive_reason, used.label("used"), act7.label("a7"), act30.label("a30"),
               _in(C.created_at, since).label("new"))
    rows = (await db.execute(q)).all()
    made = [r for r in rows if r.new]
    live = [r for r in rows if (r.status or "ACTIVE") == "ACTIVE"]

    # What clients did on Showcases in the period.
    ev = dict((await db.execute(select(L.source_type, func.count()).where(
        L.source_type.in_(SHOWCASE_GUEST_EVENTS), L.collection_id.isnot(None), _in(L.occurred_at, since)).group_by(L.source_type))).all())
    comments = (await db.execute(select(func.count()).select_from(m.PropertyComment).where(
        m.PropertyComment.is_agent.is_(False), _in(m.PropertyComment.created_at, since)))).scalar() or 0
    tours = (await db.execute(select(func.count()).select_from(m.PropertyTour).where(_in(m.PropertyTour.created_at, since)))).scalar() or 0

    ce = (await db.execute(select(func.count(), func.count(m.ClientEmail.opened_at), func.count(m.ClientEmail.clicked_at)).where(
        m.ClientEmail.template.in_(SHOWCASE_EMAIL_TEMPLATES), m.ClientEmail.status == "sent", _in(m.ClientEmail.sent_at, since)))).one()

    first = await _first_day(db, C.created_at)
    buckets = _buckets(days, now, first)
    series = _merge_series(
        buckets,
        made=await _count_series(db, C.created_at, unit, since),
        client_actions=await _count_series(db, L.occurred_at, unit, since, L.source_type.in_(SHOWCASE_GUEST_EVENTS), L.collection_id.isnot(None)),
    )

    sources: dict[str, int] = {}
    for r in made:
        sources[r.source or "manual"] = sources.get(r.source or "manual", 0) + 1
    asleep: dict[str, int] = {}
    for r in rows:
        if (r.status or "ACTIVE") != "ACTIVE":
            asleep[r.inactive_reason or "agent"] = asleep.get(r.inactive_reason or "agent", 0) + 1

    likes_by = await _by_owner(db, select(C.owner_id, func.count()).select_from(L).join(C, C.id == L.collection_id).where(
        L.source_type == "showcase_like", _in(L.occurred_at, since)).group_by(C.owner_id))
    actions_by = await _by_owner(db, select(C.owner_id, func.count()).select_from(L).join(C, C.id == L.collection_id).where(
        L.source_type.in_(SHOWCASE_GUEST_EVENTS), _in(L.occurred_at, since)).group_by(C.owner_id))
    tours_by = await _by_owner(db, select(C.owner_id, func.count()).select_from(m.PropertyTour).join(C, C.id == m.PropertyTour.collection_id)
                               .where(_in(m.PropertyTour.created_at, since)).group_by(C.owner_id))
    table = []
    for uid, u in agents.items():
        mine = [r for r in rows if r.owner_id == uid]
        new = [r for r in mine if r.new]
        table.append({**_agent_row(u), "total": len(mine), "made": len(new), "used": sum(1 for r in mine if r.used),
                      "used_rate": pct(sum(1 for r in mine if r.used), len(mine)),
                      "active_7": sum(1 for r in mine if r.a7), "asleep": sum(1 for r in mine if (r.status or "ACTIVE") != "ACTIVE"),
                      "client_actions": actions_by.get(uid, 0), "likes": likes_by.get(uid, 0), "tours": tours_by.get(uid, 0)})
    table.sort(key=lambda r: (-r["total"], r["name"]))

    return {
        **_meta(days, now),
        "kpis": {
            "total": len(rows), "made": len(made), "made_used": sum(1 for r in made if r.used),
            "made_used_rate": pct(sum(1 for r in made if r.used), len(made)),
            "used": sum(1 for r in rows if r.used), "used_rate": pct(sum(1 for r in rows if r.used), len(rows)),
            "live": len(live), "active_7": sum(1 for r in live if r.a7), "active_30": sum(1 for r in live if r.a30),
            "active_7_rate": pct(sum(1 for r in live if r.a7), len(live)),
            "asleep": len(rows) - len(live),
            "views": ev.get("showcase_view", 0), "likes": ev.get("showcase_like", 0), "passes": ev.get("showcase_dislike", 0),
            "comments": comments, "tours": tours, "search_changes": ev.get("showcase_refine", 0) + ev.get("showcase_search_changed", 0),
            "emails_sent": ce[0], "email_open_rate": pct(ce[1], ce[0]), "email_click_rate": pct(ce[2], ce[0]),
        },
        "sources": [{"key": k, "count": n} for k, n in sorted(sources.items(), key=lambda kv: -kv[1])],
        "asleep": [{"key": k, "count": n} for k, n in sorted(asleep.items(), key=lambda kv: -kv[1])],
        "series": series, "agents": table,
    }


# ---------------------------------------------------------------- emails

async def emails(db: AsyncSession, days: int) -> dict:
    from app.services import email_log_service
    await email_log_service.flush()
    now = _now()
    since = _since(days, now)
    unit = _unit(days)
    agents = await _agent_map(db)
    G, CE = m.EmailLog, m.ClientEmail

    log = (await db.execute(select(G.template, G.audience, G.status, G.delivery, G.via, G.owner_id, G.client_email_id).where(_in(G.created_at, since)))).all()
    tracking_from = (await db.execute(select(func.min(G.created_at)))).scalar()
    ce_rows = (await db.execute(select(CE.id, CE.owner_id, CE.template, CE.via, CE.delivery, CE.opened_at, CE.clicked_at, CE.status).where(
        CE.status.in_(("sent", "failed")), _in(func.coalesce(CE.sent_at, CE.created_at), since)))).all()
    # Client emails from before every email was logged (or sent from the agent's own mailbox before
    # then) still count as emails sent - added as if they'd been logged.
    logged = {r.client_email_id for r in log if r.client_email_id}
    log = list(log) + [SimpleNamespace(template=r.template, audience="client", status=r.status, delivery=r.delivery, via=r.via or "openhousepal",
                                       owner_id=r.owner_id, client_email_id=r.id) for r in ce_rows if r.id not in logged]

    def bad(d) -> bool:
        return d in ("bounced", "blocked")

    audiences = {}
    for a in ("agent", "client", "account"):
        rs = [r for r in log if r.audience == a]
        audiences[a] = {"sent": sum(1 for r in rs if r.status != "failed"), "failed": sum(1 for r in rs if r.status == "failed"),
                        "held": sum(1 for r in rs if r.status == "held"), "bounced": sum(1 for r in rs if bad(r.delivery)),
                        "spam": sum(1 for r in rs if r.delivery == "spam")}

    sent_ce = [r for r in ce_rows if r.status == "sent"]
    client = {
        "sent": len(sent_ce), "failed": sum(1 for r in ce_rows if r.status == "failed"),
        "opened": sum(1 for r in sent_ce if r.opened_at), "clicked": sum(1 for r in sent_ce if r.clicked_at),
        "bounced": sum(1 for r in ce_rows if bad(r.delivery)), "spam": sum(1 for r in ce_rows if r.delivery == "spam"),
        "delivered": sum(1 for r in sent_ce if r.delivery == "delivered"),
        "via_mailbox": sum(1 for r in sent_ce if r.via == "mailbox"), "via_openhousepal": sum(1 for r in sent_ce if r.via != "mailbox"),
    }
    for k in ("opened", "clicked", "bounced", "spam"):
        client[f"{k}_rate"] = pct(client[k], client["sent"] + (client["failed"] if k == "bounced" else 0))

    templates: dict[str, dict] = {}
    for r in log:
        t = templates.setdefault(r.template, {"template": r.template, "audience": r.audience, "sent": 0, "failed": 0, "bounced": 0, "spam": 0,
                                              "tracked": 0, "opened": 0, "clicked": 0})
        if r.status == "failed":
            t["failed"] += 1
        else:
            t["sent"] += 1
        t["bounced"] += 1 if bad(r.delivery) else 0
        t["spam"] += 1 if r.delivery == "spam" else 0
    for r in sent_ce:  # opens / clicks only exist on tracked client emails
        t = templates.setdefault(r.template, {"template": r.template, "audience": "client", "sent": 0, "failed": 0, "bounced": 0, "spam": 0,
                                              "tracked": 0, "opened": 0, "clicked": 0})
        t["tracked"] += 1
        t["opened"] += 1 if r.opened_at else 0
        t["clicked"] += 1 if r.clicked_at else 0
    for t in templates.values():
        t["label"] = TEMPLATE_LABELS.get(t["template"], t["template"].replace("_v2", "").replace("_", " ").capitalize())
        t["open_rate"], t["click_rate"] = pct(t["opened"], t["tracked"]), pct(t["clicked"], t["tracked"])

    first = await _first_day(db, G.created_at, CE.created_at)
    buckets = _buckets(days, now, first)
    series = _merge_series(
        buckets,
        all=await _count_series(db, G.created_at, unit, since),
        client=await _count_series(db, CE.sent_at, unit, since, CE.status == "sent"),
        opened=await _count_series(db, CE.opened_at, unit, since),
        clicked=await _count_series(db, CE.clicked_at, unit, since),
    )

    per: dict[str, dict] = {}
    for r in log:
        if r.owner_id and r.audience == "agent":
            per.setdefault(r.owner_id, {}).setdefault("to_agent", 0)
            per[r.owner_id]["to_agent"] += 1
    for r in ce_rows:
        p = per.setdefault(r.owner_id, {})
        p["client_sent"] = p.get("client_sent", 0) + (1 if r.status == "sent" else 0)
        p["opened"] = p.get("opened", 0) + (1 if r.status == "sent" and r.opened_at else 0)
        p["clicked"] = p.get("clicked", 0) + (1 if r.status == "sent" and r.clicked_at else 0)
        p["bounced"] = p.get("bounced", 0) + (1 if bad(r.delivery) or r.delivery == "spam" else 0)
        p["mailbox"] = p.get("mailbox", 0) + (1 if r.status == "sent" and r.via == "mailbox" else 0)
    mailbox_owners = set((await db.execute(select(m.MailConnection.owner_id))).scalars().all())
    table = []
    for uid, u in agents.items():
        p = per.get(uid, {})
        cs = p.get("client_sent", 0)
        table.append({**_agent_row(u), "to_agent": p.get("to_agent", 0), "client_sent": cs, "opened": p.get("opened", 0),
                      "open_rate": pct(p.get("opened", 0), cs), "click_rate": pct(p.get("clicked", 0), cs),
                      "bounced": p.get("bounced", 0), "mailbox": p.get("mailbox", 0), "mailbox_connected": uid in mailbox_owners})
    table.sort(key=lambda r: (-r["client_sent"], -r["to_agent"], r["name"]))

    total_sent = sum(a["sent"] for a in audiences.values())
    total_failed = sum(a["failed"] for a in audiences.values())
    return {
        **_meta(days, now),
        "tracking_from": tracking_from,
        "kpis": {
            "total": total_sent + total_failed, "sent": total_sent, "failed": total_failed,
            "held": sum(a["held"] for a in audiences.values()),
            "bounced": sum(a["bounced"] for a in audiences.values()), "spam": sum(a["spam"] for a in audiences.values()),
            "delivered": sum(1 for r in log if r.delivery == "delivered"),
        },
        "audiences": audiences, "client": client,
        "templates": sorted(templates.values(), key=lambda t: -(t["sent"] + t["failed"])),
        "series": series, "agents": table,
    }


# ---------------------------------------------------------------- tools (what agents use)

async def tools(db: AsyncSession, days: int) -> dict:
    now = _now()
    since = _since(days, now)
    unit = _unit(days)
    agents = await _agent_map(db)
    ids = set(agents)
    span = timedelta(days=days) if days else None

    features, counts = [], {}
    for f in act.FEATURES:
        t, owner = f.time(), f.owner_col()
        base = select(owner.label("uid"), func.count().label("n")).select_from(f.model)
        for jm, on in f.joins:
            base = base.join(jm, on)
        base = base.where(t.isnot(None), *f.where)
        cur = {uid: n for uid, n in (await db.execute(base.where(_in(t, since)).group_by(owner))).all() if uid in ids}
        prev = {}
        if span:
            prev = {uid: n for uid, n in (await db.execute(base.where(t >= since - span, t < since).group_by(owner))).all() if uid in ids}
        counts[f.key] = cur
        features.append({"key": f.key, "label": f.label, "group": f.group, "kind": f.kind,
                         "uses": sum(cur.values()), "agents": len(cur), "agents_rate": pct(len(cur), len(ids)),
                         "prev_uses": sum(prev.values()) if span else None})

    active = await _by_owner(db, select(m.AgentActiveDay.user_id, func.count()).where(
        m.AgentActiveDay.user_id.in_(ids) if ids else true(),
        m.AgentActiveDay.day >= since.astimezone(_eastern()).date() if since else true()).group_by(m.AgentActiveDay.user_id))
    first = (await db.execute(select(func.min(m.AgentActiveDay.day)))).scalar()
    buckets = _buckets(days, now, first)
    action_series: dict[date, int] = {}
    for f in act.FEATURES:
        if f.kind != "action":
            continue
        joins = list(f.joins)
        s = await _count_series(db, f.time(), unit, since, *f.where, joins=joins)
        for k, v in s.items():
            action_series[k] = action_series.get(k, 0) + v

    table = []
    for uid, u in agents.items():
        c = {k: counts[k].get(uid, 0) for k in counts}
        actions = sum(n for k, n in c.items() if act.FEATURE_BY_KEY[k].kind == "action")
        table.append({**_agent_row(u), "last_seen_at": u.last_seen_at, "active_days": active.get(uid, 0), "actions": actions,
                      "features_used": sum(1 for k, n in c.items() if n and act.FEATURE_BY_KEY[k].kind == "action"), "counts": c})
    table.sort(key=lambda r: (-r["actions"], -r["active_days"], r["name"]))
    return {**_meta(days, now), "groups": act.GROUPS, "features": features,
            "series": _merge_series(buckets, actions=action_series), "agents": table}


# ---------------------------------------------------------------- guests (what agents' visitors do)

async def guests(db: AsyncSession, days: int) -> dict:
    now = _now()
    since = _since(days, now)
    unit = _unit(days)
    agents = await _agent_map(db)
    H, PV, PW, B, L, S = m.AgentHubEvent, m.PropertyWebsiteView, m.PropertyWebsite, m.BlogEvent, m.LeadEvent, m.FormViewSession

    hub = dict((await db.execute(select(H.event_type, func.count()).where(_in(H.created_at, since)).group_by(H.event_type))).all())
    hub_unique = (await db.execute(select(func.count(distinct(H.visitor_id))).where(H.event_type == "visit", _in(H.created_at, since)))).scalar() or 0
    spot = (await db.execute(select(func.count(), func.count(distinct(PV.visitor_id))).where(_in(PV.created_at, since)))).one()
    blog = dict((await db.execute(select(B.event_type, func.count()).where(_in(B.created_at, since)).group_by(B.event_type))).all())
    form = (await db.execute(select(func.count(), func.count(S.started_at), func.count(S.completed_at)).where(_in(S.created_at, since)))).one()
    oh_views = (await db.execute(select(func.count()).select_from(m.OpenHouseSignInView).where(_in(m.OpenHouseSignInView.created_at, since)))).scalar() or 0
    oh_signins = (await db.execute(select(func.count()).select_from(m.OpenHouseVisitor).where(_in(m.OpenHouseVisitor.created_at, since)))).scalar() or 0
    sc = dict((await db.execute(select(L.source_type, func.count()).where(L.source_type.in_(SHOWCASE_GUEST_EVENTS), _in(L.occurred_at, since))
                                .group_by(L.source_type))).all())
    comments = (await db.execute(select(func.count()).select_from(m.PropertyComment).where(m.PropertyComment.is_agent.is_(False),
                                                                                          _in(m.PropertyComment.created_at, since)))).scalar() or 0
    tours = (await db.execute(select(func.count()).select_from(m.PropertyTour).where(_in(m.PropertyTour.created_at, since)))).scalar() or 0
    opens = (await db.execute(select(func.count()).select_from(m.ClientEmail).where(m.ClientEmail.opened_at.isnot(None), _in(m.ClientEmail.opened_at, since)))).scalar() or 0
    clicks = (await db.execute(select(func.count()).select_from(m.ClientEmail).where(m.ClientEmail.clicked_at.isnot(None), _in(m.ClientEmail.clicked_at, since)))).scalar() or 0

    # Where new leads first came from (the kind of first touch - never who).
    from app.api.lead_routes import _origin_group
    first_ev = (select(L.lead_id, func.min(L.occurred_at).label("at")).group_by(L.lead_id).subquery())
    origin_rows = (await db.execute(
        select(L).join(first_ev, and_(first_ev.c.lead_id == L.lead_id, first_ev.c.at == L.occurred_at)).where(_in(L.occurred_at, since))
    )).scalars().all()
    origins: dict[str, int] = {}
    seen_leads = set()
    for e in origin_rows:
        if e.lead_id in seen_leads:
            continue
        seen_leads.add(e.lead_id)
        g = _origin_group(e)
        origins[g] = origins.get(g, 0) + 1

    first = await _first_day(db, H.created_at, PV.created_at, m.OpenHouseVisitor.created_at, L.occurred_at)
    buckets = _buckets(days, now, first)
    series = _merge_series(
        buckets,
        hub=await _count_series(db, H.created_at, unit, since, H.event_type == "visit"),
        spotlight=await _count_series(db, PV.created_at, unit, since),
        signins=await _count_series(db, m.OpenHouseVisitor.created_at, unit, since),
        showcase=await _count_series(db, L.occurred_at, unit, since, L.source_type.in_(SHOWCASE_GUEST_EVENTS)),
        forms=await _count_series(db, S.completed_at, unit, since),
    )

    hub_by = await _by_owner(db, select(H.owner_id, func.count()).where(H.event_type == "visit", _in(H.created_at, since)).group_by(H.owner_id))
    spot_by = await _by_owner(db, select(PW.owner_id, func.count()).select_from(PV).join(PW, PW.id == PV.property_website_id)
                              .where(_in(PV.created_at, since)).group_by(PW.owner_id))
    signin_by = await _by_owner(db, select(m.OpenHouseEvent.agent_id, func.count()).select_from(m.OpenHouseVisitor)
                                .join(m.OpenHouseEvent, m.OpenHouseEvent.id == m.OpenHouseVisitor.open_house_event_id)
                                .where(_in(m.OpenHouseVisitor.created_at, since)).group_by(m.OpenHouseEvent.agent_id))
    form_by = await _by_owner(db, select(S.owner_id, func.count()).where(S.completed_at.isnot(None), _in(S.completed_at, since)).group_by(S.owner_id))
    sc_by = await _by_owner(db, select(m.Lead.owner_id, func.count()).select_from(L).join(m.Lead, m.Lead.id == L.lead_id)
                            .where(L.source_type.in_(SHOWCASE_GUEST_EVENTS), _in(L.occurred_at, since)).group_by(m.Lead.owner_id))
    open_by = await _by_owner(db, select(m.ClientEmail.owner_id, func.count()).where(m.ClientEmail.opened_at.isnot(None),
                                                                                  _in(m.ClientEmail.opened_at, since)).group_by(m.ClientEmail.owner_id))
    table = []
    for uid, u in agents.items():
        r = {**_agent_row(u), "hub": hub_by.get(uid, 0), "spotlight": spot_by.get(uid, 0), "signins": signin_by.get(uid, 0),
             "forms": form_by.get(uid, 0), "showcase": sc_by.get(uid, 0), "email_opens": open_by.get(uid, 0)}
        r["total"] = r["hub"] + r["spotlight"] + r["signins"] + r["forms"] + r["showcase"]
        table.append(r)
    table.sort(key=lambda r: (-r["total"], r["name"]))

    return {
        **_meta(days, now),
        "kpis": {
            "hub_visits": hub.get("visit", 0), "hub_unique": hub_unique, "hub_qr": hub.get("qr_scan", 0), "hub_clicks": hub.get("interaction", 0),
            "spotlight_visits": spot[0], "spotlight_unique": spot[1],
            "blog_views": blog.get("view", 0), "blog_clicks": sum(v for k, v in blog.items() if k != "view"),
            "form_views": form[0], "form_started": form[1], "form_finished": form[2],
            "signin_views": oh_views, "signins": oh_signins,
            "showcase_views": sc.get("showcase_view", 0), "likes": sc.get("showcase_like", 0), "passes": sc.get("showcase_dislike", 0),
            "comments": comments, "tours": tours, "search_changes": sc.get("showcase_refine", 0) + sc.get("showcase_search_changed", 0),
            "email_opens": opens, "email_clicks": clicks, "new_leads": len(seen_leads),
        },
        "origins": [{"key": k, "label": ORIGIN_LABELS.get(k, k), "count": n} for k, n in sorted(origins.items(), key=lambda kv: -kv[1])],
        "series": series, "agents": table,
    }


BUILDERS = {"overview": overview, "signins": signins, "showcases": showcases, "emails": emails, "tools": tools, "guests": guests}
