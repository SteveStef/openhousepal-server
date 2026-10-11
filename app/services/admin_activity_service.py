"""Admin activity tracker - what every agent is using, how often, and whether it's working for them
(are their open houses getting sign-ins, are leads coming in).

Privacy: the admin sees each AGENT's activity, never their clients. Nothing here returns a lead's,
guest's or reviewer's name, email or phone - only counts and what KIND of thing happened ("Got an
open house sign-in at 239 Windham Dr", "Got a new lead from a Spotlight").

Each feature is one row in FEATURES: the table, whose it is, which time column marks a use. One
grouped query per feature covers every agent at once."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import Date, and_, cast, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import database as m


@dataclass
class Feature:
    key: str
    label: str
    group: str  # open_house | leads | marketing | tools | follow_up | results
    kind: str   # action = something the agent did | result = something that came to them
    model: Any
    time_col: str
    owner: Any = None                    # column holding the agent's id (default model.owner_id)
    joins: list = field(default_factory=list)  # (model, onclause)
    where: list = field(default_factory=list)
    describe: Optional[Callable[[Any], str]] = None  # one timeline line - never a client's details

    def owner_col(self):
        return self.owner if self.owner is not None else self.model.owner_id

    def time(self):
        return getattr(self.model, self.time_col)


def _street(addr: Optional[str]) -> str:
    s = (addr or "").split(",")[0].strip()
    if s.isupper():
        from app.services.tasks_service import _nice_case
        s = _nice_case(s)
    return s


_DESIGN = lambda t: (t or "design").replace("_", " ")

FEATURES: list[Feature] = [
    # ── open houses
    Feature("open_houses", "Open houses set up", "open_house", "action", m.OpenHouseEvent, "created_at", owner=m.OpenHouseEvent.agent_id,
            describe=lambda r: f"Set up an open house at {_street(r.address) or 'a home'}"),
    Feature("signins", "Open house sign-ins", "open_house", "result", m.OpenHouseVisitor, "created_at", owner=m.OpenHouseEvent.agent_id,
            joins=[(m.OpenHouseEvent, m.OpenHouseEvent.id == m.OpenHouseVisitor.open_house_event_id)],
            describe=lambda r: "Got an open house sign-in"),
    Feature("team_broadcasts", "Asked team to host", "open_house", "action", m.OpenHouseTeamBroadcast, "created_at",
            describe=lambda r: "Asked their team to host an open house"),
    # ── leads + follow-ups
    Feature("leads", "New leads", "leads", "result", m.Lead, "created_at", describe=lambda r: "Got a new lead"),
    Feature("form_responses", "Form responses", "leads", "result", m.FormSubmission, "created_at",
            describe=lambda r: f"Got a form response ({(r.form_type or 'form').replace('_', ' ')})"),
    Feature("showcases", "Showcases made", "leads", "result", m.Collection, "created_at",
            describe=lambda r: "A Showcase was made for a lead"),
    Feature("client_showcase", "Client Showcase activity", "leads", "result", m.LeadEvent, "occurred_at", owner=m.Lead.owner_id,
            joins=[(m.Lead, m.Lead.id == m.LeadEvent.lead_id)], where=[m.LeadEvent.source_type.like("showcase_%")],
            describe=lambda r: "A client used their Showcase"),
    Feature("leads_contacted", "Leads marked contacted", "follow_up", "action", m.Lead, "last_contacted_at",
            where=[m.Lead.last_contacted_at.isnot(None)], describe=lambda r: "Marked a lead contacted"),
    Feature("followups_done", "Follow-ups checked off", "follow_up", "action", m.AgentTask, "completed_at",
            where=[m.AgentTask.completed_at.isnot(None)], describe=lambda r: "Checked off a follow-up"),
    Feature("followups_added", "Own follow-ups added", "follow_up", "action", m.AgentTask, "created_at",
            where=[m.AgentTask.auto_source.is_(None)], describe=lambda r: "Added their own follow-up"),
    Feature("lead_notes", "Notes on leads", "follow_up", "action", m.LeadNote, "created_at", owner=m.LeadNote.author_id,
            describe=lambda r: "Wrote a note on a lead"),
    # ── marketing
    Feature("cma", "CMAs", "marketing", "action", m.CmaReport, "created_at", describe=lambda r: "Made a CMA"),
    Feature("studio", "Studio designs", "marketing", "action", m.DesignProject, "created_at",
            describe=lambda r: f"Made a {_DESIGN(r.design_type)}"),
    Feature("ai_media", "AI Media", "marketing", "action", m.StudioMediaAsset, "created_at", where=[m.StudioMediaAsset.kind.in_(("image", "video"))],
            describe=lambda r: f"Made an AI {r.kind}"),
    Feature("spotlights", "Spotlight sites", "marketing", "action", m.PropertyWebsite, "created_at", describe=lambda r: "Built a Spotlight site"),
    Feature("blogs", "Blog posts", "marketing", "action", m.Blog, "created_at", describe=lambda r: f"Started a blog post: {r.title or 'untitled'}"),
    Feature("campaigns", "Form share links", "marketing", "action", m.FormCampaign, "created_at", describe=lambda r: "Made a form share link"),
    Feature("hub_visits", "Agent Hub visits", "marketing", "result", m.AgentHubEvent, "created_at", where=[m.AgentHubEvent.event_type == "visit"],
            describe=lambda r: "Someone visited their Agent Hub"),
    Feature("reviews", "Reviews received", "marketing", "result", m.AgentReview, "created_at", where=[m.AgentReview.source == "direct"],
            describe=lambda r: f"Got a {r.rating}-star review"),
    # ── tools
    Feature("assistant", "AI assistant chats", "tools", "action", m.AIConversation, "created_at", describe=lambda r: "Asked the AI assistant"),
    Feature("net_sheets", "Seller net sheets", "tools", "action", m.SellerNetSheet, "created_at", describe=lambda r: "Made a seller net sheet"),
    Feature("buyer_cash", "Buyer cash sheets", "tools", "action", m.BuyerCashNeeded, "created_at", describe=lambda r: "Made a buyer cash-needed sheet"),
]
FEATURE_BY_KEY = {f.key: f for f in FEATURES}
ACTION_KEYS = [f.key for f in FEATURES if f.kind == "action"]

GROUPS = [
    {"key": "open_house", "label": "Open houses"}, {"key": "leads", "label": "Leads & Showcases"},
    {"key": "follow_up", "label": "Working their leads"}, {"key": "marketing", "label": "Marketing"},
    {"key": "tools", "label": "Tools"},
]

# Thresholds for the plain-English labels.
REGULAR_WEEKS = 3        # used in 3+ of the last 8 weeks -> "Keeps using"
RECENT_DAYS = 30
ONCE_GRACE_DAYS = 14     # used exactly once, more than 2 weeks ago -> "Tried once"


def feature_status(total: int, last_at: Optional[datetime], weeks_8: int, now: datetime) -> str:
    """never | regular (keeps using) | once (tried once, never again) | recent | stopped"""
    if not total:
        return "never"
    if weeks_8 >= REGULAR_WEEKS:
        return "regular"
    age = (now - last_at).days if last_at else 9999
    if total == 1 and age > ONCE_GRACE_DAYS:
        return "once"
    if age <= RECENT_DAYS:
        return "recent"
    return "stopped"


def health(last_seen: Optional[datetime], active_days_30: int, now: datetime) -> str:
    """power (in most weeks, lots of days) | active | slipping | inactive | never"""
    if last_seen is None:
        return "never"
    age = (now - last_seen).days
    if age <= 7 and active_days_30 >= 8:
        return "power"
    if age <= 7:
        return "active"
    if age <= 30:
        return "slipping"
    return "inactive"


def _admin_email() -> str:
    return os.getenv("ADMIN_EMAIL", "admin@openhousepal.com")


async def _agents(db: AsyncSession) -> list:
    return list((await db.execute(select(m.User).where(m.User.email != _admin_email()))).scalars().all())


async def _feature_stats(db: AsyncSession, f: Feature, now: datetime, agent_id: Optional[str] = None) -> dict[str, dict]:
    t = f.time()
    owner = f.owner_col()
    q = select(
        owner.label("uid"), func.count().label("total"),
        func.count().filter(t >= now - timedelta(days=30)).label("last_30"),
        func.min(t).label("first_at"), func.max(t).label("last_at"),
        func.count(distinct(func.date_trunc("week", t))).filter(t >= now - timedelta(weeks=8)).label("weeks_8"),
    ).select_from(f.model)
    for jm, on in f.joins:
        q = q.join(jm, on)
    q = q.where(t.isnot(None), *f.where)
    if agent_id:
        q = q.where(owner == agent_id)
    rows = (await db.execute(q.group_by(owner))).all()
    return {r.uid: {"total": r.total, "last_30": r.last_30, "first_at": r.first_at, "last_at": r.last_at, "weeks_8": r.weeks_8} for r in rows}


async def _active_days(db: AsyncSession, now: datetime, agent_id: Optional[str] = None) -> dict[str, int]:
    since = (now - timedelta(days=30)).date()
    q = select(m.AgentActiveDay.user_id, func.count()).where(m.AgentActiveDay.day >= since)
    if agent_id:
        q = q.where(m.AgentActiveDay.user_id == agent_id)
    return {uid: n for uid, n in (await db.execute(q.group_by(m.AgentActiveDay.user_id))).all()}


async def _open_house_stats(db: AsyncSession, agent_id: Optional[str] = None) -> dict[str, dict]:
    """Per agent: open houses, how many have already happened, how many of those got sign-ins."""
    visitors = (select(m.OpenHouseVisitor.open_house_event_id.label("oh"), func.count().label("n"))
                .group_by(m.OpenHouseVisitor.open_house_event_id).subquery())
    today = date.today()
    q = (select(m.OpenHouseEvent.agent_id, m.OpenHouseEvent.scheduled_date, m.OpenHouseEvent.created_at, func.coalesce(visitors.c.n, 0))
         .outerjoin(visitors, visitors.c.oh == m.OpenHouseEvent.id))
    if agent_id:
        q = q.where(m.OpenHouseEvent.agent_id == agent_id)
    out: dict[str, dict] = {}
    for uid, sched, created, n in (await db.execute(q)).all():
        s = out.setdefault(uid, {"total": 0, "held": 0, "held_with_signins": 0, "signins": 0})
        s["total"] += 1
        s["signins"] += n
        held = (sched or (created.date() if created else today)) <= today
        if held:
            s["held"] += 1
            if n:
                s["held_with_signins"] += 1
    return out


def _setup(u) -> dict[str, bool]:
    return {"crm": bool(u.crm_push_enabled or u.crm_fub_api_key or u.crm_webhook_url)}


async def _setup_rows(db: AsyncSession, agent_id: Optional[str] = None) -> dict[str, set]:
    """Which one-time setups each agent has done: IDX site published, Agent Hub, signature, calendar."""
    out: dict[str, set] = {}

    async def add(key, q):
        if agent_id:
            q = q.where(q.selected_columns[0] == agent_id)
        for (uid,) in (await db.execute(q)).all():
            out.setdefault(uid, set()).add(key)
    await add("idx_site", select(m.Site.owner_id).where(m.Site.published_at.isnot(None)).distinct())
    await add("agent_hub", select(m.AgentHub.owner_id).distinct())
    await add("email_signature", select(m.EmailSignature.owner_id).distinct())
    await add("calendar", select(m.CalendarConnection.owner_id).distinct())
    return out


SETUP_LABELS = {"idx_site": "IDX website live", "agent_hub": "Agent Hub", "email_signature": "Email signature",
                "calendar": "Calendar connected", "crm": "CRM connected"}


async def overview_and_agents(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    agents = await _agents(db)
    stats = {f.key: await _feature_stats(db, f, now) for f in FEATURES}
    days = await _active_days(db, now)
    ohs = await _open_house_stats(db)
    setups = await _setup_rows(db)

    rows = []
    for u in agents:
        feats = {k: stats[k].get(u.id) for k in stats}
        last_action = max((v["last_at"] for k, v in feats.items() if v and k in ACTION_KEYS and v["last_at"]), default=None)
        last_seen = u.last_seen_at or last_action
        using = [k for k in ACTION_KEYS if feats[k] and feats[k]["last_30"]]
        oh = ohs.get(u.id, {"total": 0, "held": 0, "held_with_signins": 0, "signins": 0})
        done = setups.get(u.id, set()) | {k for k, v in _setup(u).items() if v}
        rows.append({
            "id": u.id, "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email, "email": u.email,
            "brokerage": u.brokerage, "plan_tier": u.plan_tier, "subscription_status": u.subscription_status,
            "billing_group_id": u.billing_group_id, "has_paypal_subscription": bool(u.subscription_id),
            "joined_at": u.created_at, "last_seen_at": last_seen, "last_action_at": last_action,
            "active_days_30": days.get(u.id, 0), "health": health(last_seen, days.get(u.id, 0), now),
            "features_used_30": len(using),
            "top_features": sorted(using, key=lambda k: -feats[k]["last_30"])[:4],
            "tried_once": [k for k in ACTION_KEYS if feats[k] and feature_status(feats[k]["total"], feats[k]["last_at"], feats[k]["weeks_8"], now) == "once"],
            "leads_30": (feats["leads"] or {}).get("last_30", 0), "leads_total": (feats["leads"] or {}).get("total", 0),
            "open_houses": oh, "setup": sorted(done),
        })

    adoption = []
    for f in FEATURES:
        st = stats[f.key]
        statuses = [feature_status(v["total"], v["last_at"], v["weeks_8"], now) for uid, v in st.items() if uid in {a.id for a in agents}]
        adoption.append({
            "key": f.key, "label": f.label, "group": f.group, "kind": f.kind,
            "agents_ever": len(statuses), "agents_30": sum(1 for s in statuses if s in ("regular", "recent")),
            "regular": statuses.count("regular"), "once": statuses.count("once"), "stopped": statuses.count("stopped"),
            "uses_30": sum(v["last_30"] for uid, v in st.items()),
        })
    seen = [r["last_seen_at"] for r in rows]
    within = lambda d: sum(1 for s in seen if s and (now - s).days < d)
    all_oh = [r["open_houses"] for r in rows]
    return {
        "generated_at": now,
        "totals": {
            "agents": len(rows), "active_today": within(1), "active_7": within(7), "active_30": within(30),
            "paying": sum(1 for r in rows if (r["subscription_status"] or "").upper() == "ACTIVE"),
            "open_houses_held": sum(o["held"] for o in all_oh), "open_houses_with_signins": sum(o["held_with_signins"] for o in all_oh),
            "signins": sum(o["signins"] for o in all_oh), "leads_30": sum(r["leads_30"] for r in rows),
        },
        "groups": GROUPS, "adoption": adoption, "agents": rows, "setup_labels": SETUP_LABELS,
    }


async def agent_detail(db: AsyncSession, agent_id: str) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    u = await db.get(m.User, agent_id)
    if u is None or u.email == _admin_email():
        return None
    features = []
    for f in FEATURES:
        v = (await _feature_stats(db, f, now, agent_id)).get(agent_id)
        features.append({
            "key": f.key, "label": f.label, "group": f.group, "kind": f.kind,
            "total": v["total"] if v else 0, "last_30": v["last_30"] if v else 0,
            "first_at": v["first_at"] if v else None, "last_at": v["last_at"] if v else None,
            "weeks_8": v["weeks_8"] if v else 0,
            "status": feature_status(v["total"], v["last_at"], v["weeks_8"], now) if v else "never",
        })
    days = await _active_days(db, now, agent_id)
    last_action = max((x["last_at"] for x in features if x["kind"] == "action" and x["last_at"]), default=None)
    last_seen = u.last_seen_at or last_action

    # Their open houses, newest first, with how many signed in (counts only).
    visitors = (select(m.OpenHouseVisitor.open_house_event_id.label("oh"), func.count().label("n"))
                .group_by(m.OpenHouseVisitor.open_house_event_id).subquery())
    oh_rows = (await db.execute(
        select(m.OpenHouseEvent, func.coalesce(visitors.c.n, 0)).outerjoin(visitors, visitors.c.oh == m.OpenHouseEvent.id)
        .where(m.OpenHouseEvent.agent_id == agent_id)
        .order_by(func.coalesce(cast(m.OpenHouseEvent.scheduled_date, Date), cast(m.OpenHouseEvent.created_at, Date)).desc())
    )).all()
    today = date.today()
    open_houses = [{
        "id": oh.id, "address": _street(oh.address) or "Open house", "city": (oh.city or "").title() or None,
        "date": oh.scheduled_date or (oh.created_at.date() if oh.created_at else None),
        "held": (oh.scheduled_date or (oh.created_at.date() if oh.created_at else today)) <= today,
        "signins": n, "removed": bool(oh.is_deleted),
    } for oh, n in oh_rows]

    # Recent timeline: the latest of every kind, merged, newest first.
    timeline = []
    for f in FEATURES:
        if f.key == "client_showcase":
            continue  # summed below - one line per client click would drown everything else
        t = f.time()
        q = select(f.model).select_from(f.model)
        for jm, on in f.joins:
            q = q.join(jm, on)
        q = q.where(f.owner_col() == agent_id, t.isnot(None), *f.where).order_by(t.desc()).limit(60)
        for row in (await db.execute(q)).scalars().all():
            when = getattr(row, f.time_col)
            if f.key == "signins":
                oh = next((o for o in open_houses if o["id"] == row.open_house_event_id), None)
                label = f"Got an open house sign-in at {oh['address']}" if oh else f.describe(row)
            else:
                label = f.describe(row) if f.describe else f.label
            timeline.append({"at": when, "feature": f.key, "group": f.group, "kind": f.kind, "label": label})
    if any(x["key"] == "leads" and x["total"] for x in features):
        await _lead_origins(db, agent_id, timeline)
    timeline.sort(key=lambda e: e["at"], reverse=True)

    # Week by week (last 12): days in the app + things they did.
    weeks = []
    start = (now - timedelta(weeks=11)).date()
    start = start - timedelta(days=start.weekday())
    day_rows = (await db.execute(select(m.AgentActiveDay.day).where(m.AgentActiveDay.user_id == agent_id, m.AgentActiveDay.day >= start))).scalars().all()
    for i in range(12):
        ws = start + timedelta(weeks=i)
        we = ws + timedelta(days=7)
        weeks.append({
            "week_of": ws, "days_active": sum(1 for d in day_rows if ws <= d < we),
            "actions": sum(1 for e in timeline if e["kind"] == "action" and ws <= e["at"].date() < we),
            "results": sum(1 for e in timeline if e["kind"] == "result" and ws <= e["at"].date() < we),
        })

    return {
        "agent": {
            "id": u.id, "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email, "email": u.email,
            "brokerage": u.brokerage, "plan_tier": u.plan_tier, "subscription_status": u.subscription_status,
            "joined_at": u.created_at, "last_seen_at": last_seen, "active_days_30": days.get(agent_id, 0),
            "health": health(last_seen, days.get(agent_id, 0), now),
        },
        "groups": GROUPS, "features": features, "open_houses": open_houses, "weeks": weeks,
        "timeline": timeline[:400],
        "setup": sorted((await _setup_rows(db, agent_id)).get(agent_id, set()) | {k for k, v in _setup(u).items() if v}),
        "setup_labels": SETUP_LABELS,
    }


async def _lead_origins(db: AsyncSession, agent_id: str, timeline: list) -> None:
    """"Got a new lead" -> "Got a new lead from an open house sign-in" (the kind of first touch, never who)."""
    from app.api.lead_routes import _origin_group
    first = (select(m.LeadEvent.lead_id, func.min(m.LeadEvent.occurred_at).label("at"))
             .join(m.Lead, m.Lead.id == m.LeadEvent.lead_id).where(m.Lead.owner_id == agent_id)
             .group_by(m.LeadEvent.lead_id).subquery())
    rows = (await db.execute(
        select(m.LeadEvent, m.Lead.created_at).join(first, and_(first.c.lead_id == m.LeadEvent.lead_id, first.c.at == m.LeadEvent.occurred_at))
        .join(m.Lead, m.Lead.id == m.LeadEvent.lead_id)
    )).all()
    words = {"open_house": "an open house sign-in", "showcase": "a Showcase", "forms": "a form", "listings": "a Spotlight or listing page",
             "website": "their website or blog", "portal": "Zillow or another portal", "review": "a review", "other": "somewhere else"}
    by_created = {}
    for ev, created in rows:
        by_created.setdefault(created, words.get(_origin_group(ev), "somewhere else"))
    for e in timeline:
        if e["feature"] == "leads" and e["at"] in by_created:
            e["label"] = f"Got a new lead from {by_created[e['at']]}"
