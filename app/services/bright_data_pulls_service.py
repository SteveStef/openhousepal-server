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

Repairs that keep running for good (10/21, 11/02):

  recheck_off_market  weekly: every OFF_MARKET-BRIGHT listing Bright still has comes back.
  stale_recheck       daily: every on-market listing Bright hasn't modified in 14 days is re-read
                      by key; one Bright no longer has goes off-market (its removal notice was lost).
  missing_check       daily (11/02): every listing Bright has on the market, key + status, against ours;
                      one we lack or hold with another status is re-read - the 5-minute sync's safety net.
  photo_refill        listings holding fewer photos than Bright's TotalPhotos get their gallery
                      again (after sold_3y finishes), plus any media fetch that failed anywhere.

Switched on by BRIGHT_DATA_PULLS (comma list, or "all"); PRICE_HISTORY_BACKFILL=true also turns on
price_history. Nothing here sends alerts or touches Showcases - it only fills the mirror.
"""
import asyncio
import json
import logging
import os
import time
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PULLS = ("sold_3y", "refresh_current", "price_history", "agent_sales", "recheck_off_market", "stale_recheck", "missing_check", "photo_refill",
         "town_sales_1y")
_CURRENT = ("ACTIVE-BRIGHT", "COMING SOON-BRIGHT", "PENDING-BRIGHT", "ACTIVE UNDER CONTRACT-BRIGHT")
PAGE = 200


def enabled(name: str) -> bool:
    raw = {p.strip() for p in os.getenv("BRIGHT_DATA_PULLS", "").lower().split(",") if p.strip()}
    if name == "price_history" and os.getenv("PRICE_HISTORY_BACKFILL", "").lower() == "true":
        return True
    # Agents' sales are part of the sales backfill - one switch (user, 10/15).
    if name == "agent_sales" and "sold_3y" in raw:
        return True
    # The off-market repair rides the same switch: wherever sales are pulled, falsely deleted
    # listings get re-checked once (10/20) - no new production setting needed.
    if name == "recheck_off_market" and "sold_3y" in raw:
        return True
    # Same switch for the daily stale recheck and the photo refill (10/21) - production only.
    if name in ("stale_recheck", "missing_check", "photo_refill", "town_sales_1y") and "sold_3y" in raw:
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


async def pull_sold_3y(db: AsyncSession, max_pages: int = 10, pause: float = 1.0, time_budget: Optional[float] = None) -> dict:
    """Month by month (oldest first), page by page. Photos are fetched for each page.

    time_budget (seconds): stop starting new pages after this long. 10 pages took ~2.5 minutes, so
    the 10-minute job sat idle the rest of the time (10/21) - the tick now pulls for ~8 minutes, still
    one request after another (Bright has never throttled it), leaving room for agent_sales."""
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
    deadline = time.monotonic() + time_budget if time_budget else None
    while pages < max_pages and (deadline is None or time.monotonic() < deadline):
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


# One-time past-year sales for the towns around the CMA test home (user, 10/22: "pull it from Bright
# now" - Downingtown-address sales Mar-Aug 2026 were ~35% priced while sold_3y caught up), same as
# the 10/20 Bala Cynwyd / Coatesville / Exton pulls. Leases skipped. Runs once, then marks done.
TOWN_SALES_1Y = (("Downingtown", "PA"), ("Thorndale", "PA"), ("Glenmoore", "PA"), ("Parkesburg", "PA"), ("West Chester", "PA"))


async def pull_town_sales_1y(db: AsyncSession, time_budget: float = 420, pause: float = 0.5) -> dict:
    from app.services.bright_mls_service import bright_mls_service
    from app.services.property_sync_service import PropertySyncService
    key = "data_pull_town_sales_1y"
    st = await _get(db, key)
    if st.get("done"):
        return {"done": True}
    date_from = st.get("from") or (date.today() - timedelta(days=365)).isoformat()
    i, skip = int(st.get("town") or 0), int(st.get("skip") or 0)
    syncer = PropertySyncService()
    added = 0
    deadline = time.monotonic() + time_budget
    while i < len(TOWN_SALES_1Y) and time.monotonic() < deadline:
        city, state = TOWN_SALES_1Y[i]
        raw_rows = await bright_mls_service.get_sold_properties(city, state, date_from, top=PAGE, skip=skip)
        rows = [r for r in raw_rows if "lease" not in str(r.get("PropertyType") or "").lower()]
        if rows:
            photos = await bright_mls_service.get_media_for_properties([str(r["ListingKey"]) for r in rows])
            for raw in rows:
                try:
                    ev = await syncer._sync_single_property(db, raw, photos, allow_inactive_insert=True)
                    added += 1 if ev and ev.get("type") in ("NEW_GLOBAL", "NEW_QUIET") else 0
                except Exception:  # noqa: BLE001 - one bad record never stops the pull
                    await db.rollback()
                    logger.warning("town_sales_1y: listing failed", exc_info=True)
        if len(raw_rows) < PAGE:
            i, skip = i + 1, 0
        else:
            skip += PAGE
        st = {"from": date_from, "town": i, "skip": skip, "read_total": int(st.get("read_total") or 0) + len(rows),
              "added_total": int(st.get("added_total") or 0) + added}
        if i >= len(TOWN_SALES_1Y):
            st.update({"done": True, "finished": date.today().isoformat()})
        await _set(db, key, st)
        added = 0
        await asyncio.sleep(pause)
    return {"done": bool(st.get("done")), **st}


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


_OFF_MARKET_KEYS: dict = {}


async def recheck_off_market(db: AsyncSession, max_keys: int = 3000, pause: float = 0.5) -> dict:
    """One pass over every listing we hold as OFF_MARKET-BRIGHT, in listing-key order, remembering
    where it stopped. Any listing Bright still returns is re-read, so its real status comes back
    (for sale, pending, or sold with its ClosePrice); the rest stay off-market.

    Repairs the false deletions applied before _sync_deletions started confirming with Bright
    (10/20): Bright's Deletion feed also reports listings that are still live, and those homes had
    vanished from Showcases, IDX and CMA comps. No alerts are sent - it only fixes the mirror.

    Weekly since 10/21: a week after a pass finishes, the next one starts from the first key."""
    from app.services.bright_mls_service import bright_mls_service
    from app.services.property_sync_service import PropertySyncService
    key = "data_pull_recheck_off_market"
    st = await _get(db, key)
    if st.get("done"):
        finished = date.fromisoformat(st["finished"]) if st.get("finished") else date.today()
        if date.today() - finished < timedelta(days=7):
            return {"done": True, "next_pass": (finished + timedelta(days=7)).isoformat()}
        st = {"pass_started": date.today().isoformat(), "last_pass_finished": finished.isoformat()}
        await _set(db, key, st)
    # The pass's off-market keys, read once (only off-market rows, via the status index) and kept in
    # this process - "listing_key > after ORDER BY listing_key" walked all 1.2 million homes in key
    # order every tick (owner, 11/13). A restart just reads the list again.
    pass_id = st.get("pass_started") or "first"
    if _OFF_MARKET_KEYS.get("pass") != pass_id:
        _OFF_MARKET_KEYS.clear()
        _OFF_MARKET_KEYS.update({"pass": pass_id, "keys": sorted((await db.execute(text(
            "SELECT listing_key FROM properties WHERE home_status = 'OFF_MARKET-BRIGHT'"))).scalars())})
    after = st.get("after_key") or ""
    import bisect
    start = bisect.bisect_right(_OFF_MARKET_KEYS["keys"], after)
    keys = _OFF_MARKET_KEYS["keys"][start:start + max_keys]
    if not keys:
        st = {**st, "done": True, "finished": date.today().isoformat()}
        await _set(db, key, st)
        return st
    syncer = PropertySyncService()
    for i in range(0, len(keys), 100):
        part = keys[i:i + 100]
        found = await bright_mls_service.get_listings_by_keys(part)
        # Photos only for homes back on the market (buyers see them); a sale keeps its stored gallery.
        on_market = [str(r["ListingKey"]) for r in found if not str(r.get("MlsStatus") or "").startswith("CLOSED")]
        photos = await bright_mls_service.get_media_for_properties(on_market) if on_market else {}
        for raw in found:
            try:
                await syncer._sync_single_property(db, raw, photos, allow_inactive_insert=False)
            except Exception:  # noqa: BLE001 - one bad record never stops the pass
                await db.rollback()
                logger.warning("recheck_off_market: listing failed", exc_info=True)
        st = {**st, "after_key": part[-1], "checked": int(st.get("checked") or 0) + len(part),
              "restored": int(st.get("restored") or 0) + len(found)}
        await _set(db, key, st)
        await asyncio.sleep(pause)
    return {"done": False, **st}


# Daily passes start in the quiet night hours (06:00-11:00 UTC = 1-6 AM Eastern in winter), never
# around the 00:00 UTC (7 PM ET) database backup.
_NIGHT_UTC = (6, 11)


def _night_now() -> bool:
    from datetime import datetime, timezone
    return _NIGHT_UTC[0] <= datetime.now(timezone.utc).hour < _NIGHT_UTC[1]


_TAX_OK = "length({c}) >= 6 AND {c} ~ '[1-9]' AND {c} !~* 'no tax'"


async def price_history_tick(db: AsyncSession, time_budget: float = 480) -> dict:
    """The price-history job's 10-minute tick, cheap (owner, 11/13). The one-time backlog is done;
    what's left is each NEW on-market listing's earlier listings. It used to find those by walking
    all 1.2 million homes every 10 minutes (3+ minutes of disk reads for ~10 parcels). Now:
      - every tick: only listings Bright changed since the bookmark (the modification_timestamp
        index - a split second), plus parcels a time limit / Bright outage left last time
      - once a night: every on-market listing's parcel not pulled yet (reads only on-market rows),
        which also catches anything the bookmark can't see (a listing put back on the market by a
        re-check with an old timestamp, a parcel whose lookup failed)
    Nothing is retried in a loop: a failed parcel waits for the next night's pass."""
    from datetime import datetime, timezone
    from app.services.price_history_service import backfill_batch
    key = "data_pull_price_history"
    st = await _get(db, key)
    today = date.today().isoformat()
    hi = (await db.execute(text("SELECT max(modification_timestamp) FROM properties"))).scalar()
    cur = list(_CURRENT)
    if not st.get("since"):
        # First run: the last day's changes now; the night pass picks up anything older.
        st = {**st, "since": ((hi or datetime.now(timezone.utc)) - timedelta(days=1)).astimezone(timezone.utc).isoformat()}
    nightly = st.get("night_day") != today and _night_now()
    if nightly:
        rows = (await db.execute(text(
            "WITH cur AS MATERIALIZED (SELECT listing_tax_id FROM properties WHERE home_status = ANY(:current)) "
            "SELECT DISTINCT c.listing_tax_id FROM cur c WHERE " + _TAX_OK.format(c="c.listing_tax_id") + " "
            "AND NOT EXISTS (SELECT 1 FROM price_history_parcels h WHERE h.tax_id = c.listing_tax_id) "
            "ORDER BY 1"), {"current": cur})).scalars().all()
    else:
        since = datetime.fromisoformat(st["since"]) - timedelta(minutes=30)  # overlap: rows saved a bit out of order
        rows = (await db.execute(text(
            "SELECT DISTINCT p.listing_tax_id FROM properties p WHERE p.modification_timestamp > :since "
            "AND p.home_status = ANY(:current) AND " + _TAX_OK.format(c="p.listing_tax_id") + " "
            "AND NOT EXISTS (SELECT 1 FROM price_history_parcels h WHERE h.tax_id = p.listing_tax_id) "
            "ORDER BY 1"), {"since": since, "current": cur})).scalars().all()
    fresh = set(rows)
    queued = [t for t in (st.get("queue") or []) if t not in fresh]
    if queued:  # left over from last tick - only those still not pulled
        pulled = set((await db.execute(text("SELECT tax_id FROM price_history_parcels WHERE tax_id = ANY(:t)"),
                                       {"t": queued})).scalars())
        queued = [t for t in queued if t not in pulled]
    todo = queued + list(rows)
    res = await backfill_batch(db, tax_ids=todo, time_budget=time_budget) if todo else {"parcels": 0, "listings_added": 0, "failed": 0, "not_reached": []}
    left = list(res.get("not_reached") or [])[:20000]
    new_st = {"since": (hi.astimezone(timezone.utc).isoformat() if hi else st.get("since")),
              "night_day": today if nightly else st.get("night_day"), "queue": left}
    await _set(db, key, new_st)
    return {"mode": "nightly" if nightly else "recent", "parcels": res.get("parcels", 0),
            "listings_added": res.get("listings_added", 0), "failed": res.get("failed", 0), "left_for_next_tick": len(left)}


# The daily stale re-check's Bright key list, gathered over a tick or two in this process (125k keys -
# too big to save after every page; a restart just gathers it again).
_STALE_SEEN: dict = {}


async def stale_recheck(db: AsyncSession, stale_days: int = 14, time_budget: float = 300, pause: float = 0.5) -> dict:
    """Once a night, finds the listings we hold as on the market that Bright no longer has on the
    market, and reads just those again by key, 100 per request:
      - Bright still has it: re-read (a missed status change - Pending -> Sold - lands, with its event)
      - Bright doesn't: marked off-market with a "Taken off market" event dated today (we only know
        its removal notice was lost, not when it was sent)
    Fixes homes stuck as Pending / Under Contract after a lost removal notice (~5,000 on 10/21). No
    photos are fetched - stored galleries are kept. No alerts are sent.

    How it finds them (owner, 11/13): Bright's list of every on-market key (1,000 keys per light
    request, like missing_check) against ours (one read of our on-market rows). It used to re-read
    every on-market listing Bright hadn't modified in 14 days - ~60,000 full records a day, nearly
    all unchanged - found by walking all 1.2 million homes. Same homes fixed, a fraction of the work,
    and a lost removal notice is caught the next night rather than after 14 days. `stale_days` is
    kept for callers; it no longer limits the pass."""
    from app.services.bright_mls_service import bright_mls_service
    from app.services.property_sync_service import PropertySyncService
    key = "data_pull_stale_recheck"
    today = date.today().isoformat()
    st = await _get(db, key)
    if st.get("day") != today:
        if not _night_now():
            return {**st, "waiting_for": "night"} if st.get("done") else st
        st = {"day": today, "checked": 0, "updated": 0, "off_market": 0,
              "last_pass": {k: st.get(k) for k in ("day", "checked", "updated", "off_market", "candidates", "done") if k in st} or None}
        await _set(db, key, st)
    if st.get("done"):
        return st
    deadline = time.monotonic() + time_budget
    if "todo" not in st:
        # 1. Bright's on-market keys (raises if Bright can't be asked - nothing is marked, the tick is retried).
        seen = _STALE_SEEN if _STALE_SEEN.get("day") == today else None
        if seen is None:
            _STALE_SEEN.clear()
            _STALE_SEEN.update({"day": today, "keys": set(), "status_idx": 0, "skip": 0})
            seen = _STALE_SEEN
        while seen["status_idx"] < len(_CURRENT):
            if time.monotonic() >= deadline:
                return {**st, "gathering": len(seen["keys"])}
            rows = await bright_mls_service.get_listings_by_status(_CURRENT[seen["status_idx"]], top=1000, skip=seen["skip"], select="ListingKey")
            seen["keys"].update(str(r.get("ListingKey")) for r in rows if r.get("ListingKey"))
            if len(rows) < 1000:
                seen["status_idx"] += 1
                seen["skip"] = 0
            else:
                seen["skip"] += 1000
            await asyncio.sleep(pause)
        # 2. Ours, minus Bright's. A key Bright's paging skipped (a status change mid-list) just gets
        # read again by key below - harmless.
        ours = (await db.execute(text(
            "SELECT listing_key FROM properties WHERE home_status = ANY(:current) "
            "AND coalesce(is_manual, false) = false AND listing_key ~ '^[0-9]+$'"), {"current": list(_CURRENT)})).scalars().all()
        todo = sorted(set(ours) - seen["keys"])
        bright_count = len(seen["keys"])
        _STALE_SEEN.clear()
        if bright_count < 1000:
            # Bright answered with (almost) nothing - never treat that as "every home is gone".
            logger.warning("stale_recheck: Bright's on-market list came back nearly empty - pass skipped", extra={"bright": bright_count})
            st.update(done=True, skipped="bright list empty", bright=bright_count)
            await _set(db, key, st)
            return st
        st.update(todo=todo, candidates=len(todo), bright=bright_count)
        if len(todo) > 3000:
            from app.services.admin_alerts import alert_admin
            await alert_admin("bright_stale_recheck", "Many listings to re-check",
                              f"{len(todo)} listings we show as on the market aren't on Bright's on-market list tonight - re-reading them by key.")
        await _set(db, key, st)
    syncer = PropertySyncService()
    while time.monotonic() < deadline:
        rows = list(st.get("todo") or [])[:100]
        if not rows:
            st.pop("todo", None)
            st["done"] = True
            break
        # Raises if Bright can't be asked - nothing is marked, the tick is retried (never "couldn't ask" = "gone").
        found = await bright_mls_service.get_listings_by_keys(rows)
        found_keys = {str(r.get("ListingKey")) for r in found}
        for raw in found:
            try:
                await syncer._sync_single_property(db, raw, {}, allow_inactive_insert=False)
            except Exception:  # noqa: BLE001 - one bad record never stops the pass
                await db.rollback()
                logger.warning("stale_recheck: listing failed", exc_info=True)
        missing = [k for k in rows if k not in found_keys]
        off = await syncer.mark_off_market(db, {k: None for k in missing}) if missing else 0
        st.update(todo=list(st.get("todo") or [])[len(rows):], checked=st["checked"] + len(rows),
                  updated=st["updated"] + len(found_keys), off_market=st["off_market"] + off)
        await _set(db, key, st)
        await asyncio.sleep(pause)
    await _set(db, key, st)
    return st


async def missing_check(db: AsyncSession, time_budget: float = 300, pause: float = 0.5) -> dict:
    """Once a day, the safety net under the 5-minute sync (owner, 11/02): every listing Bright has
    for sale / coming soon / pending / under contract, 1,000 keys per request (key + status only),
    compared with ours. One we don't have, or hold with a different status, is read again in full
    and saved - so a change the sync ever missed, for any reason, is fixed within a day. Listings we
    hold as on-market that Bright has since changed are stale_recheck's job. A pass that has to fix
    a lot tells the admin, since it means the 5-minute sync is missing things. No alerts to agents."""
    from app.services.bright_mls_service import bright_mls_service
    from app.services.property_sync_service import PropertySyncService
    key = "data_pull_missing_check"
    today = date.today().isoformat()
    st = await _get(db, key)
    if st.get("day") != today:
        st = {"day": today, "status_idx": 0, "skip": 0, "bright": 0, "fixed": 0, "new": 0,
              "last_pass": {k: st.get(k) for k in ("day", "bright", "fixed", "new", "done") if k in st} or None}
    if st.get("done"):
        return st
    syncer = PropertySyncService()
    deadline = time.monotonic() + time_budget
    while time.monotonic() < deadline:
        if st["status_idx"] >= len(_CURRENT):
            st["done"] = True
            logger.info("Missing-listings check done", extra={"event": "missing_check", "result": st})
            if st["fixed"] > 25:
                from app.services.admin_alerts import alert_admin
                await alert_admin("bright_missing_check", "Bright sync missed listings",
                                  f"The nightly check fixed {st['fixed']} of {st['bright']} on-market listings ({st['new']} we didn't have).")
            break
        status = _CURRENT[st["status_idx"]]
        # Raises if Bright can't be asked - nothing is changed, the tick is retried.
        rows = await bright_mls_service.get_listings_by_status(status, top=1000, skip=st["skip"], select="ListingKey,MlsStatus")
        keys = [str(r.get("ListingKey")) for r in rows if r.get("ListingKey")]
        ours = dict((await db.execute(text("SELECT listing_key, home_status FROM properties WHERE listing_key = ANY(:k)"),
                                      {"k": keys})).all()) if keys else {}
        wrong = [str(r.get("ListingKey")) for r in rows if r.get("ListingKey") and ours.get(str(r.get("ListingKey"))) != r.get("MlsStatus")]
        if wrong:
            full = await bright_mls_service.get_listings_by_keys(wrong)
            new_keys = [k for k in wrong if k not in ours]
            photos = await bright_mls_service.get_media_for_properties(new_keys) if new_keys else {}
            for raw in full:
                try:
                    await syncer._sync_single_property(db, raw, photos, allow_inactive_insert=False)
                except Exception:  # noqa: BLE001 - one bad record never stops the pass
                    await db.rollback()
                    logger.warning("missing_check: listing failed", exc_info=True)
            st["fixed"] += len(full)
            st["new"] += len(new_keys)
        st["bright"] += len(rows)
        # By position within one status: a listing changing status mid-pass can be passed over
        # tonight - tomorrow's pass (and the 5-minute sync) has it.
        if len(rows) < 1000:
            st["status_idx"] += 1
            st["skip"] = 0
        else:
            st["skip"] += 1000
        await _set(db, key, st)
        await asyncio.sleep(pause)
    await _set(db, key, st)
    return st


PHOTO_QUEUE_KEY = "photo_retry_queue"


async def _queue_photo_retries(db: AsyncSession, keys) -> None:
    """Adds listing keys to the stored photo retry queue (atomic - several jobs may add at once)."""
    keys = sorted({str(k) for k in keys})
    if not keys:
        return
    await db.execute(text(
        "INSERT INTO system_settings (key, value) VALUES (:k, jsonb_build_object('keys', CAST(:new AS jsonb))) "
        "ON CONFLICT (key) DO UPDATE SET updated_at = now(), value = jsonb_build_object('keys', ("
        "  SELECT coalesce(jsonb_agg(DISTINCT x), '[]'::jsonb) FROM jsonb_array_elements_text("
        "    coalesce(system_settings.value->'keys', '[]'::jsonb) || CAST(:new AS jsonb)) AS x))"),
        {"k": PHOTO_QUEUE_KEY, "new": json.dumps(keys)})
    await db.commit()


async def _unqueue_photo_retries(db: AsyncSession, keys) -> None:
    await db.execute(text(
        "UPDATE system_settings SET updated_at = now(), value = jsonb_build_object('keys', ("
        "  SELECT coalesce(jsonb_agg(x), '[]'::jsonb) FROM jsonb_array_elements_text(value->'keys') AS x"
        "  WHERE NOT (x = ANY(:done)))) WHERE key = :k"), {"k": PHOTO_QUEUE_KEY, "done": [str(k) for k in keys]})
    await db.commit()


async def _refill_photos(db: AsyncSession, held: dict) -> dict:
    """{listing key: photos we hold} -> fetches the gallery again where Bright has more photos
    (TotalPhotos) than we hold and the seller didn't ask for them hidden once off market. Saves a
    gallery only when it is longer than the stored one. Raises if Bright can't be asked."""
    from app.models.database import PropertyFieldChange
    from app.services.bright_mls_service import bright_mls_service
    counts = await bright_mls_service.get_photo_counts(list(held))
    todo = []
    for k, n in held.items():
        info = counts.get(k)
        if not info or not isinstance(info.get("total"), int) or info["total"] <= n:
            continue
        on_market = str(info.get("status") or "") in _CURRENT
        if info.get("hide_off_market") and not on_market:
            continue
        todo.append(k)
    photos = await bright_mls_service.get_media_for_properties(todo) if todo else {}
    filled = 0
    for k in todo:
        gallery = photos.get(k) or []
        if len(gallery) <= held[k]:
            continue
        res = await db.execute(text(
            "UPDATE properties SET photos = CAST(:p AS jsonb), img_src = coalesce(img_src, :first), updated_at = now() "
            "WHERE listing_key = :k AND (CASE WHEN jsonb_typeof(photos) = 'array' THEN jsonb_array_length(photos) ELSE 0 END) < :n"),
            {"p": json.dumps(gallery), "first": gallery[0], "k": k, "n": len(gallery)})
        if res.rowcount:
            db.add(PropertyFieldChange(listing_key=k, field="photos_count", old_value=str(held[k]), new_value=str(len(gallery))))
            filled += 1
    await db.commit()
    return {"checked": len(held), "short": len(todo), "filled": filled}


async def photo_refill(db: AsyncSession, time_budget: float = 480, pause: float = 0.5) -> dict:
    """Galleries that came out short: older sales saved before the 10/11 media fix (about 20 photos
    of about 37), history rows saved before 10/21 (one photo), and any listing whose media fetch
    failed since (bright_mls_service.media_retry_keys -> the stored queue). Queued listings go first,
    any time; the pass starts once sold_3y is done, so the two never compete for Bright. The pass
    compares every listing that isn't on the market (on-market ones are kept whole by the sync) with
    Bright's TotalPhotos - a light three-field read per 100 - and fetches only the short ones. One
    pass, remembering where it stopped."""
    from app.services.bright_mls_service import bright_mls_service
    key = "data_pull_photo_refill"
    st = await _get(db, key)
    deadline = time.monotonic() + time_budget
    # 1. Failed media fetches from any job in this process, then the stored queue.
    pending = set(bright_mls_service.media_retry_keys)
    bright_mls_service.media_retry_keys.difference_update(pending)
    await _queue_photo_retries(db, pending)
    queued = [str(k) for k in ((await _get(db, PHOTO_QUEUE_KEY)).get("keys") or [])][:500]
    retried = 0
    for i in range(0, len(queued), 100):
        if time.monotonic() >= deadline:
            break
        part = queued[i:i + 100]
        held = {r.listing_key: int(r.n) for r in (await db.execute(text(
            "SELECT listing_key, CASE WHEN jsonb_typeof(photos) = 'array' THEN jsonb_array_length(photos) ELSE 0 END AS n "
            "FROM properties WHERE listing_key = ANY(:keys)"), {"keys": part})).all()}
        if held:
            retried += (await _refill_photos(db, held))["filled"]
        await _unqueue_photo_retries(db, part)
    if retried:
        st = {**st, "queue_filled_total": int(st.get("queue_filled_total") or 0) + retried}
        await _set(db, key, st)
    # 2. The pass over short galleries - after the sales backfill.
    if st.get("done"):
        return {**st, "queue_filled": retried}
    if not (await _get(db, "data_pull_sold_3y")).get("done"):
        return {"waiting_for": "sold_3y", "queue_filled": retried}
    while time.monotonic() < deadline:
        rows = (await db.execute(text(
            "SELECT listing_key, CASE WHEN jsonb_typeof(photos) = 'array' THEN jsonb_array_length(photos) ELSE 0 END AS n "
            "FROM properties WHERE listing_key > :after AND listing_key ~ '^[0-9]+$' AND coalesce(is_manual, false) = false "
            "AND NOT (home_status = ANY(:current)) "
            "ORDER BY listing_key LIMIT 100"), {"after": st.get("after_key") or "", "current": list(_CURRENT)})).all()
        if not rows:
            st = {**st, "done": True, "finished": date.today().isoformat()}
            await _set(db, key, st)
            break
        res = await _refill_photos(db, {r.listing_key: int(r.n) for r in rows})
        st = {**st, "after_key": rows[-1].listing_key, "checked": int(st.get("checked") or 0) + res["checked"],
              "short": int(st.get("short") or 0) + res["short"], "filled": int(st.get("filled") or 0) + res["filled"]}
        await _set(db, key, st)
        await asyncio.sleep(pause)
    return {**st, "queue_filled": retried}


# price_history runs as its own scheduler job: one batch of 300 parcels takes ~2 hours, and while it
# shared the tick, sold_3y only got its 10 pages every ~2 hours instead of every 10 minutes (10/20).
# photo_refill rides the sales job: while sold_3y runs it only retries the (short) queue of failed
# media fetches; once sold_3y is done the job's whole 8 minutes go to the photo pass.
SALES_PULLS = ("town_sales_1y", "refresh_current", "sold_3y", "agent_sales", "photo_refill")
HISTORY_PULLS = ("price_history",)
REPAIR_PULLS = ("recheck_off_market", "stale_recheck", "missing_check")


async def run_enabled(db: AsyncSession, names: tuple = PULLS) -> dict:
    """One tick of every switched-on pull in `names` (called every 10 minutes). Each pull is isolated
    - one failing (e.g. Bright rejecting its query) is logged and never stops the others from running."""
    out = {}
    # Both long pulls stop starting new work at ~8 minutes, so each tick ends inside its 10-minute slot.
    for name, run in (("town_sales_1y", lambda: pull_town_sales_1y(db)),
                      ("refresh_current", lambda: refresh_current(db)),
                      ("sold_3y", lambda: pull_sold_3y(db, max_pages=60, time_budget=480)),
                      ("price_history", lambda: price_history_tick(db, time_budget=480)),
                      ("agent_sales", lambda: pull_agent_sales(db)),
                      ("photo_refill", lambda: photo_refill(db, time_budget=480)),
                      ("recheck_off_market", lambda: recheck_off_market(db)),
                      ("stale_recheck", lambda: stale_recheck(db, time_budget=300)),
                      ("missing_check", lambda: missing_check(db, time_budget=240))):
        if name not in names or not enabled(name):
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
        "recheck_off_market": await _get(db, "data_pull_recheck_off_market"),
        "stale_recheck": await _get(db, "data_pull_stale_recheck"),
        "town_sales_1y": await _get(db, "data_pull_town_sales_1y"),
        "photo_refill": {**await _get(db, "data_pull_photo_refill"),
                         "queued": len((await _get(db, PHOTO_QUEUE_KEY)).get("keys") or [])},
    }
