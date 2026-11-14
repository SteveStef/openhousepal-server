from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AgentHubEvent, BlogEvent, FormSubmission, FormViewSession, Lead, OpenHouseEvent, OpenHouseVisitor, OpenHouseSignInView, User
from app.utils.own_traffic import not_owner_test

Metric = Literal["agent_hub_visits", "agent_hub_interactions", "qr_scans", "form_views", "form_fills", "blog_reads", "open_house_signins", "new_leads", "open_house_signin_views", "open_house_unrepresented"]

METRICS: List[Metric] = ["agent_hub_visits", "agent_hub_interactions", "qr_scans", "form_views", "form_fills", "blog_reads", "open_house_signins", "new_leads", "open_house_signin_views", "open_house_unrepresented"]

# Tallied as unique anonymous visitors (by browser-local visitor_id) rather than raw event counts,
# so a visitor reloading the page or clicking several buttons doesn't inflate the number.
UNIQUE_VISITOR_METRICS = {"agent_hub_visits", "agent_hub_interactions"}
# Daily charts count by the agent's own calendar day (Eastern, like the rest of the app) - a
# timestamp's UTC date put an evening sign-in on the next day.
EASTERN = ZoneInfo("America/New_York")


def local_day(ts: datetime) -> date:
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(EASTERN).date()
# A visitor "interacted" when they clicked anything on the hub OR saved the contact card (the vCard
# download is logged server-side, so it still counts if the click ping was cut off by the download).
_EVENT_TYPES_BY_UNIQUE_METRIC = {"agent_hub_visits": ("visit",), "agent_hub_interactions": ("interaction", "save_contact")}


def real_signin_views():
    """Sign-in page showings that aren't the host's own test: a Kiosk showing still counts when it's
    recorded (guests use the host's signed-in tablet), unless it turned into a sign-in flagged as
    the host's own (owner_test, e.g. they typed their own email). See utils/own_traffic."""
    tests = select(OpenHouseVisitor.id).where(OpenHouseVisitor.owner_test.is_(True))
    return or_(OpenHouseSignInView.visitor_id.is_(None), OpenHouseSignInView.visitor_id.notin_(tests))


def not_owner_lead(owner_id: str):
    """A lead made from the agent's own email (their test sign-in / form fill) isn't a new lead."""
    own_email = select(User.email).where(User.id == owner_id).scalar_subquery()
    return or_(Lead.email.is_(None), func.lower(Lead.email) != func.lower(own_email))


def _hub_visit_filter():
    # Opening the hub from its QR code logs a "qr_scan" instead of a "visit" - that person is still a
    # visitor (11/03: they were left out, so "Interacted" could top "Visitors"). Only the hub page's
    # scans carry a visitor_id; a form page's QR scan has none and isn't a hub visit.
    return or_(AgentHubEvent.event_type == "visit", and_(AgentHubEvent.event_type == "qr_scan", AgentHubEvent.visitor_id.is_not(None)))


async def _visitor_rows_for_metric(db: AsyncSession, owner_id: str, metric: Metric, since: datetime, until: datetime) -> List[tuple]:
    """(created_at, visitor_id) rows backing the unique-visitor metrics."""
    kind = _hub_visit_filter() if metric == "agent_hub_visits" else AgentHubEvent.event_type.in_(_EVENT_TYPES_BY_UNIQUE_METRIC[metric])
    stmt = select(AgentHubEvent.created_at, AgentHubEvent.visitor_id).where(
        AgentHubEvent.owner_id == owner_id, kind,
        AgentHubEvent.created_at >= since, AgentHubEvent.created_at <= until,
    )
    result = await db.execute(stmt)
    return result.all()


async def _timestamps_for_metric(db: AsyncSession, owner_id: str, metric: Metric, since: datetime, until: datetime) -> List[datetime]:
    if metric == "agent_hub_visits":
        stmt = select(AgentHubEvent.created_at).where(
            AgentHubEvent.owner_id == owner_id, _hub_visit_filter(),
            AgentHubEvent.created_at >= since, AgentHubEvent.created_at <= until,
        )
    elif metric == "agent_hub_interactions":
        stmt = select(AgentHubEvent.created_at).where(
            AgentHubEvent.owner_id == owner_id, AgentHubEvent.event_type.in_(("interaction", "save_contact")),
            AgentHubEvent.created_at >= since, AgentHubEvent.created_at <= until,
        )
    elif metric == "qr_scans":
        stmt = select(AgentHubEvent.created_at).where(
            AgentHubEvent.owner_id == owner_id, AgentHubEvent.event_type == "qr_scan",
            AgentHubEvent.created_at >= since, AgentHubEvent.created_at <= until,
        )
    elif metric == "form_views":
        # One per visitor session (reloads don't double count) - the same numbers the Forms tab's
        # per-form insights show, across Agent Hub and IDX form URLs alike.
        from app.services.forms_service import real_form_sessions
        stmt = select(FormViewSession.created_at).where(
            FormViewSession.owner_id == owner_id, real_form_sessions(),
            FormViewSession.created_at >= since, FormViewSession.created_at <= until,
        )
    elif metric == "form_fills":
        stmt = select(FormSubmission.created_at).where(
            FormSubmission.owner_id == owner_id, not_owner_test(FormSubmission.owner_test),
            FormSubmission.created_at >= since, FormSubmission.created_at <= until,
        )
    elif metric == "blog_reads":
        stmt = select(BlogEvent.created_at).where(
            BlogEvent.owner_id == owner_id, BlogEvent.event_type == "view",
            BlogEvent.created_at >= since, BlogEvent.created_at <= until,
        )
    elif metric == "open_house_signins":
        stmt = (
            select(OpenHouseVisitor.created_at)
            .join(OpenHouseEvent, OpenHouseVisitor.open_house_event_id == OpenHouseEvent.id)
            .where(
                OpenHouseEvent.agent_id == owner_id, not_owner_test(OpenHouseVisitor.owner_test),
                OpenHouseVisitor.created_at >= since, OpenHouseVisitor.created_at <= until,
            )
        )
    elif metric == "new_leads":
        stmt = select(Lead.created_at).where(
            Lead.owner_id == owner_id, not_owner_lead(owner_id),
            Lead.created_at >= since, Lead.created_at <= until,
        )
    elif metric == "open_house_signin_views":
        stmt = (
            select(OpenHouseSignInView.created_at)
            .join(OpenHouseEvent, OpenHouseSignInView.open_house_event_id == OpenHouseEvent.id)
            .where(
                OpenHouseEvent.agent_id == owner_id, real_signin_views(),
                OpenHouseSignInView.created_at >= since, OpenHouseSignInView.created_at <= until,
            )
        )
    elif metric == "open_house_unrepresented":
        stmt = (
            select(OpenHouseVisitor.created_at)
            .join(OpenHouseEvent, OpenHouseVisitor.open_house_event_id == OpenHouseEvent.id)
            .where(
                OpenHouseEvent.agent_id == owner_id, OpenHouseVisitor.has_agent == "NO", not_owner_test(OpenHouseVisitor.owner_test),
                OpenHouseVisitor.created_at >= since, OpenHouseVisitor.created_at <= until,
            )
        )
    else:
        raise ValueError(f"Unknown metric: {metric}")

    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


async def get_daily_counts(db: AsyncSession, owner_id: str, metric: Metric, since: datetime, until: datetime) -> Dict[date, int]:
    counts: Dict[date, int] = defaultdict(int)
    if metric in UNIQUE_VISITOR_METRICS:
        rows = await _visitor_rows_for_metric(db, owner_id, metric, since, until)
        seen_by_day: Dict[date, set] = defaultdict(set)
        for created_at, visitor_id in rows:
            day = local_day(created_at)
            # No visitor_id (older client, or one that didn't send it) - still count the visit
            # itself rather than silently dropping it, just can't be deduped against others.
            seen_by_day[day].add(visitor_id or created_at.isoformat())
        for day, visitors in seen_by_day.items():
            counts[day] = len(visitors)
        return counts

    timestamps = await _timestamps_for_metric(db, owner_id, metric, since, until)
    for ts in timestamps:
        counts[local_day(ts)] += 1
    return counts


def _percent_change(current: int, previous: int) -> int:
    if previous == 0:
        return 100 if current > 0 else 0
    return round(((current - previous) / previous) * 100)


async def get_summary(db: AsyncSession, owner_id: str) -> Dict[str, Dict[str, int]]:
    now = datetime.now(timezone.utc)
    # The last 30 Eastern calendar days (today included) vs the 30 before - the same days the stat
    # pop-up's 30D shows, so the card and the pop-up always agree (11/03: a rolling 30x24h window
    # pulled part of a 31st day into the card). Daily sums, the way the pop-up's bars add up, so a
    # hub visitor counts once a day there and here.
    today = local_day(now)
    spark_days = [today - timedelta(days=29 - i) for i in range(30)]
    previous_start, _ = day_bounds(today - timedelta(days=59), today)

    summary: Dict[str, Dict[str, int]] = {}
    for metric in METRICS:
        counts = await get_daily_counts(db, owner_id, metric, previous_start, now)
        current = sum(n for d, n in counts.items() if d >= spark_days[0])
        previous = sum(n for d, n in counts.items() if d < spark_days[0])
        spark = [counts.get(d, 0) for d in spark_days]
        summary[metric] = {"current": current, "previous": previous, "percent": _percent_change(current, previous), "spark": spark}

    views_current = summary["form_views"]["current"]
    views_previous = summary["form_views"]["previous"]
    fills_current = summary["form_fills"]["current"]
    fills_previous = summary["form_fills"]["previous"]
    # Capped at 100 - fills can outnumber views (a visitor submitting more than once, or views
    # undercounted before a past fix) which would otherwise push this over 100% completed.
    completion_current = min(round((fills_current / views_current) * 100), 100) if views_current else 0
    completion_previous = min(round((fills_previous / views_previous) * 100), 100) if views_previous else 0
    summary["form_completion"] = {
        "current": completion_current,
        "previous": completion_previous,
        "percent": _percent_change(completion_current, completion_previous),
    }

    signin_views_current = summary["open_house_signin_views"]["current"]
    signin_views_previous = summary["open_house_signin_views"]["previous"]
    signins_current = summary["open_house_signins"]["current"]
    signins_previous = summary["open_house_signins"]["previous"]
    # Capped at 100 for the same reason as form_completion above - a completed sign-in without
    # a matching tracked view (e.g. from before this tracking existed) would otherwise push a
    # window over 100%.
    signin_completion_current = min(round((signins_current / signin_views_current) * 100), 100) if signin_views_current else 0
    signin_completion_previous = min(round((signins_previous / signin_views_previous) * 100), 100) if signin_views_previous else 0
    summary["open_house_signin_completion"] = {
        "current": signin_completion_current,
        "previous": signin_completion_previous,
        "percent": _percent_change(signin_completion_current, signin_completion_previous),
    }

    return summary


# ---------------------------------------------------------------- what it was (11/03 stats rework)
# Under a stat's chart: which form, which post, which open house, where new leads came from - the
# thing an agent wants to know right after "how many". Agent Hub visits/taps and QR scans aren't
# recorded with what was tapped, so they have none (their pop-up shows the share of visitors instead).
_LEAD_ORIGIN_LABELS = {"open_house": "Open houses", "forms": "Forms & invites", "showcase": "Showcases", "listings": "Listing pages",
                       "website": "Your website", "portal": "Portals", "review": "Reviews", "other": "Other"}
BREAKDOWN_TITLES = {"form_views": "Forms people opened", "form_fills": "Forms people filled in", "form_completion": "Finish rate by form",
                    "blog_reads": "Most-read posts", "open_house_signins": "Sign-ins by open house",
                    "open_house_unrepresented": "Guests without an agent, by open house", "open_house_signin_views": "Sign-in page views by open house",
                    "open_house_signin_completion": "Sign-in rate by open house", "new_leads": "Where new leads came from"}


def day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """Eastern calendar days [start, end] as UTC datetimes."""
    from datetime import time as _t
    return (datetime.combine(start, _t.min, EASTERN).astimezone(timezone.utc),
            datetime.combine(end + timedelta(days=1), _t.min, EASTERN).astimezone(timezone.utc))


def _tidy(s) -> str:
    s = " ".join(str(s or "").split())
    return s.title() if s.isupper() else s


async def get_breakdown(db: AsyncSession, owner_id: str, metric: str, start: date, end: date, limit: int = 6) -> dict:
    from sqlalchemy import func
    from app.models.database import AgentForm, Blog, LeadEvent
    from app.services.forms_service import real_form_sessions
    since, until = day_bounds(start, end)
    rows: List[tuple] = []
    rate = {}
    if metric in ("form_views", "form_fills", "form_completion"):
        views = dict((await db.execute(
            select(AgentForm.title, func.count()).select_from(FormViewSession).join(AgentForm, AgentForm.id == FormViewSession.form_id)
            .where(FormViewSession.owner_id == owner_id, real_form_sessions(), FormViewSession.created_at >= since, FormViewSession.created_at < until)
            .group_by(AgentForm.title))).all())
        fills = dict((await db.execute(
            select(AgentForm.title, func.count()).select_from(FormSubmission).join(AgentForm, AgentForm.id == FormSubmission.form_id)
            .where(FormSubmission.owner_id == owner_id, not_owner_test(FormSubmission.owner_test), FormSubmission.created_at >= since, FormSubmission.created_at < until)
            .group_by(AgentForm.title))).all())
        if metric == "form_views":
            rows = list(views.items())
        elif metric == "form_fills":
            rows = list(fills.items())
        else:
            rows = [(t, min(round(fills.get(t, 0) / v * 100), 100)) for t, v in views.items() if v]
            rate = {t: (fills.get(t, 0), v) for t, v in views.items() if v}
    elif metric == "blog_reads":
        rows = (await db.execute(
            select(Blog.title, func.count()).select_from(BlogEvent).join(Blog, Blog.id == BlogEvent.blog_id)
            .where(BlogEvent.owner_id == owner_id, BlogEvent.event_type == "view", BlogEvent.created_at >= since, BlogEvent.created_at < until)
            .group_by(Blog.title))).all()
    elif metric in ("open_house_signins", "open_house_unrepresented", "open_house_signin_views", "open_house_signin_completion"):
        def visitors(unrep: bool = False):
            q = (select(OpenHouseEvent.address, func.count()).select_from(OpenHouseVisitor).join(OpenHouseEvent, OpenHouseVisitor.open_house_event_id == OpenHouseEvent.id)
                 .where(OpenHouseEvent.agent_id == owner_id, not_owner_test(OpenHouseVisitor.owner_test), OpenHouseVisitor.created_at >= since, OpenHouseVisitor.created_at < until))
            if unrep:
                q = q.where(OpenHouseVisitor.has_agent == "NO")
            return q.group_by(OpenHouseEvent.address)
        page_views = (select(OpenHouseEvent.address, func.count()).select_from(OpenHouseSignInView).join(OpenHouseEvent, OpenHouseSignInView.open_house_event_id == OpenHouseEvent.id)
                      .where(OpenHouseEvent.agent_id == owner_id, real_signin_views(), OpenHouseSignInView.created_at >= since, OpenHouseSignInView.created_at < until)
                      .group_by(OpenHouseEvent.address))
        if metric == "open_house_signin_views":
            rows = (await db.execute(page_views)).all()
        elif metric == "open_house_signin_completion":
            v, s = dict((await db.execute(page_views)).all()), dict((await db.execute(visitors())).all())
            rows = [(a, min(round(s.get(a, 0) / n * 100), 100)) for a, n in v.items() if n]
            rate = {a: (s.get(a, 0), n) for a, n in v.items() if n}
        else:
            rows = (await db.execute(visitors(metric == "open_house_unrepresented"))).all()
    elif metric == "new_leads":
        from app.api.lead_routes import _origin_group
        leads = (await db.execute(select(Lead.id).where(Lead.owner_id == owner_id, not_owner_lead(owner_id), Lead.created_at >= since, Lead.created_at < until))).scalars().all()
        groups: Dict[str, int] = defaultdict(int)
        if leads:
            evs = (await db.execute(select(LeadEvent).where(LeadEvent.lead_id.in_(leads)).order_by(LeadEvent.lead_id, LeadEvent.occurred_at))).scalars().all()
            first: Dict[str, LeadEvent] = {}
            for e in evs:
                first.setdefault(e.lead_id, e)
            for lid in leads:
                groups[_LEAD_ORIGIN_LABELS.get(_origin_group(first[lid]) if lid in first else "other", "Other")] += 1
        rows = list(groups.items())
    else:
        return {"title": None, "items": []}
    rate = {(_tidy(k) or "Untitled"): v for k, v in rate.items()}
    merged: Dict[str, int] = defaultdict(int)
    for label, n in rows:
        merged[_tidy(label) or "Untitled"] += int(n or 0)
    items = sorted(({"label": k, "count": v, **({"num": rate[k][0], "den": rate[k][1]} if k in rate else {})} for k, v in merged.items() if v or rate),
                   key=lambda i: (-(i.get("den") or i["count"]), i["label"]))
    if metric in ("form_completion", "open_house_signin_completion"):
        items = [{**i, "num": rate.get(i["label"], (0, 0))[0], "den": rate.get(i["label"], (0, 0))[1]} for i in items]
    return {"title": BREAKDOWN_TITLES.get(metric), "items": items[:limit], "more": max(0, len(items) - limit)}
