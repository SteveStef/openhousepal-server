"""Every email OpenHousePal sends, counted for the admin Insights Emails page (EmailLog).

Sending happens on worker threads (EmailService.send_simple_message runs in the thread pool), so
record() only drops a small dict in a buffer; the rows are written by flush() on the app's event
loop moments later (and by the Insights page and the Mailgun webhook before they read). Nothing
here can ever fail or slow down a send.

Privacy: template name, who it's for (agent | client | account), the agent it belongs to and what
Mailgun said happened. Never the address, subject or body."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import update

logger = logging.getLogger(__name__)

AUDIENCES = ("agent", "client", "account")
_MAX_BUFFER = 5000

_buf: list[dict] = []
_lock = threading.Lock()
_loop: Optional[asyncio.AbstractEventLoop] = None
_scheduled = False


def attach_loop(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Called once at startup so sends on worker threads can hand their rows to the app's loop."""
    global _loop
    try:
        _loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        pass


def provider_id_from(body: Optional[str]) -> Optional[str]:
    """Mailgun answers {"id": "<2026...@mg.openhousepal.com>", ...}; its webhooks carry the same id
    without the brackets."""
    try:
        mid = (json.loads(body or "") or {}).get("id")
    except Exception:  # noqa: BLE001
        return None
    return str(mid).strip().strip("<>")[:255] if mid else None


def record(template: str, audience: str, *, ok: bool = True, held: bool = False, owner_id: Optional[str] = None,
           client_email_id: Optional[str] = None, via: str = "openhousepal", provider_id: Optional[str] = None) -> None:
    try:
        row = {
            "id": str(uuid.uuid4()), "template": (template or "unknown")[:80],
            "audience": audience if audience in AUDIENCES else "account",
            "owner_id": owner_id or None, "client_email_id": client_email_id or None, "via": via[:16],
            "status": "held" if held else ("sent" if ok else "failed"),
            "provider_id": provider_id, "created_at": datetime.now(timezone.utc),
        }
        with _lock:
            if len(_buf) < _MAX_BUFFER:
                _buf.append(row)
        _schedule_flush()
    except Exception:  # noqa: BLE001
        logger.debug("email_log record failed", exc_info=True)


def _schedule_flush() -> None:
    global _scheduled
    with _lock:
        if _scheduled:
            return
        _scheduled = True
    try:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            running.create_task(_flush_bg())
        elif _loop is not None and _loop.is_running():
            asyncio.run_coroutine_threadsafe(_flush_bg(), _loop)
        else:
            raise RuntimeError("no loop")
    except Exception:  # noqa: BLE001 - the scheduler's minute job flushes it instead
        with _lock:
            _scheduled = False


async def _flush_bg() -> None:
    global _scheduled
    await asyncio.sleep(1)  # let a burst of sends land in one write
    with _lock:
        _scheduled = False
    await flush()


async def flush(db=None) -> int:
    """Write whatever's buffered. Uses its own session unless one is given."""
    from app.models.database import EmailLog
    with _lock:
        rows, _buf[:] = list(_buf), []
    if not rows:
        return 0
    try:
        if db is not None:
            db.add_all([EmailLog(**r) for r in rows])
            await db.flush()
        else:
            from app.database import AsyncSessionLocal
            async with AsyncSessionLocal() as s:
                s.add_all([EmailLog(**r) for r in rows])
                await s.commit()
        return len(rows)
    except Exception:  # noqa: BLE001
        logger.warning("email_log flush failed", exc_info=True)
        with _lock:  # try again on the next flush
            _buf[:0] = rows[: max(0, _MAX_BUFFER - len(_buf))]
        return 0


async def mark_delivery(db, provider_id: Optional[str], delivery: str) -> None:
    """A Mailgun webhook about one of our emails. A later 'delivered' never hides an earlier bounce."""
    if not provider_id:
        return
    from app.models.database import EmailLog
    await flush()
    pid = str(provider_id).strip().strip("<>")[:255]
    q = update(EmailLog).where(EmailLog.provider_id == pid)
    if delivery in ("delivered", "delayed"):
        q = q.where((EmailLog.delivery.is_(None)) | (EmailLog.delivery.in_(("delivered", "delayed"))))
    await db.execute(q.values(delivery=delivery, delivery_at=datetime.now(timezone.utc)))
    await db.commit()
