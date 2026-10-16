"""Production Bright MLS data pulls - the one-time loads the 5-minute sync can't do on its own.

The 5-minute sync only follows listings that CHANGE, and only adds a listing it first sees as
active / coming soon. So three loads run once in production, a few pages every 10 minutes, each
remembering where it stopped (system_settings), until done:

  sold_3y          every sale (not leases) that closed in the last 3 years across Bright, with
                   photos - the CMA comparable pool (~650k sales).
  refresh_current  every listing on the market now, re-read once (with photos) so fields we started
                   requesting (internet-display flags, Coming Soon date, sale type, concessions...)
                   are filled in, and rows saved before the photo-paging fix get their full HD
                   gallery, without waiting for each listing to change.
  price_history    every earlier listing of each home on the market (back to 2012) - see
                   price_history_service.backfill_batch.
  agent_sales      part of sold_3y (on whenever it is): every sale (any year, listing side or buyer
                   side, no leases) of every agent who has an OpenHousePal account - their Agent Hub / website / signature sales go
                   further back than the 3-year pool. Never "done": a new agent's MLS id is picked
                   up on the next tick (Bright audit, 10/15). New sales after that arrive through
                   the 5-minute sync's closed-listings step.

Switched on by BRIGHT_DATA_PULLS (comma list, or "all"); PRICE_HISTORY_BACKFILL=true also turns on
price_history. Nothing here sends alerts or touches Showcases - it only fills the mirror.
"""
import asyncio
import json
import logging
import os
from datetime import date
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PULLS = ("sold_3y", "refresh_current", "price_history", "agent_sales")
_CURRENT = ("ACTIVE-BRIGHT", "COMING SOON-BRIGHT", "PENDING-BRIGHT", "ACTIVE UNDER CONTRACT-BRIGHT")
PAGE = 200


def enabled(name: str) -> bool:
    raw = {p.strip() for p in os.getenv("BRIGHT_DATA_PULLS", "").lower().split(",") if p.strip()}
    if name == "price_history" and os.getenv("PRICE_HISTORY_BACKFILL", "").lower() == "true":
        return True
    # Agents' sales are part of the sales backfill - one switch (user, 10/15).
    if name == "agent_sales" and "sold_3y" in raw:
        return True
    return "all" in raw or name in raw


def any_enabled() -> bool:
    return any(enabled(p) for p in PULLS)


async def _get(db: AsyncSession, key: str) -> dict:
    v = (await db.execute(text("SELECT value FROM system_settings WHERE key = :k"), {"k": key})).scalar()
    return v if isinstance(v, dict) else {}


async def _set(db: AsyncSession, key: str, value: dict) -> None:
    await db.execute(text(
        "INSERT INTO system_settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"), {"k": key, "v": json.dumps(value)})
    await db.commit()


def _month_add(d: date, n: int = 1) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


async def pull_sold_3y(db: AsyncSession, max_pages: int = 10, pause: float = 1.0) -> dict:
    """Month by month (oldest first), page by page. Photos are fetched for each page."""
    from app.services.bright_mls_service import bright_mls_service
    from app.services.property_sync_service import PropertySyncService
    key = "data_pull_sold_3y"
    st = await _get(db, key)
    if st.get("done"):
        return {"done": True}
    today = date.today()
    month = date.fromisoformat(st["month"]) if st.get("month") else date(today.year - 3, today.month, 1)
    skip = int(st.get("skip") or 0)
    syncer = PropertySyncService()
    pages = added = 0
    while pages < max_pages:
        if month > today:
            await _set(db, key, {**st, "done": True, "finished": today.isoformat()})
            return {"done": True, "added": added}
        nxt = _month_add(month)
        rows = await bright_mls_service.get_closed_window(month.isoformat(), nxt.isoformat(), top=PAGE, skip=skip)
        if rows:
            photos = await bright_mls_service.get_media_for_properties([str(r["ListingKey"]) for r in rows])
            for raw in rows:
                try:
                    ev = await syncer._sync_single_property(db, raw, photos, allow_inactive_insert=True)
                    added += 1 if ev and ev.get("type") in ("NEW_GLOBAL", "NEW_QUIET") else 0
                except Exception:  # noqa: BLE001 - one bad record never stops the pull
                    await db.rollback()
                    logger.warning("sold_3y: listing failed", exc_info=True)
        if len(rows) < PAGE:
            month, skip = nxt, 0
        else:
            skip += PAGE
        st = {"month": month.isoformat(), "skip": skip, "added_total": int(st.get("added_total") or 0) + added}
        await _set(db, key, st)
        added = 0
        pages += 1
        await asyncio.sleep(pause)
    return {"done": False, **st}


async def refresh_current(db: AsyncSession, max_pages: int = 10, pause: float = 1.0) -> dict:
    """Every on-market listing once, status by status, photos included (older rows hold a short,
    lower-res set from before the media paging fix). If the media call fails for a listing, its
    stored photos are kept - see _sync_single_property."""
    from app.services.bright_mls_service import bright_mls_service
    from app.services.property_sync_service import PropertySyncService
    key = "data_pull_refresh_current"
    st = await _get(db, key)
    if st.get("done"):
        return {"done": True}
    idx, skip = int(st.get("status_idx") or 0), int(st.get("skip") or 0)
    syncer = PropertySyncService()
    pages = 0
    while pages < max_pages:
        if idx >= len(_CURRENT):
            await _set(db, key, {"done": True, "finished": date.today().isoformat()})
            return {"done": True}
        rows = await bright_mls_service.get_listings_by_status(_CURRENT[idx], top=PAGE, skip=skip)
        photos = await bright_mls_service.get_media_for_properties([str(r["ListingKey"]) for r in rows]) if rows else {}
        for raw in rows:
            try:
                await syncer._sync_single_property(db, raw, photos, allow_inactive_insert=False)
            except Exception:  # noqa: BLE001
                await db.rollback()
                logger.warning("refresh_current: listing failed", exc_info=True)
        if len(rows) < PAGE:
            idx, skip = idx + 1, 0
        else:
            skip += PAGE
        await _set(db, key, {"status_idx": idx, "skip": skip})
        pages += 1
        await asyncio.sleep(pause)
    return {"done": False, "status": _CURRENT[idx] if idx < len(_CURRENT) else None, "skip": skip}


async def pull_agent_sales(db: AsyncSession, max_agents: int = 5, pause: float = 1.0) -> dict:
    """Each OpenHousePal agent's full sales history from Bright, a few agents per tick. Remembers
    which MLS ids are finished (system_settings data_pull_agent_sales), so a new sign-up is the
    only work on later ticks."""
    from app.services.bright_mls_service import bright_mls_service
    from app.services.property_sync_service import PropertySyncService
    key = "data_pull_agent_sales"
    st = await _get(db, key)
    done = set(st.get("done_ids") or [])
    ids = [r for r in (await db.execute(text(
        "SELECT DISTINCT trim(mls_id) FROM users WHERE mls_id IS NOT NULL AND trim(mls_id) <> ''"))).scalars()]
    todo = [i for i in ids if i not in done][:max_agents]
    syncer = PropertySyncService()
    added = 0
    for mid in todo:
        ok = True
        for field in bright_mls_service._AGENT_FIELDS:
            skip = 0
            for _ in range(25):  # 5,000 sales per side is far beyond any one agent
                try:
                    rows = await bright_mls_service.get_agent_closed_sales(field, mid, top=PAGE, skip=skip)
                except Exception:  # noqa: BLE001 - retried next tick
                    logger.warning("agent_sales: Bright lookup failed", extra={"field": field}, exc_info=True)
                    ok = False
                    break
                if rows:
                    photos = await bright_mls_service.get_media_for_properties([str(r["ListingKey"]) for r in rows])
                    for raw in rows:
                        try:
                            ev = await syncer._sync_single_property(db, raw, photos, allow_inactive_insert=True)
                            added += 1 if ev and ev.get("type") in ("NEW_GLOBAL", "NEW_QUIET") else 0
                        except Exception:  # noqa: BLE001
                            await db.rollback()
                            logger.warning("agent_sales: listing failed", exc_info=True)
                if len(rows) < PAGE:
                    break
                skip += PAGE
                await asyncio.sleep(pause)
        if ok:
            done.add(mid)
    st = {"done_ids": sorted(done), "added_total": int(st.get("added_total") or 0) + added, "last_tick": date.today().isoformat()}
    await _set(db, key, st)
    return {"agents_done": len(done), "agents_waiting": max(0, len(ids) - len(done)), "added": added}


async def run_enabled(db: AsyncSession) -> dict:
    """One tick of every switched-on pull (called every 10 minutes). Each pull is isolated - one
    failing (e.g. Bright rejecting its query) is logged and never stops the others from running."""
    from app.services.price_history_service import backfill_batch
    out = {}
    for name, run in (("refresh_current", lambda: refresh_current(db)),
                      ("sold_3y", lambda: pull_sold_3y(db)),
                      ("price_history", lambda: backfill_batch(db, max_parcels=300)),
                      ("agent_sales", lambda: pull_agent_sales(db))):
        if not enabled(name):
            continue
        try:
            out[name] = await run()
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            logger.error(f"Bright data pull failed: {name}", exc_info=True)
            out[name] = {"error": str(getattr(e, "detail", e))[:300]}
            # A pull that keeps failing used to go unnoticed for days - tell the admin (throttled).
            from app.services.admin_alerts import alert_admin
            await alert_admin("bright_data_pulls", f"Bright data pull failing: {name}", out[name]["error"])
    if out:
        logger.info("Bright data pulls tick", extra={"event": "bright_data_pulls", "result": out})
    return out


async def status(db: AsyncSession) -> dict:
    parcels_done = (await db.execute(text("SELECT count(*) FROM price_history_parcels"))).scalar()
    return {
        "switched_on": [p for p in PULLS if enabled(p)],
        "sold_3y": await _get(db, "data_pull_sold_3y"),
        "refresh_current": await _get(db, "data_pull_refresh_current"),
        "price_history": {"parcels_done": parcels_done},
        "agent_sales": {k: v for k, v in (await _get(db, "data_pull_agent_sales")).items() if k != "done_ids"},
    }
