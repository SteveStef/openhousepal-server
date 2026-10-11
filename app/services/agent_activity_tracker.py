"""Remembers when each agent was last in the app and which days they used it - for the admin
activity tracker. Called on every signed-in request; writes at most once every few minutes per
agent (in memory per server process), on its own session in the background, so it never slows a
request down or breaks one."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TOUCH_EVERY_SECONDS = 300
EASTERN = ZoneInfo("America/New_York")
_last_touch: dict[str, datetime] = {}
_pending: set = set()


def touch(user_id: str) -> None:
    now = datetime.now(timezone.utc)
    last = _last_touch.get(user_id)
    if last is not None and (now - last).total_seconds() < TOUCH_EVERY_SECONDS:
        return
    _last_touch[user_id] = now
    try:
        task = asyncio.get_running_loop().create_task(_write(user_id, now))
    except RuntimeError:
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _write(user_id: str, now: datetime) -> None:
    try:
        from sqlalchemy import update
        from sqlalchemy.dialects.postgresql import insert
        from app.database import AsyncSessionLocal
        from app.models.database import AgentActiveDay, User
        async with AsyncSessionLocal() as db:
            await db.execute(update(User).where(User.id == user_id).values(last_seen_at=now))
            await db.execute(insert(AgentActiveDay).values(user_id=user_id, day=now.astimezone(EASTERN).date())
                             .on_conflict_do_nothing(index_elements=["user_id", "day"]))
            await db.commit()
    except Exception as e:  # never let tracking break anything
        logger.warning(f"activity touch failed for {user_id}: {e}")
