"""Warnings for the OpenHousePal admin - never for agents.

When something fails for a reason only we can fix (an AI provider out of credit, a key missing, a
provider down, a setting not switched on), the agent just sees a calm "not available right now"
message (user, 10/15: agents must never see why - e.g. that OpenHousePal ran out of credit with a
provider). The real reason comes here instead:

- a bell notification on the admin account (ADMIN_EMAIL - the same account require_admin_user
  checks), type ADMIN_WARNING,
- the Discord channel when DISCORD_WEBHOOK_URL is set,
- an error line in the log.

The same warning repeats at most once every THROTTLE so a broken provider can't flood the bell.
Never raises.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

THROTTLE = 30 * 60
_last: dict[tuple[str, str], float] = {}

# What an agent sees when the real reason is ours to fix.
AGENT_MESSAGE = "This isn't available right now - please try again in a little while. We've been notified."


async def alert_admin(area: str, title: str, detail: str = "", *, owner_id: Optional[str] = None, path: Optional[str] = None) -> None:
    key = (area, title)
    now = time.monotonic()
    if now - _last.get(key, -THROTTLE * 2) < THROTTLE:
        return
    _last[key] = now
    logger.error("admin alert", extra={"area": area, "title": title, "detail": (detail or "")[:500], "owner_id": owner_id, "path": path})
    try:
        from sqlalchemy import select
        from app.database import AsyncSessionLocal
        from app.models.database import Notification, User
        async with AsyncSessionLocal() as db:
            admin_id = (await db.execute(select(User.id).where(User.email == os.getenv("ADMIN_EMAIL", "admin@openhousepal.com")))).scalar_one_or_none()
            who = ""
            if owner_id:
                who = (await db.execute(select(User.email).where(User.id == owner_id))).scalar_one_or_none() or ""
            if admin_id:
                bits = [b for b in [(detail or "").strip()[:600], f"Agent: {who}" if who else "", f"Where: {path}" if path else ""] if b]
                db.add(Notification(agent_id=admin_id, type="ADMIN_WARNING", reference_type="SYSTEM", reference_id=area[:60],
                                    title=f"Warning: {title}"[:200], message=" · ".join(bits) or title))
                await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("admin alert not saved", exc_info=True)
    try:
        from app.services.discord_notifier import notifier
        notifier.send(f"⚠️ **{title}** ({area})\n{(detail or '')[:900]}" + (f"\nAgent: {owner_id}" if owner_id else "") + (f"\n{path}" if path else ""))
    except Exception:  # noqa: BLE001
        pass
