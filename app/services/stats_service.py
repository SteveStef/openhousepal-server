from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AgentHubEvent, BlogEvent, FormSubmission, FormViewSession, Lead, OpenHouseEvent, OpenHouseVisitor, OpenHouseSignInView

Metric = Literal["agent_hub_visits", "agent_hub_interactions", "qr_scans", "form_views", "form_fills", "blog_reads", "open_house_signins", "new_leads", "open_house_signin_views", "open_house_unrepresented"]

METRICS: List[Metric] = ["agent_hub_visits", "agent_hub_interactions", "qr_scans", "form_views", "form_fills", "blog_reads", "open_house_signins", "new_leads", "open_house_signin_views", "open_house_unrepresented"]

# Tallied as unique anonymous visitors (by browser-local visitor_id) rather than raw event counts,
# so a visitor reloading the page or clicking several buttons doesn't inflate the number.
UNIQUE_VISITOR_METRICS = {"agent_hub_visits", "agent_hub_interactions"}
# A visitor "interacted" when they clicked anything on the hub OR saved the contact card (the vCard
# download is logged server-side, so it still counts if the click ping was cut off by the download).
_EVENT_TYPES_BY_UNIQUE_METRIC = {"agent_hub_visits": ("visit",), "agent_hub_interactions": ("interaction", "save_contact")}


async def _visitor_rows_for_metric(db: AsyncSession, owner_id: str, metric: Metric, since: datetime, until: datetime) -> List[tuple]:
    """(created_at, visitor_id) rows backing the unique-visitor metrics."""
    stmt = select(AgentHubEvent.created_at, AgentHubEvent.visitor_id).where(
        AgentHubEvent.owner_id == owner_id, AgentHubEvent.event_type.in_(_EVENT_TYPES_BY_UNIQUE_METRIC[metric]),
        AgentHubEvent.created_at >= since, AgentHubEvent.created_at <= until,
    )
    result = await db.execute(stmt)
    return result.all()


async def _timestamps_for_metric(db: AsyncSession, owner_id: str, metric: Metric, since: datetime, until: datetime) -> List[datetime]:
    if metric == "agent_hub_visits":
        stmt = select(AgentHubEvent.created_at).where(
            AgentHubEvent.owner_id == owner_id, AgentHubEvent.event_type == "visit",
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
        stmt = select(FormViewSession.created_at).where(
            FormViewSession.owner_id == owner_id,
            FormViewSession.created_at >= since, FormViewSession.created_at <= until,
        )
    elif metric == "form_fills":
        stmt = select(FormSubmission.created_at).where(
            FormSubmission.owner_id == owner_id,
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
                OpenHouseEvent.agent_id == owner_id,
                OpenHouseVisitor.created_at >= since, OpenHouseVisitor.created_at <= until,
            )
        )
    elif metric == "new_leads":
        stmt = select(Lead.created_at).where(
            Lead.owner_id == owner_id,
            Lead.created_at >= since, Lead.created_at <= until,
        )
    elif metric == "open_house_signin_views":
        stmt = (
            select(OpenHouseSignInView.created_at)
            .join(OpenHouseEvent, OpenHouseSignInView.open_house_event_id == OpenHouseEvent.id)
            .where(
                OpenHouseEvent.agent_id == owner_id,
                OpenHouseSignInView.created_at >= since, OpenHouseSignInView.created_at <= until,
            )
        )
    elif metric == "open_house_unrepresented":
        stmt = (
            select(OpenHouseVisitor.created_at)
            .join(OpenHouseEvent, OpenHouseVisitor.open_house_event_id == OpenHouseEvent.id)
            .where(
                OpenHouseEvent.agent_id == owner_id, OpenHouseVisitor.has_agent == "NO",
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
            day = created_at.date()
            # No visitor_id (older client, or one that didn't send it) - still count the visit
            # itself rather than silently dropping it, just can't be deduped against others.
            seen_by_day[day].add(visitor_id or created_at.isoformat())
        for day, visitors in seen_by_day.items():
            counts[day] = len(visitors)
        return counts

    timestamps = await _timestamps_for_metric(db, owner_id, metric, since, until)
    for ts in timestamps:
        counts[ts.date()] += 1
    return counts


def _percent_change(current: int, previous: int) -> int:
    if previous == 0:
        return 100 if current > 0 else 0
    return round(((current - previous) / previous) * 100)


async def get_summary(db: AsyncSession, owner_id: str) -> Dict[str, Dict[str, int]]:
    now = datetime.now(timezone.utc)
    current_start = now - timedelta(days=30)
    previous_start = now - timedelta(days=60)

    summary: Dict[str, Dict[str, int]] = {}
    for metric in METRICS:
        if metric in UNIQUE_VISITOR_METRICS:
            rows = await _visitor_rows_for_metric(db, owner_id, metric, previous_start, now)
            current_visitors = {visitor_id or created_at.isoformat() for created_at, visitor_id in rows if created_at >= current_start}
            previous_visitors = {visitor_id or created_at.isoformat() for created_at, visitor_id in rows if previous_start <= created_at < current_start}
            current, previous = len(current_visitors), len(previous_visitors)
        else:
            timestamps = await _timestamps_for_metric(db, owner_id, metric, previous_start, now)
            current = sum(1 for ts in timestamps if ts >= current_start)
            previous = sum(1 for ts in timestamps if previous_start <= ts < current_start)
        summary[metric] = {"current": current, "previous": previous, "percent": _percent_change(current, previous)}

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
