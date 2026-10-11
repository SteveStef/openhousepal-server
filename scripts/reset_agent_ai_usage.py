"""One-off dev utility: reset an agent's AI usage so testing isn't blocked by real budget
gates. Deletes this month's ai_usage_events for the named owner (their personal monthly
budget, see ai_rate_limit_service.total_monthly_ai_cost) and zeroes today's/this month's
usage_budget_counters rows for the CMA global buckets (the platform-wide daily/monthly
ceiling, see ai_rate_limit_service.reserve_cma_analysis/reserve_cma_reasoning).

Usage: python -m scripts.reset_agent_ai_usage "Peter Stefanatos"
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import select, delete, or_
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.models.database import User, AIUsageEvent, UsageBudgetCounter
from app.database import DATABASE_URL

CMA_BUCKETS = ("cma_ai_global", "cma_reasoning_global")


def _start_of_month() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def reset_usage(name_query: str):
    engine = create_async_engine(DATABASE_URL)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with AsyncSessionLocal() as db:
        name_parts = name_query.split()
        name_filters = [
            (User.first_name + " " + User.last_name).ilike(f"%{name_query}%"),
            User.email.ilike(f"%{name_query}%"),
        ]
        for part in name_parts:
            name_filters.append(User.first_name.ilike(f"%{part}%"))
            name_filters.append(User.last_name.ilike(f"%{part}%"))
        result = await db.execute(select(User).where(or_(*name_filters)))
        users = result.scalars().all()
        if not users:
            print(f"No user matched '{name_query}'.")
            return
        if len(users) > 1:
            print(f"Multiple users matched '{name_query}':")
            for u in users:
                print(f"  {u.id}  {u.first_name} {u.last_name}  {u.email}")
            print("Refine the query.")
            return

        user = users[0]
        print(f"Resetting AI usage for {user.first_name} {user.last_name} <{user.email}> ({user.id})")

        month_start = _start_of_month()
        del_result = await db.execute(
            delete(AIUsageEvent).where(
                AIUsageEvent.owner_id == user.id,
                AIUsageEvent.created_at >= month_start,
            )
        )
        print(f"  Deleted {del_result.rowcount} ai_usage_events row(s) since {month_start.date()}.")

        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")
        counters_result = await db.execute(
            select(UsageBudgetCounter).where(
                UsageBudgetCounter.bucket.in_(CMA_BUCKETS),
                or_(
                    (UsageBudgetCounter.period_type == "day") & (UsageBudgetCounter.period_key == today_key),
                    (UsageBudgetCounter.period_type == "month") & (UsageBudgetCounter.period_key == month_key),
                ),
            )
        )
        counters = counters_result.scalars().all()
        for c in counters:
            print(f"  Zeroing {c.bucket}/{c.period_type}/{c.period_key} (was count={c.count}, cost_micros={c.cost_micros})")
            c.count = 0
            c.cost_micros = 0

        await db.commit()
        print("Done.")

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python -m scripts.reset_agent_ai_usage "Peter Stefanatos"')
    else:
        asyncio.run(reset_usage(sys.argv[1]))
