"""Shared, provider-agnostic spend/count budget reservation - the mechanism behind every
day/month ceiling on a metered external API call (Google Places, and the platform-wide global
layer on top of Anthropic/OpenAI usage in ai_rate_limit_service.py). Backed by
UsageBudgetCounter (one row per bucket+period), so limits are correct across a backend restart
and across multiple worker processes - unlike an in-process Counter.

Two-phase reserve/reconcile, not a single read-then-write check:
  1. reserve() atomically increments a bucket's day AND month rows by a cost estimate, but only
     if doing so would not exceed either limit - a single guarded UPDATE per row (see
     reserve_bucket), not a separate SELECT-then-INSERT, so concurrent requests across workers
     genuinely cannot both pass at once. All buckets/periods in one reserve() call share one DB
     transaction: if any of them would exceed its limit, nothing is committed - full rollback,
     not a partial reservation.
  2. reconcile() corrects the reservation once the real cost is known (exact for Places, where
     the cost is a fixed known constant; a trued-up estimate for AI calls, where the real token
     cost isn't known until the model responds) - or fully refunds it (actual_cost_usd=0.0) if
     the call never actually happened (blocked upstream, or failed after budget was reserved).

cost_micros is an integer (1,000,000 micros = $1.00), never a float, so a day of repeated
increments can't accumulate floating-point drift in a number actively gating spend.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import UsageBudgetCounter

logger = get_logger(__name__)

MICROS_PER_USD = 1_000_000


def usd_to_micros(usd: float) -> int:
    return int(round(usd * MICROS_PER_USD))


def micros_to_usd(micros: int) -> float:
    return micros / MICROS_PER_USD


class BudgetExceeded(Exception):
    """Raised by reserve() when any bucket/period in the request would exceed its limit.
    Nothing is reserved when this is raised - the whole attempt rolls back atomically."""

    def __init__(self, bucket: str, period_type: str):
        self.bucket = bucket
        self.period_type = period_type
        super().__init__(f"Budget exceeded for bucket={bucket!r} period={period_type!r}")


@dataclass
class Reservation:
    buckets: list[str]
    cost_micros: int
    day_key: str
    month_key: str


def _period_keys(now: Optional[datetime] = None) -> tuple[str, str]:
    current = now or datetime.now(timezone.utc)
    return current.date().isoformat(), current.strftime("%Y-%m")


async def reserve_bucket(
    db: AsyncSession, bucket: str, period_type: str, period_key: str,
    count_delta: int, cost_delta_micros: int,
    count_limit: Optional[int], cost_limit_micros: Optional[int],
) -> bool:
    """Atomically reserves (count_delta, cost_delta_micros) against one (bucket, period) row,
    only if the result would stay within count_limit and cost_limit_micros (a None limit is
    never checked). Returns True if reserved. Caller owns the transaction - does not commit."""
    insert_stmt = pg_insert(UsageBudgetCounter).values(
        id=str(uuid.uuid4()), bucket=bucket, period_type=period_type, period_key=period_key,
        count=0, cost_micros=0,
    ).on_conflict_do_nothing(index_elements=["bucket", "period_type", "period_key"])
    await db.execute(insert_stmt)

    conditions = [
        UsageBudgetCounter.bucket == bucket,
        UsageBudgetCounter.period_type == period_type,
        UsageBudgetCounter.period_key == period_key,
    ]
    if count_limit is not None:
        conditions.append(UsageBudgetCounter.count + count_delta <= count_limit)
    if cost_limit_micros is not None:
        conditions.append(UsageBudgetCounter.cost_micros + cost_delta_micros <= cost_limit_micros)

    update_stmt = (
        update(UsageBudgetCounter)
        .where(*conditions)
        .values(count=UsageBudgetCounter.count + count_delta, cost_micros=UsageBudgetCounter.cost_micros + cost_delta_micros, updated_at=func.now())
        .returning(UsageBudgetCounter.count)
    )
    result = await db.execute(update_stmt)
    return result.first() is not None


# One bucket's limits: (daily_count_limit, monthly_count_limit, daily_cost_limit_usd, monthly_cost_limit_usd)
BucketLimits = tuple[Optional[int], Optional[int], Optional[float], Optional[float]]


async def reserve(
    db: AsyncSession, buckets: dict[str, BucketLimits], cost_usd: float, count_delta: int = 0,
) -> Reservation:
    """Reserves cost_usd (and optionally count_delta) against every bucket in `buckets`, each
    checked against its own daily+monthly limits, all atomically in one transaction. Raises
    BudgetExceeded (nothing reserved, full rollback) the moment any single bucket/period would
    exceed its limit - so a multi-bucket reservation (e.g. a feature bucket plus the platform
    emergency bucket) is genuinely all-or-nothing."""
    day_key, month_key = _period_keys()
    cost_micros = usd_to_micros(cost_usd)

    try:
        for bucket, (daily_count_limit, monthly_count_limit, daily_cost_limit_usd, monthly_cost_limit_usd) in buckets.items():
            daily_cost_limit_micros = usd_to_micros(daily_cost_limit_usd) if daily_cost_limit_usd is not None else None
            monthly_cost_limit_micros = usd_to_micros(monthly_cost_limit_usd) if monthly_cost_limit_usd is not None else None

            if not await reserve_bucket(db, bucket, "day", day_key, count_delta, cost_micros, daily_count_limit, daily_cost_limit_micros):
                await db.rollback()
                raise BudgetExceeded(bucket, "day")
            if not await reserve_bucket(db, bucket, "month", month_key, count_delta, cost_micros, monthly_count_limit, monthly_cost_limit_micros):
                await db.rollback()
                raise BudgetExceeded(bucket, "month")
        await db.commit()
    except BudgetExceeded:
        raise

    return Reservation(buckets=list(buckets.keys()), cost_micros=cost_micros, day_key=day_key, month_key=month_key)


async def reconcile(db: AsyncSession, reservation: Reservation, actual_cost_usd: float) -> None:
    """Corrects every bucket a prior reserve() touched by (actual - reserved) - a plain
    unconditional atomic increment, no limit re-check (the cap was already enforced at
    reservation time; this only keeps the running total accurate). Pass actual_cost_usd=0.0 to
    fully refund a reservation whose call never actually happened. Never raises - a best-effort
    correction, not a second gate; a failure here just means a stale-but-safe (over-reserved,
    never under-reserved) running total until the next reservation nudges it."""
    delta_micros = usd_to_micros(actual_cost_usd) - reservation.cost_micros
    if delta_micros == 0:
        return
    try:
        for bucket in reservation.buckets:
            for period_type, period_key in (("day", reservation.day_key), ("month", reservation.month_key)):
                await db.execute(
                    update(UsageBudgetCounter)
                    .where(
                        UsageBudgetCounter.bucket == bucket,
                        UsageBudgetCounter.period_type == period_type,
                        UsageBudgetCounter.period_key == period_key,
                    )
                    .values(cost_micros=UsageBudgetCounter.cost_micros + delta_micros, updated_at=func.now())
                )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.warning("Usage budget reconciliation failed", extra={"buckets": reservation.buckets, "error": str(exc)})


async def add_spend(db: AsyncSession, buckets: list[str], cost_usd: float) -> None:
    """Adds money already spent to today's/this month's running totals, no limit check - for a
    follow-up call made after the reservation was settled (the CMA's comp-note rewrites), which
    used to be on the AI cost record but missing from these totals. Never raises."""
    cost_micros = usd_to_micros(cost_usd)
    if cost_micros <= 0:
        return
    day_key, month_key = _period_keys()
    try:
        for bucket in buckets:
            for period_type, period_key in (("day", day_key), ("month", month_key)):
                await reserve_bucket(db, bucket, period_type, period_key, 0, cost_micros, None, None)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.warning("Usage budget add_spend failed", extra={"buckets": buckets, "error": str(exc)})


async def usage_snapshot(db: AsyncSession, bucket: str, now: Optional[datetime] = None) -> dict:
    """Current day+month count/cost for one bucket - for admin reporting only, not used for
    any gating decision (reserve() is the sole source of truth for that)."""
    day_key, month_key = _period_keys(now)
    rows = (
        await db.execute(
            select(UsageBudgetCounter.period_type, UsageBudgetCounter.count, UsageBudgetCounter.cost_micros)
            .where(
                UsageBudgetCounter.bucket == bucket,
                UsageBudgetCounter.period_type.in_(["day", "month"]),
                UsageBudgetCounter.period_key.in_([day_key, month_key]),
            )
        )
    ).all()
    by_period = {period_type: (count, cost_micros) for period_type, count, cost_micros in rows}
    day_count, day_cost = by_period.get("day", (0, 0))
    month_count, month_cost = by_period.get("month", (0, 0))
    return {
        "bucket": bucket,
        "day_count": day_count,
        "day_cost_usd": micros_to_usd(day_cost),
        "month_count": month_count,
        "month_cost_usd": micros_to_usd(month_cost),
    }
