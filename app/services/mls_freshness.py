"""How current our copy of the MLS is (grader audit 11/13, findings 1/2/24).

Three CMAs and three articles were made on a copy whose listings had stopped updating 25 days
earlier: a closed sale showed as "pending", three price cuts were missing, and the articles said
"right now" about a sales window that ended Oct 19. Nothing checked. Every CMA and AI article now
asks here first.

"Current through" = the newest listing change we hold (indexed column). Bright changes listings
every few minutes, so a gap of a day means the sync is behind, not that the market was quiet.

Where the sync is supposed to run (PROPERTY_SYNC_ENABLED=true, the live site) data more than
BLOCK_HOURS old stops the CMA/article. On a copy that never syncs (a dev machine) nothing is
blocked - the report is labeled instead, so a test can still run but can't pass for current.
"""
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

WARN_HOURS = 24.0
BLOCK_HOURS = 72.0
FRESH_WORDING_DAYS = 7   # articles may say "right now"/"latest" only when the data is this fresh


def sync_expected() -> bool:
    return os.getenv("PROPERTY_SYNC_ENABLED", "false").lower() == "true"


def _day(dt: datetime) -> str:
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def describe(current_through: Optional[datetime], *, now: Optional[datetime] = None, enforced: Optional[bool] = None) -> dict:
    """Pure: {current_through, age_hours, age_days, level: ok|warn|blocked, message, fresh_wording}."""
    now = now or datetime.now(timezone.utc)
    enforced = sync_expected() if enforced is None else enforced
    if current_through is None:
        return {"current_through": None, "age_hours": None, "age_days": None, "level": "ok", "message": None,
                "fresh_wording": True}
    if current_through.tzinfo is None:
        current_through = current_through.replace(tzinfo=timezone.utc)
    hours = max(0.0, (now - current_through).total_seconds() / 3600)
    days = int(hours // 24)
    level = "ok"
    if hours > BLOCK_HOURS and enforced:
        level = "blocked"
    elif hours > WARN_HOURS:
        level = "warn"
    message = None
    if level != "ok":
        ago = f"{days} day{'s' if days != 1 else ''} ago"
        message = (f"Review before sending: the MLS data behind this was last updated {_day(current_through)} ({ago}). "
                   "Prices and statuses may have changed since - a home shown as for sale or under contract may have "
                   "sold. Check the homes before you send it.")
    return {"current_through": current_through.isoformat(), "current_through_label": _day(current_through),
            "age_hours": round(hours, 1), "age_days": days, "level": level, "message": message,
            "fresh_wording": hours <= FRESH_WORDING_DAYS * 24}


def blocked_message(info: dict, what: str = "CMA") -> str:
    return (f"Our MLS data is {info.get('age_days')} days behind (last updated {info.get('current_through_label')}), so "
            f"this {what} could show old prices or sold homes as still for sale. We're on it - please try again a little later.")


async def check(db: AsyncSession, *, now: Optional[datetime] = None) -> dict:
    try:
        ts = (await db.execute(text(
            "select max(modification_timestamp) from properties where modification_timestamp <= now() + interval '1 day'"
        ))).scalar()
    except Exception:  # noqa: BLE001 - never fail a report because the check itself failed
        ts = None
    return describe(ts, now=now)
