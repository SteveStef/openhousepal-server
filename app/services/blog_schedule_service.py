"""Scheduled articles go live at their time.

"Schedule it" on Review & Publish (and the AI helper's "schedule my post") saves an article as SCHEDULED with the
time the agent picked. Until 11/16 nothing ever acted on that time: every public page shows PUBLISHED articles only,
so a scheduled article stayed "Scheduled" for good. Found in the full editor test before launch - no agent had
scheduled one yet. One job, every minute (main.py), publishes what is due.

One UPDATE both finds and changes the rows, so several copies of the server running the job in the same minute
publish an article exactly once.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.logging import get_logger
from app.models.database import Blog

logger = get_logger(__name__)


def due_statement(now: datetime):
    """The one statement: every scheduled article whose time has come becomes published.
    Its date for readers is the time the agent chose; an article that was live before keeps its first date - the
    same rule as the Publish button (blog_routes.update_blog_status)."""
    return (
        update(Blog)
        .where(Blog.status == "SCHEDULED", Blog.scheduled_at.is_not(None), Blog.scheduled_at <= now)
        .values(status="PUBLISHED", published_at=func.coalesce(Blog.published_at, Blog.scheduled_at), scheduled_at=None)
        .returning(Blog.id, Blog.owner_id, Blog.slug, Blog.title)
        .execution_options(synchronize_session=False)
    )


async def publish_due(db: AsyncSession, now: Optional[datetime] = None, statement=None) -> list[dict]:
    """Publish what is due and say what went live. Commits."""
    rows = (await db.execute(statement if statement is not None else due_statement(now or datetime.now(timezone.utc)))).all()
    await db.commit()
    return [{"id": r.id, "owner_id": r.owner_id, "slug": r.slug, "title": r.title} for r in rows]


async def scheduled_publish_due() -> int:
    """The every-minute job (main.py). Its own session; a failure is logged and tried again a minute later."""
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            live = await publish_due(db)
    except Exception:  # noqa: BLE001
        logger.exception("publishing scheduled articles failed")
        return 0
    for b in live:
        logger.info("scheduled article published", extra={"event": "blog_scheduled_published", "blog_id": b["id"], "owner_id": b["owner_id"], "slug": b["slug"]})
    return len(live)
