from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.database import User
from app.services import stats_service
from app.utils.auth import get_current_active_user

router = APIRouter(prefix="/stats", tags=["stats"])

ALL_METRICS = set(stats_service.METRICS) | {"form_completion", "open_house_signin_completion"}


class DailyPoint(BaseModel):
    date: str
    count: int
    # Rates (form / sign-in completion): the day's finished and viewed counts, so a range's rate is
    # sum(num) / sum(den), never an average of daily percents; capped at 100 only on the total - a sign-in
    # on a day its page view landed the day before still counts (11/03)
    num: Optional[int] = None
    den: Optional[int] = None


class Companion(BaseModel):
    """The number a stat is best read against - Agent Hub taps vs visitors, forms filled vs opened."""
    metric: str
    label: str
    daily: List[DailyPoint]


class MetricHistory(BaseModel):
    metric: str
    joined_at: str
    daily: List[DailyPoint]
    companion: Optional[Companion] = None


# stat -> (the stat it's read against, what those are called)
COMPANIONS = {"agent_hub_interactions": ("agent_hub_visits", "visitors"), "form_fills": ("form_views", "people who opened a form"),
              "open_house_signins": ("open_house_signin_views", "sign-in page views"), "open_house_unrepresented": ("open_house_signins", "sign-ins")}


@router.get("/summary")
async def get_stats_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    return await stats_service.get_summary(db, current_user.id)


@router.get("/{metric}/history", response_model=MetricHistory)
async def get_stats_history(
    metric: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    if metric not in ALL_METRICS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown metric.")

    since = current_user.created_at
    until = datetime.now(timezone.utc)

    if metric == "form_completion":
        views = await stats_service.get_daily_counts(db, current_user.id, "form_views", since, until)
        fills = await stats_service.get_daily_counts(db, current_user.id, "form_fills", since, until)
        days = sorted(set(views) | set(fills))
        daily = [
            DailyPoint(date=d.isoformat(), count=min(round((fills.get(d, 0) / views[d]) * 100), 100) if views.get(d) else 0,
                       num=fills.get(d, 0), den=views.get(d, 0))
            for d in days
        ]
    elif metric == "open_house_signin_completion":
        views = await stats_service.get_daily_counts(db, current_user.id, "open_house_signin_views", since, until)
        signins = await stats_service.get_daily_counts(db, current_user.id, "open_house_signins", since, until)
        days = sorted(set(views) | set(signins))
        daily = [
            DailyPoint(date=d.isoformat(), count=min(round((signins.get(d, 0) / views[d]) * 100), 100) if views.get(d) else 0,
                       num=signins.get(d, 0), den=views.get(d, 0))
            for d in days
        ]
    else:
        counts = await stats_service.get_daily_counts(db, current_user.id, metric, since, until)
        daily = [DailyPoint(date=d.isoformat(), count=n) for d, n in sorted(counts.items())]

    companion = None
    if metric in COMPANIONS:
        other, words = COMPANIONS[metric]
        counts = await stats_service.get_daily_counts(db, current_user.id, other, since, until)
        companion = Companion(metric=other, label=words, daily=[DailyPoint(date=d.isoformat(), count=n) for d, n in sorted(counts.items())])

    return MetricHistory(metric=metric, joined_at=stats_service.local_day(since).isoformat(), daily=daily, companion=companion)


@router.get("/{metric}/breakdown")
async def get_stats_breakdown(
    metric: str,
    start: date = Query(...),
    end: date = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """What the stat was made of in [start, end] (Eastern days): which forms, posts, open houses, lead sources."""
    if metric not in ALL_METRICS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown metric.")
    if end < start or (end - start).days > 3660:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pick a start before the end.")
    # from when they joined, like the chart - otherwise imported / older rows made the parts add up to more than the total
    start = max(start, stats_service.local_day(current_user.created_at))
    if end < start:
        return {"title": None, "items": []}
    return await stats_service.get_breakdown(db, current_user.id, metric, start, end)
