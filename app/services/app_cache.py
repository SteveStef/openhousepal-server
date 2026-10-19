"""Warm caches that survive a restart. The town/IDX market numbers live in memory for speed
(content_adapters._market_cache / _served_areas_cache); every time one is computed it's also saved
to the app_cache table, and at startup they're loaded back (`preload`) - so after a deploy the very
first visitor gets the last numbers instantly, refreshed in the background, instead of waiting
~16 s while they're rebuilt (user, 10/19 - "nobody should ever have a bad experience").
Never raises - a cache problem must never break a page.
"""
import asyncio
import datetime as _dt
import decimal
import json
import logging
import uuid
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

_tasks: set = set()


def _default(o):
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (_dt.datetime, _dt.date)):
        return o.isoformat()
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    raise TypeError(f"not JSON-able: {type(o).__name__}")


async def save(key: str, value) -> None:
    try:
        payload = json.dumps(value, default=_default)
    except Exception:  # noqa: BLE001 - not storable; it just won't survive a restart
        return
    try:
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            await db.execute(text(
                "INSERT INTO app_cache (key, value, updated_at) VALUES (:k, CAST(:v AS JSONB), now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
            ), {"k": key, "v": payload})
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("app cache save failed", extra={"key": key}, exc_info=True)


def save_bg(key: str, value) -> None:
    """Save without making the caller wait (fire-and-forget on the running loop)."""
    try:
        task = asyncio.get_running_loop().create_task(save(key, value))
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def load_all(prefixes: tuple[str, ...], max_age_days: int = 7) -> dict[str, object]:
    """{key: value} for every saved entry under these prefixes, newer than `max_age_days`."""
    out: dict[str, object] = {}
    try:
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(text(
                "SELECT key, value FROM app_cache WHERE updated_at > now() - make_interval(days => :d)"
            ), {"d": max_age_days})).all()
        for key, value in rows:
            if key.startswith(prefixes):
                out[key] = value
    except Exception:  # noqa: BLE001 - e.g. table not created yet
        logger.warning("app cache load failed", exc_info=True)
    return out


async def load(key: str) -> Optional[object]:
    try:
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            return (await db.execute(text("SELECT value FROM app_cache WHERE key = :k"), {"k": key})).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        return None
