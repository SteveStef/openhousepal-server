"""Read-only dev utility: inspect the most recent CMA report for a named agent - status,
QA blocking/flags, and the AI cost incurred generating it (from ai_usage_events).

Usage: python -m scripts.inspect_latest_cma "Peter Stefanatos"
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.models.database import User, CmaReport, AIUsageEvent
from app.database import DATABASE_URL

CMA_EVENT_TYPES = ("cma_ai", "cma_ai_recompute", "cma_reasoning")


async def inspect(name_query: str):
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
        if len(users) != 1:
            print(f"Expected exactly 1 user match for '{name_query}', got {len(users)}.")
            return
        user = users[0]

        result = await db.execute(
            select(CmaReport)
            .where(CmaReport.owner_id == user.id)
            .order_by(CmaReport.created_at.desc())
            .limit(1)
        )
        report = result.scalar_one_or_none()
        if not report:
            print("No CMA reports found for this user.")
            return

        print(f"Report {report.id}  status={report.status}  created={report.created_at}  updated={report.updated_at}")
        subject = report.subject or {}
        print(f"Subject: {subject.get('street_address')}, {subject.get('city')}, {subject.get('state')}")

        estimate = (report.pricing or {}).get("ai_estimate") or {}
        qa_result = estimate.get("qa_result") or {}
        qa_blocking = estimate.get("qa_blocking") or []
        qa_flags = estimate.get("qa_flags") or []
        blocking_errors = qa_result.get("blocking_errors") or []

        print(f"\nqa_result keys: {list(qa_result.keys())}")
        print(f"qa_flags ({len(qa_flags)}):")
        for f in qa_flags:
            print(f"  - {f}")
        print(f"qa_blocking ({len(qa_blocking)}):")
        for b in qa_blocking:
            print(f"  - {b}")
        print(f"qa_result.blocking_errors ({len(blocking_errors)}):")
        for b in blocking_errors:
            print(f"  - {b}")

        ai_analysis = report.ai_analysis or {}
        print(f"\nai_analysis metadata: {json.dumps(ai_analysis, default=str)[:2000]}")

        # Cost: usage events around this report's creation time, best-effort window.
        window_start = report.created_at
        result = await db.execute(
            select(AIUsageEvent)
            .where(
                AIUsageEvent.owner_id == user.id,
                AIUsageEvent.event_type.in_(CMA_EVENT_TYPES),
                AIUsageEvent.created_at >= window_start,
            )
            .order_by(AIUsageEvent.created_at.asc())
        )
        events = result.scalars().all()
        total = sum(e.estimated_cost_usd or 0 for e in events)
        print(f"\nAI usage events since report creation ({len(events)}), total ${total:.4f}:")
        for e in events:
            print(f"  {e.created_at}  {e.event_type}  model={e.model}  cost=${(e.estimated_cost_usd or 0):.4f}  blocked={e.blocked}  reason={e.block_reason}")

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python -m scripts.inspect_latest_cma "Peter Stefanatos"')
    else:
        asyncio.run(inspect(sys.argv[1]))
