from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
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


class MetricHistory(BaseModel):
    metric: str
    joined_at: str
    daily: List[DailyPoint]


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
            DailyPoint(date=d.isoformat(), count=round((fills.get(d, 0) / views[d]) * 100) if views.get(d) else 0)
            for d in days
        ]
    elif metric == "open_house_signin_completion":
        views = await stats_service.get_daily_counts(db, current_user.id, "open_house_signin_views", since, until)
        signins = await stats_service.get_daily_counts(db, current_user.id, "open_house_signins", since, until)
        days = sorted(set(views) | set(signins))
        daily = [
            DailyPoint(date=d.isoformat(), count=min(round((signins.get(d, 0) / views[d]) * 100), 100) if views.get(d) else 0)
            for d in days
        ]
    else:
        counts = await stats_service.get_daily_counts(db, current_user.id, metric, since, until)
        daily = [DailyPoint(date=d.isoformat(), count=n) for d, n in sorted(counts.items())]

    return MetricHistory(metric=metric, joined_at=since.date().isoformat(), daily=daily)
