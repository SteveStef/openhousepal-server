from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload, joinedload
from typing import List, Dict, Any, Optional
from datetime import date, datetime, timezone, timedelta
import os
import time
import re

from app.models.database import (
    Collection, CollectionPreferences, Property, collection_properties, 
    PropertyInteraction, ScheduledEmail, Notification, SystemSettings, SchoolDistrict, 
    City, Township, Brokerage
)
from app.services.bright_mls_service import bright_mls_service
from app.services.mls_open_houses import merge_open_house_rows, rebuild_open_house_slots, single_columns, stored_slots
from app.utils.mls_mapper import map_reso_to_internal, parse_dt
from app.utils.geo import get_lat_long_offsets, is_within_distance
from app.utils.normalization import normalize_brokerage, normalize_township, normalize_school_district
from app.services.email_service import EmailService
from app.services.blacklist_service import BlacklistService
from app.services.website.site_service import resolve_owner_public_base_url
from app.config.logging import get_logger
from app.database import AsyncSessionLocal
from app.services.agent_email_service import agent_public_phone
from urllib.parse import quote

logger = get_logger(__name__)

# Raw MLS keys Bright only fills on closed-scoped queries (see bright_mls_service
# get_closed_properties_modified_since) - kept when a later read comes back without them.
_STICKY_SOLD_KEYS = ("ClosePrice", "CloseDate", "BuyerAgentMlsId", "CoBuyerAgentMlsId", "BuyerAgentFullName",
                     "CoBuyerAgentFullName", "BuyerOfficeName", "BuyerOfficeMlsId", "PurchaseContractDate")

# Field change log (property_field_changes): mapped columns left out - the raw copy itself, values that
# move on every read (day counts, read times, Bright's own modified time) and the primary photo URL
# (the gallery is logged as its photo count instead).
_FIELD_LOG_SKIP = {"raw_mls_data", "dom_read_at", "updated_at", "modification_timestamp", "days_on_market",
                   "cumulative_days_on_market", "img_src", "photos"}
# Bright fields with no column of their own that are still worth a history.
_FIELD_LOG_RAW = ("ClosePrice", "CloseDate", "PurchaseContractDate", "OnMarketDate", "OffMarketDate",
                  "ExpectedOnMarketDate", "TotalPhotos", "AutomaticallyHideMediaOffMarketYN",
                  "InternetEntireListingDisplayYN", "InternetAddressDisplayYN")
_FIELD_LOG_MAX = 500  # characters kept of one value
_FIELD_LOG_MAX_ROWS = 40  # fields logged for one listing in one read


def _int_or_none(v) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _field_text(v) -> Optional[str]:
    """One value as comparable text: the DB's copy and Bright's new copy of the same value come out
    identical (float vs int, datetime vs string, list order), so only real changes are logged."""
    import enum
    import json
    if isinstance(v, enum.Enum):  # HomeType is a str enum - compare its value, not "HomeType.CONDO"
        v = v.value
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f.is_integer() else str(round(f, 6))
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (list, dict)):
        if not v:
            return None
        return json.dumps(v, sort_keys=True, default=str)[:_FIELD_LOG_MAX]
    s = str(v).strip()
    if s.endswith("Z") and len(s) >= 19 and s[4:5] == "-" and s[10:11] == "T":  # a Bright timestamp string
        parsed = parse_dt(s)
        if parsed:
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    return s[:_FIELD_LOG_MAX] or None


def _log_field_changes(db, l_key: str, existing, mapped: Dict[str, Any]) -> int:
    """Adds one property_field_changes row per field this read changes (old -> new). Called before
    the new values are applied. Returns how many were logged."""
    from app.models.database import PropertyFieldChange
    changes: list[tuple[str, Optional[str], Optional[str]]] = []
    for key, value in mapped.items():
        if key in _FIELD_LOG_SKIP or not hasattr(existing, key):
            continue
        old, new = _field_text(getattr(existing, key)), _field_text(value)
        if old != new:
            changes.append((key, old, new))
    old_photos = len(existing.photos) if isinstance(existing.photos, list) else 0
    new_photos = len(mapped.get("photos") or []) if isinstance(mapped.get("photos"), list) else 0
    if "photos" in mapped and old_photos != new_photos:
        changes.append(("photos_count", str(old_photos), str(new_photos)))
    old_raw = existing.raw_mls_data if isinstance(existing.raw_mls_data, dict) else {}
    new_raw = mapped.get("raw_mls_data") if isinstance(mapped.get("raw_mls_data"), dict) else None
    if new_raw is not None:
        for key in _FIELD_LOG_RAW:
            if key not in old_raw:  # not asked for back then (TotalPhotos before 10/21) - not a change
                continue
            old, new = _field_text(old_raw.get(key)), _field_text(new_raw.get(key))
            if old != new:
                changes.append((f"raw.{key}", old, new))
    for field, old, new in changes[:_FIELD_LOG_MAX_ROWS]:
        db.add(PropertyFieldChange(listing_key=l_key, field=field, old_value=old, new_value=new))
    return len(changes[:_FIELD_LOG_MAX_ROWS])

class PropertySyncService:
    """
    Global Synchronization Service.
    Syncs ALL modified properties from Bright MLS to the local mirror 
    and notifies affected collections.
    """
    def __init__(self):
        self.email_service = EmailService()
        self.SYNC_KEY = "last_property_sync_time"
        self.OPEN_HOUSE_SYNC_KEY = "last_open_house_sync_time"
        # Day (YYYY-MM-DD) the once-a-day open house reconciliation last ran, and its page cap
        # (200 rows/page - well above Bright's typical count of upcoming open houses).
        self.OPEN_HOUSE_RECONCILE_KEY = "last_open_house_reconcile_date"
        self.OPEN_HOUSE_RECONCILE_MAX_PAGES = 150
        self.CLOSED_SYNC_KEY = "last_closed_property_sync_time"
        self.DELETION_SYNC_KEY = "last_deletion_sync_time"

    async def get_last_sync_time(self, db: AsyncSession, key: Optional[str] = None, fallback_hours: int = 24) -> str:
        """Retrieves the last successful sync timestamp from SystemSettings.

        fallback_hours: how far back to start when there is no stored checkpoint yet. The 5-minute
        global sync only ever needs a short window; the closed-listings leg uses a wider one so a
        cold start still sweeps up recently-settled sales."""
        stmt = select(SystemSettings).where(SystemSettings.key == (key or self.SYNC_KEY))
        result = await db.execute(stmt)
        setting = result.scalar_one_or_none()

        if setting and setting.value and "timestamp" in setting.value:
            return setting.value["timestamp"]

        fallback = (datetime.now(timezone.utc) - timedelta(hours=fallback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return fallback

    async def update_last_sync_time(self, db: AsyncSession, timestamp: str, key: Optional[str] = None):
        """Persists the last successful sync timestamp."""
        stmt = select(SystemSettings).where(SystemSettings.key == (key or self.SYNC_KEY))
        result = await db.execute(stmt)
        setting = result.scalar_one_or_none()

        val = {"timestamp": timestamp}
        if setting:
            setting.value = val
        else:
            db.add(SystemSettings(key=(key or self.SYNC_KEY), value=val))

        await db.commit()

    async def run_global_sync(self, max_pages: Optional[int] = None) -> Dict[str, Any]:
        """
        The Main Sync Loop.
        1. Process Hard Deletions (Isolated).
        2. Process Modifications & Status Changes (Paged).
        3. Notify affected collections.

        max_pages caps how many pages (of page_size properties each) this run will process before
        stopping early - used by the on-demand "Refresh Listings" trigger so a stale/never-run
        checkpoint (e.g. a big backlog) can't turn one button click into an unbounded, long-running
        sync. The scheduled cron calls this with no cap, since it runs often enough that the
        backlog per run stays small. Checkpoint progress made before an early stop is still saved
        (see "Finalize sync state" below), so a follow-up run just continues from there.
        """
        logger.info("Starting Global Synchronization Cycle...")
        stats = {
            "updated": 0, 
            "notifications_sent": 0, 
            "deletions": 0,
            "errors": 0, 
            "property_details": []
        }
        
        async with AsyncSessionLocal() as db:
            last_sync = await self.get_last_sync_time(db)
            logger.info(f"Syncing from checkpoint: {last_sync}")

            # --- PART A: PROCESS HARD DELETIONS (Isolated) ---
            try:
                await self._sync_deletions(db, last_sync, stats)
            except Exception as e:
                logger.error(f"Critical error in deletion sync (skipping to modifications): {e}")
                stats["errors"] += 1

            # --- PART B: PROCESS MODIFICATIONS (Existing Paged Loop) ---
            skip = 0
            page_size = 200
            new_last_sync = last_sync
            had_failure = False
            pages_processed = 0

            while True:
                if max_pages is not None and pages_processed >= max_pages:
                    logger.info(f"Reached max_pages={max_pages} for this run - stopping early, checkpoint saved so far.")
                    break
                try:
                    # 1. Fetch modified batch from MLS
                    raw_properties = await bright_mls_service.get_properties_modified_since(last_sync, top=page_size, skip=skip)
                    if not raw_properties:
                        logger.info("No more modified properties found.")
                        break
                    
                    logger.info(f"Found {len(raw_properties)} modified properties in this batch (skip={skip})")

                    # 2. Batch fetch media
                    listing_keys = [str(p["ListingKey"]) for p in raw_properties]
                    photo_map = await bright_mls_service.get_media_for_properties(listing_keys)

                    # 3. Process each property
                    for raw_prop in raw_properties:
                        l_key = str(raw_prop.get("ListingKey"))
                        mod_ts = raw_prop.get("ModificationTimestamp")
                        
                        try:
                            # Perform the local update and detect events
                            sync_event = await self._sync_single_property(db, raw_prop, photo_map)
                            if sync_event:
                                stats["updated"] += 1
                                stats["property_details"].append({
                                    "listing_key": sync_event["listing_key"],
                                    "home_type": sync_event["data"]["home_type"],
                                    "event": sync_event["type"]
                                })
                                # 4. Notify affected collections
                                notified = await self._propagate_property_change(db, sync_event)
                                stats["notifications_sent"] += notified
                            
                            if not had_failure and mod_ts and (not new_last_sync or mod_ts > new_last_sync):
                                new_last_sync = mod_ts
                                
                        except Exception as prop_error:
                            logger.error(f"Error syncing property {l_key}: {prop_error}")
                            stats["errors"] += 1
                            had_failure = True

                    pages_processed += 1
                    if len(raw_properties) < page_size:
                        break
                    skip += page_size

                except Exception as e:
                    logger.error(f"Error in sync batch at skip {skip}: {e}")
                    stats["errors"] += 1
                    break

            # 4. Finalize sync state
            if new_last_sync != last_sync:
                await self.update_last_sync_time(db, new_last_sync)
                logger.info(f"Global Sync Cycle Complete. New Checkpoint: {new_last_sync}")

        return stats

    async def run_open_house_sync(self, max_pages: Optional[int] = None) -> Dict[str, Any]:
        """
        Incrementally syncs Bright's separately-tracked open house schedule (BrightOpenHouses)
        into Property.mls_open_houses (every upcoming slot) + Property.mls_open_house_* (the
        soonest one). Deliberately its own loop with its own checkpoint (not folded into
        run_global_sync): scheduling a new open house doesn't necessarily touch the listing's own
        ModificationTimestamp, only the open house record's.

        Every row in a batch is merged (a listing can have a Saturday and a Sunday slot) and past
        slots are dropped, so an edit to an old open house never knocks out an upcoming one. An
        open house stays on the listing until its day passes (no cancellation handling - product
        rule). Once a day it also runs _reconcile_open_houses: adds anything the incremental feed
        missed and clears slots whose day has passed.
        """
        logger.info("Starting Open House Synchronization Cycle...")
        stats = {"updated": 0, "errors": 0}

        async with AsyncSessionLocal() as db:
            last_sync = await self.get_last_sync_time(db, key=self.OPEN_HOUSE_SYNC_KEY)
            logger.info(f"Syncing open houses from checkpoint: {last_sync}")

            skip = 0
            page_size = 200
            new_last_sync = last_sync
            had_failure = False
            pages_processed = 0

            while True:
                if max_pages is not None and pages_processed >= max_pages:
                    logger.info(f"Reached max_pages={max_pages} for open house sync - stopping early, checkpoint saved so far.")
                    break
                try:
                    raw_open_houses = await bright_mls_service.get_open_houses_modified_since(last_sync, top=page_size, skip=skip)
                    if not raw_open_houses:
                        logger.info("No more modified open houses found.")
                        break

                    logger.info(f"Found {len(raw_open_houses)} modified open house records in this batch (skip={skip})")

                    # Group every row per listing, keeping feed (modification) order so the
                    # latest change to a slot wins.
                    by_listing: Dict[str, List[Dict[str, Any]]] = {}
                    for row in raw_open_houses:
                        l_key = str(row.get("OpenHouseListingKey") or "")
                        if not l_key:
                            continue

                        mod_ts = row.get("OpenHouseModificationTimestamp")
                        if not had_failure and mod_ts and (not new_last_sync or mod_ts > new_last_sync):
                            new_last_sync = mod_ts

                        by_listing.setdefault(l_key, []).append(row)

                    if not await self._apply_open_house_rows(db, by_listing, stats):
                        had_failure = True

                    pages_processed += 1
                    if len(raw_open_houses) < page_size:
                        break
                    skip += page_size

                except Exception as e:
                    logger.error(f"Error in open house sync batch at skip {skip}: {e}")
                    stats["errors"] += 1
                    break

            if new_last_sync != last_sync:
                await self.update_last_sync_time(db, new_last_sync, key=self.OPEN_HOUSE_SYNC_KEY)
                logger.info(f"Open House Sync Cycle Complete. New Checkpoint: {new_last_sync}")

            # Once a day: full rebuild. Same PROPERTY_SYNC_ENABLED gate as the scheduler, checked
            # again here so a manual/admin call in a sync-disabled environment never walks Bright's
            # whole upcoming set.
            if os.getenv("PROPERTY_SYNC_ENABLED", "false").lower() == "true":
                today_str = date.today().isoformat()
                last_reconcile = await self.get_last_sync_time(db, key=self.OPEN_HOUSE_RECONCILE_KEY)
                if not str(last_reconcile).startswith(today_str):
                    # Marked before running so a failing reconcile can't retry every 30 minutes.
                    await self.update_last_sync_time(db, today_str, key=self.OPEN_HOUSE_RECONCILE_KEY)
                    stats["reconcile"] = await self._reconcile_open_houses(db)

        return stats

    async def run_open_house_backfill(self, max_pages: Optional[int] = None) -> Dict[str, Any]:
        """
        Full pull of every currently-scheduled future open house, regardless of modification
        time - the same rebuild the daily reconciliation does (_reconcile_open_houses), run on
        demand. Safe to run more than once.

        On completion, advances the incremental checkpoint to now() so the regular loop doesn't
        re-walk records this backfill already covered.
        """
        logger.info("Starting Open House Backfill...")
        async with AsyncSessionLocal() as db:
            stats = await self._reconcile_open_houses(db, max_pages=max_pages)
            now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            await self.update_last_sync_time(db, now_ts, key=self.OPEN_HOUSE_SYNC_KEY)
            logger.info(f"Open House Backfill Complete. Checkpoint advanced to: {now_ts}")
        return stats

    async def _reconcile_open_houses(self, db: AsyncSession, max_pages: Optional[int] = None) -> Dict[str, Any]:
        """Adds every open house in Bright's full upcoming set to its listing (catching anything
        the incremental feed missed) and drops slots whose day has passed. Additive - it never
        removes an upcoming slot.

        Paged and capped like the other loops. Nothing is written unless the whole set was read
        cleanly - a partial read must never wipe real open houses."""
        stats = {"updated": 0, "cleared": 0, "errors": 0, "complete": False}
        page_cap = max_pages if max_pages is not None else self.OPEN_HOUSE_RECONCILE_MAX_PAGES
        page_size = 200
        skip = 0
        pages_processed = 0
        rows_by_listing: Dict[str, List[Dict[str, Any]]] = {}

        while True:
            if pages_processed >= page_cap:
                logger.warning(f"Open house reconcile hit its {page_cap}-page cap - skipping the rebuild this time.")
                break
            try:
                raw_open_houses = await bright_mls_service.get_upcoming_open_houses(top=page_size, skip=skip)
            except Exception as e:
                logger.error(f"Error in open house reconcile batch at skip {skip}: {e}")
                stats["errors"] += 1
                break
            for row in raw_open_houses or []:
                l_key = str(row.get("OpenHouseListingKey") or "")
                if l_key:
                    rows_by_listing.setdefault(l_key, []).append(row)
            pages_processed += 1
            if not raw_open_houses or len(raw_open_houses) < page_size:
                stats["complete"] = True
                break
            skip += page_size

        if not stats["complete"]:
            return stats

        today = date.today()
        plan = await self._open_house_reconcile_plan(db, rows_by_listing, today)
        for l_key, slots in plan.items():
            try:
                res = await db.execute(
                    update(Property).where(Property.listing_key == l_key)
                    .values(mls_open_houses=slots or None, **single_columns(slots))
                )
                await db.commit()
                if res.rowcount:
                    stats["updated" if slots else "cleared"] += 1
            except Exception as e:
                logger.error(f"Error reconciling open houses for {l_key}: {e}")
                stats["errors"] += 1
                await db.rollback()
        logger.info(f"Open house reconcile done: {stats}")
        return stats

    async def _open_house_reconcile_plan(self, db: AsyncSession, rows_by_listing: Dict[str, List[Dict[str, Any]]], today: date) -> Dict[str, List[dict]]:
        """Which listings the reconcile needs to rewrite, and to what: each listing's stored slots
        plus anything new in Bright's upcoming set, minus slots whose day has passed. Listings
        already correct are skipped. Reads the DB only - no Bright calls."""
        cols_q = (Property.listing_key, Property.mls_open_houses, Property.mls_open_house_date,
                  Property.mls_open_house_start_time, Property.mls_open_house_end_time)
        # Local listings currently showing something, plus (in chunks - this set can be large)
        # every listing Bright has an upcoming open house for.
        result = await db.execute(select(*cols_q).where(or_(Property.mls_open_house_date >= today, Property.mls_open_houses.is_not(None))))
        local = {r.listing_key: r for r in result.all()}
        keys = [k for k in rows_by_listing if k not in local]
        for i in range(0, len(keys), 1000):
            result = await db.execute(select(*cols_q).where(Property.listing_key.in_(keys[i:i + 1000])))
            local.update({r.listing_key: r for r in result.all()})
        plan: Dict[str, List[dict]] = {}
        for r in local.values():
            stored = stored_slots(r.mls_open_houses, r.mls_open_house_date, r.mls_open_house_start_time, r.mls_open_house_end_time)
            slots = rebuild_open_house_slots(stored, rows_by_listing.get(r.listing_key, []), today)
            cols = single_columns(slots)
            unchanged = (
                (r.mls_open_houses or []) == slots
                and r.mls_open_house_date == cols["mls_open_house_date"]
                and r.mls_open_house_start_time == cols["mls_open_house_start_time"]
                and r.mls_open_house_end_time == cols["mls_open_house_end_time"]
            )
            if not unchanged:
                plan[r.listing_key] = slots
        return plan

    async def run_closed_sync(self, max_pages: Optional[int] = None) -> Dict[str, Any]:
        """Incremental sync of CLOSED-BRIGHT listings, on its own checkpoint.

        Why this exists as a separate leg from run_global_sync:
          - The general modified-since feed carries closed listings with a NULL sale price -
            Bright only fills ClosePrice/CloseDate when the query scopes to closed status. The
            CMA valuation engine needs the verified closed price, so we re-fetch closed records
            through get_closed_properties_modified_since.
          - run_global_sync never *inserts* a listing that wasn't ACTIVE/COMING SOON when first
            seen, so a comp that sold before we ever tracked it would never enter the mirror.
            Here we pass allow_inactive_insert=True so newly-discovered sold comps land too.

        Catches the active->closed transition (ModificationTimestamp bumps on settlement) and
        later corrections to an already-closed record. No collection propagation - a settled sale
        isn't a buyer alert, and we don't want price-drop emails firing off closed comps.

        First run (no checkpoint) starts 45 days back. The deep multi-year history per market is
        the job of scripts/pull_sold_comps.py, not this loop.
        """
        logger.info("Starting Closed-Listings Synchronization Cycle...")
        stats = {"updated": 0, "inserted": 0, "errors": 0}

        async with AsyncSessionLocal() as db:
            last_sync = await self.get_last_sync_time(db, key=self.CLOSED_SYNC_KEY, fallback_hours=45 * 24)
            logger.info(f"Syncing closed listings from checkpoint: {last_sync}")

            skip = 0
            page_size = 200
            new_last_sync = last_sync
            had_failure = False
            pages_processed = 0

            while True:
                if max_pages is not None and pages_processed >= max_pages:
                    logger.info(f"Reached max_pages={max_pages} for closed sync - stopping early, checkpoint saved so far.")
                    break
                try:
                    raw_properties = await bright_mls_service.get_closed_properties_modified_since(last_sync, top=page_size, skip=skip)
                    if not raw_properties:
                        logger.info("No more modified closed listings found.")
                        break

                    logger.info(f"Found {len(raw_properties)} modified closed listings in this batch (skip={skip})")

                    listing_keys = [str(p["ListingKey"]) for p in raw_properties]
                    photo_map = await bright_mls_service.get_media_for_properties(listing_keys)

                    for raw_prop in raw_properties:
                        l_key = str(raw_prop.get("ListingKey"))
                        mod_ts = raw_prop.get("ModificationTimestamp")
                        try:
                            sync_event = await self._sync_single_property(db, raw_prop, photo_map, allow_inactive_insert=True)
                            if sync_event:
                                if sync_event["type"] in ("NEW_GLOBAL", "NEW_QUIET"):
                                    stats["inserted"] += 1
                                else:
                                    stats["updated"] += 1

                            if not had_failure and mod_ts and (not new_last_sync or mod_ts > new_last_sync):
                                new_last_sync = mod_ts

                        except Exception as prop_error:
                            logger.error(f"Error syncing closed listing {l_key}: {prop_error}")
                            stats["errors"] += 1
                            had_failure = True

                    pages_processed += 1
                    if len(raw_properties) < page_size:
                        break
                    skip += page_size

                except Exception as e:
                    logger.error(f"Error in closed sync batch at skip {skip}: {e}")
                    stats["errors"] += 1
                    break

            if new_last_sync != last_sync:
                await self.update_last_sync_time(db, new_last_sync, key=self.CLOSED_SYNC_KEY)
                logger.info(
                    f"Closed-Listings Sync Cycle Complete. inserted={stats['inserted']} "
                    f"updated={stats['updated']} errors={stats['errors']} New Checkpoint: {new_last_sync}"
                )

        return stats

    async def _apply_open_house_rows(self, db: AsyncSession, rows_by_listing: Dict[str, List[Dict[str, Any]]], stats: Dict[str, Any]) -> bool:
        """Merges a batch's BrightOpenHouses rows into each listing's stored slots (see
        merge_open_house_rows) and refreshes the soonest-slot columns. Listings that aren't in our
        local mirror (e.g. inactive/never synced) are skipped. Returns False if any listing failed."""
        if not rows_by_listing:
            return True
        result = await db.execute(
            select(Property.listing_key, Property.mls_open_houses, Property.mls_open_house_date,
                   Property.mls_open_house_start_time, Property.mls_open_house_end_time)
            .where(Property.listing_key.in_(list(rows_by_listing.keys())))
        )
        local = {r.listing_key: r for r in result.all()}
        today = date.today()
        ok = True
        for l_key, rows in rows_by_listing.items():
            r = local.get(l_key)
            if r is None:
                continue
            try:
                existing = stored_slots(r.mls_open_houses, r.mls_open_house_date, r.mls_open_house_start_time, r.mls_open_house_end_time)
                slots = merge_open_house_rows(existing, rows, today)
                await db.execute(
                    update(Property).where(Property.listing_key == l_key)
                    .values(mls_open_houses=slots or None, **single_columns(slots))
                )
                await db.commit()
                stats["updated"] += 1
            except Exception as e:
                logger.error(f"Error syncing open house for {l_key}: {e}")
                stats["errors"] += 1
                ok = False
                await db.rollback()
        return ok

    async def _deletion_checkpoint(self, db: AsyncSession, last_sync: str) -> str:
        """Where the deletions step starts. Its own checkpoint (10/21) - it used to read from the
        modifications checkpoint, which moves every cycle, so a failed or partial deletion read was
        lost for good. Before its first save it starts where the modifications checkpoint is."""
        setting = (await db.execute(select(SystemSettings).where(SystemSettings.key == self.DELETION_SYNC_KEY))).scalar_one_or_none()
        if setting and isinstance(setting.value, dict) and setting.value.get("timestamp"):
            return setting.value["timestamp"]
        return last_sync

    async def _sync_deletions(self, db: AsyncSession, last_sync: str, stats: Dict[str, Any]):
        """Helper to process hard-deleted properties from MLS.

        The deletions checkpoint only moves once the whole window was read, confirmed with Bright
        and applied - any failure raises or returns before that, and the next cycle re-reads it."""
        logger.info("Checking for hard deletions...")
        since = await self._deletion_checkpoint(db, last_sync)
        deletions = await bright_mls_service.get_recent_deletions(since)

        if not deletions:
            logger.info("No hard deletions found.")
            return

        # Rows come oldest first, so the last row's time is how far this cycle got.
        done_through = deletions[-1]["timestamp"]
        logger.info(f"Detected {len(deletions)} hard deletions. Validating timestamps...")

        valid_deletion_keys = []
        batch_keys = [d["key"] for d in deletions]
        deletion_map = {d["key"]: parse_dt(d["timestamp"]) for d in deletions if parse_dt(d["timestamp"])}

        # Fetch current timestamps from DB for these keys
        stmt = select(Property.listing_key, Property.modification_timestamp).where(Property.listing_key.in_(batch_keys))
        result = await db.execute(stmt)
        local_props = {row.listing_key: row.modification_timestamp for row in result.all()}

        for d_key, d_ts in deletion_map.items():
            local_ts = local_props.get(d_key)
            # If we don't have it, or our last modification is older than the deletion event
            if not local_ts or (d_ts and d_ts > local_ts):
                valid_deletion_keys.append(d_key)
            else:
                logger.debug(f"Skipping deletion for {d_key}: local mod ({local_ts}) is newer than deletion ({d_ts})")

        if not valid_deletion_keys:
            logger.info("All deletions were older than our local modifications. No updates needed.")
            await self.update_last_sync_time(db, done_through, key=self.DELETION_SYNC_KEY)
            return

        # Bright's Deletion feed (CWA_BRIGHT_ALL) also reports listings that are still live in
        # BrightProperties - 4 of 8 sampled "deleted" active/pending homes and recent sales with a
        # ClosePrice were still there (10/20), and marking them off-market hid them from Showcases,
        # IDX and CMA comps. So a listing only goes off-market once Bright no longer returns it; one
        # it still has is re-read instead. If Bright can't be asked, nothing is marked this cycle.
        try:
            still_listed = await bright_mls_service.get_listings_by_keys(valid_deletion_keys)
        except Exception as e:
            logger.error(f"Could not confirm {len(valid_deletion_keys)} deletions with Bright - none applied this cycle: {e}")
            stats["errors"] += 1
            return
        live_keys = {str(r.get("ListingKey")) for r in still_listed}
        for raw in still_listed:
            try:
                await self._sync_single_property(db, raw, {}, allow_inactive_insert=False)
            except Exception as e:  # noqa: BLE001 - one bad record never stops the deletion step
                await db.rollback()
                logger.warning(f"Re-read of still-listed {raw.get('ListingKey')} failed: {e}")
        if live_keys:
            logger.info(f"Bright still lists {len(live_keys)} of the reported deletions - kept and re-read, not marked off-market.")
        valid_deletion_keys = [k for k in valid_deletion_keys if k not in live_keys]
        stats["false_deletions_kept"] = len(live_keys)
        if not valid_deletion_keys:
            await self.update_last_sync_time(db, done_through, key=self.DELETION_SYNC_KEY)
            return

        logger.warning(f"Applying {len(valid_deletion_keys)} verified hard deletions. Marking as off-market.")

        deleted_at = {d["key"]: d["timestamp"] for d in deletions}
        stats["deletions"] += await self.mark_off_market(db, {k: deleted_at.get(k) for k in valid_deletion_keys})
        await self.update_last_sync_time(db, done_through, key=self.DELETION_SYNC_KEY)
        logger.info(f"Successfully processed {stats['deletions']} hard deletions.")

    async def mark_off_market(self, db: AsyncSession, removed_at: Dict[str, Optional[str]]) -> int:
        """Marks listings Bright no longer has as OFF_MARKET-BRIGHT ({listing key: when Bright
        removed it, or None = now}) and commits. Returns how many changed (keys we don't hold, or
        already off-market, are left alone).

        Each home is kept with all its data - only its status changes. Going off-market is logged in
        the price history (it never was before 10/21) and the field change log, dated when Bright
        removed it, and that date is kept on the row (raw_mls_data.OhpOffMarketTimestamp - Bright has
        no off-market date field we can read) so the history never shows the stale last-modified date."""
        from app.models.database import PropertyFieldChange, PropertyPriceEvent
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        keys = list(removed_at)
        changed = 0
        for i in range(0, len(keys), 500):
            batch = keys[i : i + 500]
            current = (await db.execute(
                select(Property.listing_key, Property.home_status, Property.price)
                .where(Property.listing_key.in_(batch), Property.home_status != 'OFF_MARKET-BRIGHT')
            )).all()
            if not current:
                continue
            when = {r.listing_key: removed_at.get(r.listing_key) or now_iso for r in current}
            await db.execute(text(
                "UPDATE properties SET home_status = 'OFF_MARKET-BRIGHT', updated_at = now(), "
                "raw_mls_data = (CASE WHEN jsonb_typeof(raw_mls_data) = 'object' THEN raw_mls_data ELSE '{}'::jsonb END) "
                "|| jsonb_build_object('OhpOffMarketTimestamp', CAST(:ts AS text)) "
                "WHERE listing_key = :k AND home_status <> 'OFF_MARKET-BRIGHT'"),
                [{"k": r.listing_key, "ts": when[r.listing_key]} for r in current])
            for r in current:
                observed = parse_dt(when[r.listing_key]) or datetime.now(timezone.utc)
                db.add(PropertyPriceEvent(
                    listing_key=r.listing_key, kind="status", price=r.price, previous_price=r.price,
                    status="OFF_MARKET-BRIGHT", previous_status=r.home_status, observed_at=observed))
                db.add(PropertyFieldChange(listing_key=r.listing_key, field="home_status", old_value=r.home_status,
                                           new_value="OFF_MARKET-BRIGHT", observed_at=observed))
            changed += len(current)
        await db.commit()
        return changed

    async def _sync_single_property(self, db: AsyncSession, raw_data: Dict[str, Any], photo_map: Dict[str, List[str]], allow_inactive_insert: bool = False) -> Optional[Dict[str, Any]]:
        """
        Updates local mirror and detects price drops.
        Returns a 'sync_event' dict if successful.

        allow_inactive_insert: the recurring sync (run_global_sync) never passes this - a listing
        not already in the mirror is only inserted if ACTIVE/COMING SOON, so the DB doesn't fill
        up with old CLOSED listings we never tracked (see the is_active check below). A deliberate
        historical backfill (see backend/scripts/pull_sold_comps.py) sets this True to insert
        CLOSED-BRIGHT rows too - reusing this method's mapping/dedup/reference-table logic rather
        than duplicating it.
        """
        l_key = str(raw_data["ListingKey"])
        mapped = map_reso_to_internal(raw_data, photo_map)
        
        # --- SAFE AUTO-POPULATION OF REFERENCE TABLES ---
        state = (mapped.get("state") or "").upper().strip()

        # 1. School Districts
        raw_sd_name = (mapped.get("school_district_name") or "")
        sd_name = normalize_school_district(raw_sd_name)
        if sd_name and state:
            try:
                await db.execute(
                    insert(SchoolDistrict)
                    .values(name=sd_name, state=state)
                    .on_conflict_do_nothing()
                )
            except Exception as e:
                logger.warning(f"Failed to auto-populate school district {sd_name}: {e}")
        
        # 2. Cities (Safe + Junk Filter)
        city_name = (mapped.get("city") or "").upper().strip()
        if city_name and state and re.search(r"[A-Za-z]", city_name):
            try:
                await db.execute(
                    insert(City)
                    .values(name=city_name, state=state)
                    .on_conflict_do_nothing()
                )
            except Exception as e:
                logger.warning(f"Failed to auto-populate city {city_name}: {e}")

        # 3. Townships (Safe + Junk Filter)
        raw_township_name = (mapped.get("township") or "")
        township_name = normalize_township(raw_township_name)
        is_junk = (
            not township_name or 
            len(township_name) <= 1 or 
            re.match(r'^[0-9.\-]+$', township_name) or 
            not re.search(r"[A-Za-z]", township_name) or
            township_name in ['NA', 'N/A', 'NO', 'NT']
        )
        if not is_junk and state:
            try:
                await db.execute(
                    insert(Township)
                    .values(name=township_name, state=state)
                    .on_conflict_do_nothing()
                )
            except Exception as e:
                logger.warning(f"Failed to auto-populate township {township_name}: {e}")

        office_name = (mapped.get("list_office_name") or "")
        off_name, parent_name = normalize_brokerage(office_name)

        if off_name and parent_name:
            try:
                await db.execute(
                    insert(Brokerage)
                    .values(name=off_name, state=state, parent_name=parent_name)
                    .on_conflict_do_nothing()
                )
            except Exception as e:
                logger.warning(f"Failed to auto-populate brokerage {office_name} parent of {parent_name}: {e}")
        # ------------------------------------------------

        # Get existing record to compare state
        stmt = select(Property).where(Property.listing_key == l_key)
        res = await db.execute(stmt)
        existing = res.scalar_one_or_none()
        
        event_type = "UPDATE"
        old_price = None
        rematch = False

        if existing:
            # A home that just became something a buyer could want - Coming Soon -> Active, back on
            # the market (Pending -> Active), or a new price while for sale (it may have dropped into
            # someone's range) - is matched against every Showcase again, like a new listing
            # (user, 10/15). Showcases that already have it only get the price-drop update.
            new_status = str(mapped.get("home_status") or "")
            old_status = str(existing.home_status or "")
            for_sale = new_status.startswith("ACTIVE-BRIGHT") or new_status.startswith("COMING SOON")
            became_active = new_status.startswith("ACTIVE-BRIGHT") and not old_status.startswith("ACTIVE-BRIGHT")
            price_moved = bool(existing.price and mapped.get("price") and mapped["price"] != existing.price)
            rematch = for_sale and (became_active or price_moved)
            # Check for Price Drop
            if existing.price and mapped["price"] and mapped["price"] < existing.price:
                event_type = "PRICE_DROP"
                old_price = existing.price

            # Price history log: Bright only ever sends a listing's latest change, so every change
            # we see is kept here (property_price_events) - never allowed to break the sync.
            # A price and a status change seen together (a cut on the day it goes pending) are two
            # events - only the price used to be logged (10/21).
            try:
                from app.models.database import PropertyPriceEvent
                if existing.price and mapped.get("price") and mapped["price"] != existing.price:
                    db.add(PropertyPriceEvent(listing_key=l_key, kind="price", price=mapped["price"], previous_price=existing.price,
                                              status=mapped.get("home_status"), previous_status=existing.home_status))
                if mapped.get("home_status") and existing.home_status and mapped["home_status"] != existing.home_status:
                    db.add(PropertyPriceEvent(listing_key=l_key, kind="status", price=mapped.get("price"), previous_price=existing.price,
                                              status=mapped["home_status"], previous_status=existing.home_status))
            except Exception:  # noqa: BLE001
                logger.warning("price history event not recorded", exc_info=True)

            # A refresh that didn't fetch this listing's media (backfills, history pulls) only has the
            # one primary photo, and a failed or partial media fetch is short too - never let either
            # shrink a fuller gallery we already have. A shorter gallery is taken only when it was
            # fetched and holds every photo Bright says the listing has (TotalPhotos) - the agent
            # really removed some.
            new_photos = mapped.get("photos") or []
            fetched_whole = bool(photo_map) and l_key in photo_map and _int_or_none(raw_data.get("TotalPhotos")) is not None \
                and len(new_photos) >= _int_or_none(raw_data.get("TotalPhotos"))
            if isinstance(existing.photos, list) and len(existing.photos) > len(new_photos) and not fetched_whole:
                mapped["photos"] = existing.photos

            # raw_mls_data is replaced whole - but Bright only fills ClosePrice / CloseDate (and some
            # buyer-side fields) when the query itself asks for closed listings. A later read that
            # wasn't closed-scoped (the price-history pull, a tax-id lookup, a refresh) must never
            # wipe them off a sale we already have (Bright audit, 10/15).
            old_raw = existing.raw_mls_data if isinstance(existing.raw_mls_data, dict) else {}
            new_raw = mapped.get("raw_mls_data")
            if old_raw and isinstance(new_raw, dict):
                for k in _STICKY_SOLD_KEYS:
                    if new_raw.get(k) in (None, "") and old_raw.get(k) not in (None, ""):
                        new_raw[k] = old_raw[k]

            # Every field this read changes, old -> new (property_field_changes, 10/21).
            try:
                _log_field_changes(db, l_key, existing, mapped)
            except Exception:  # noqa: BLE001 - the change log never breaks the sync
                logger.warning("field change log not recorded", exc_info=True)

            # Update all columns
            for key, value in mapped.items():
                setattr(existing, key, value)
            existing.updated_at = datetime.now(timezone.utc)
            logger.debug(f"Updated existing property: {l_key} | {mapped['street_address']} | Status: {mapped['home_status']}")
        else:
            # For NEW properties, only add them if they are ACTIVE or COMING SOON
            # This prevents our DB from filling up with old CLOSED listings we never tracked
            status = raw_data.get("MlsStatus", "")
            is_active = status.startswith("ACTIVE-BRIGHT") or status.startswith("COMING SOON")
            # Pending / Active Under Contract homes are still on the market (My Listings, CMA pending
            # comps, IDX) - they used to be skipped when first seen that way, so a home that went
            # under contract before we mirrored it never showed up (Bright audit, 10/15). Mirrored
            # quietly: no "new home" alerts for a home that's already under contract.
            under_contract = status.startswith("PENDING") or status.startswith("ACTIVE UNDER CONTRACT")

            if not is_active and not under_contract and not allow_inactive_insert:
                logger.debug(f"Skipping discovery for NEW inactive property: {l_key} | Status: {status}")
                return None

            # New property to our system (Global Mirror). Only a newly-for-sale home is "new" to
            # Showcases / team alerts; anything else is just filled into the mirror.
            event_type = "NEW_GLOBAL" if is_active else "NEW_QUIET"
            existing = Property(**mapped)
            db.add(existing)
            try:
                from app.models.database import PropertyPriceEvent
                if is_active:  # a historical listing pulled in by the backfill isn't "listed today"
                    db.add(PropertyPriceEvent(listing_key=l_key, kind="listed", price=mapped.get("price"), status=mapped.get("home_status")))
                elif under_contract:
                    # First seen already under contract (10/21: these left no trace) - dated when Bright
                    # says the status changed, else today.
                    db.add(PropertyPriceEvent(listing_key=l_key, kind="status", price=mapped.get("price"), status=mapped.get("home_status"),
                                              observed_at=parse_dt(raw_data.get("StatusChangeTimestamp")) or datetime.now(timezone.utc)))
            except Exception:  # noqa: BLE001
                logger.warning("price history event not recorded", exc_info=True)
            logger.debug(f"Added NEW property: {l_key} | {mapped['street_address']} | Status: {mapped['home_status']}")
        
        try:
            await db.commit()
            await db.refresh(existing)
            return {
                "property_id": existing.id,
                "listing_key": l_key,
                "type": event_type,
                "old_price": old_price,
                "new_price": mapped["price"],
                "data": mapped,
                "rematch": rematch,
            }
        except Exception as e:
            logger.error(f"Failed to upsert property {l_key}: {e}")
            await db.rollback()
            return None

    async def _propagate_property_change(self, db: AsyncSession, event: Dict[str, Any]) -> int:
        """
        Identifies all active collections containing this property and schedules notifications.
        Also handles 'Discovery' for brand new properties.
        """
        prop_id = event["property_id"]
        l_key = event["listing_key"]
        count = 0

        # 1. DISCOVERY: If this is a NEW house to our system, find ALL matching collections
        # Showcases only keep working while their agent is on a live Showcase plan - a downgrade
        # to Basic or an ended subscription pauses new matches + client alerts (the showcase
        # itself is kept, so upgrading/resubscribing resumes it). Agents are warned about exactly
        # this before downgrading/cancelling (Settings > Billing).
        from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS

        if event["type"] == "NEW_GLOBAL" or event.get("rematch"):
            discovered_collections = [
                col for col in await self._discover_matching_collections(db, event["data"])
                if owner_has_access(col.owner, SHOWCASE_ACCESS_TIERS)
            ]
            for col in discovered_collections:
                # Link property to collection (Mark as NEW by setting added_at) - a Showcase that
                # already has this home (or the same house under another MLS number) is skipped.
                if not await self._link_property_to_collection(db, col.id, prop_id):
                    continue

                # Schedule 'New Property' notification
                changes = {"new_properties": [event["data"]], "price_drops": []}
                await self._schedule_combined_notification(db, col, changes)
                count += 1

            # My Team: if this brand-new listing belongs to an on-platform agent, let their
            # accepted team members know there's a new opportunity to host. NEW_GLOBAL only ever
            # fires once per listing_key for the life of that Property row (_upsert_property only
            # takes this branch on first insert; every later resync is UPDATE/PRICE_DROP), so this
            # can't repeat on its own - the notification-existence check below is an extra,
            # explicit guard against ever double-notifying for the same listing.
            if event["type"] == "NEW_GLOBAL":  # not on a re-match - the team already heard about it
                await self._notify_team_of_new_listing(db, l_key, event["data"])

        # 2. UPDATES: Find collections that already HAVE this property (Price Drops)
        query = (
            select(Collection)
            .join(collection_properties)
            .outerjoin(PropertyInteraction, and_(
                PropertyInteraction.collection_id == Collection.id,
                PropertyInteraction.property_id == prop_id
            ))
            .options(selectinload(Collection.owner))
            .where(
                and_(
                    collection_properties.c.property_id == prop_id,
                    Collection.status == 'ACTIVE',
                    or_(
                        PropertyInteraction.id == None,
                        PropertyInteraction.disliked == False
                    )
                )
            )
        )
        result = await db.execute(query)
        existing_collections = result.scalars().all()
        
        for col in existing_collections:
            if not owner_has_access(col.owner, SHOWCASE_ACCESS_TIERS):
                continue
            if event["type"] == "PRICE_DROP":
                # Include price change info in the data
                drop_data = {
                    **event["data"],
                    "old_price_raw": event.get("old_price"),
                    "new_price_raw": event.get("new_price")
                }
                changes = {"new_properties": [], "price_drops": [drop_data]}
                await self._schedule_combined_notification(db, col, changes, is_broadcast=True)
                count += 1
                
        await db.commit()
        return count

    async def _notify_team_of_new_listing(self, db: AsyncSession, listing_key: str, mapped: Dict[str, Any]) -> None:
        """Alerts an on-platform listing agent's accepted My Team members that they've got a new
        listing that might need a host. Only ever called from the NEW_GLOBAL (first-insert) branch
        above, so a resync of an already-known listing can't reach this - the per-recipient
        existence check below is an additional, explicit guard against ever sending the same
        (listing, member) alert twice."""
        from app.models.database import OpenHouseTeamMember, User

        owner_mls_id = mapped.get("list_agent_mls_id") or mapped.get("co_list_agent_mls_id")
        if not owner_mls_id:
            return
        owner_result = await db.execute(select(User).where(User.mls_id == owner_mls_id).order_by(User.test_copy_of_user_id.isnot(None), User.created_at))
        owner = owner_result.scalars().first()  # the real agent, not a test copy sharing the ID
        if not owner:
            return

        members_result = await db.execute(
            select(OpenHouseTeamMember.member_agent_id).where(
                OpenHouseTeamMember.owner_id == owner.id, OpenHouseTeamMember.status == "accepted",
            )
        )
        member_ids = [row[0] for row in members_result.all()]
        if not member_ids:
            return

        existing_result = await db.execute(
            select(Notification.agent_id).where(
                Notification.type == "TEAM_NEW_LISTING",
                Notification.reference_id == listing_key,
                Notification.agent_id.in_(member_ids),
            )
        )
        already_notified = {row[0] for row in existing_result.all()}

        street_address = mapped.get("street_address") or "A new listing"
        owner_name = f"{owner.first_name} {owner.last_name}".strip()
        for member_id in member_ids:
            if member_id in already_notified:
                continue
            try:
                db.add(Notification(
                    agent_id=member_id,
                    type="TEAM_NEW_LISTING",
                    reference_type="PROPERTY",
                    reference_id=listing_key,
                    title=f"{owner_name} has a new listing",
                    message=f"{street_address} just hit the market - might be worth hosting an open house.",
                    property_address=street_address,
                    link="/discovery",
                    is_read=False,
                ))
            except Exception:
                pass
        try:
            to_notify = [m for m in member_ids if m not in already_notified]
            if to_notify:
                from app.models.database import User as _User
                from app.services.agent_email_service import absolute, email_agent
                price = mapped.get("price") or mapped.get("list_price")
                for member in (await db.execute(select(_User).where(_User.id.in_(to_notify)))).scalars():
                    email_agent(member, "TEAM_NEW_LISTING", "team_new_listing", f"{owner_name} listed a new property: {street_address}",
                                {"owner_name": owner_name, "property_address": street_address,
                                 "list_price": f"${float(price):,.0f}" if price else None,
                                 "property_image": mapped.get("img_src"), "link": absolute("/discovery")})
        except Exception:
            logger.warning("team new-listing email failed", exc_info=True)

    async def _discover_matching_collections(self, db: AsyncSession, prop: Dict[str, Any]) -> List[Collection]:
        """
        The Matchmaker.
        High-performance query to find all active collections whose preferences 
        match this specific property.
        """
        # Extract property attributes for the reverse query
        price = prop.get("price") or 0
        beds = prop.get("bedrooms") or 0
        baths = prop.get("bathrooms") or 0
        city = prop.get("city")
        state = prop.get("state")
        township = prop.get("township")
        school_district = prop.get("school_district_name")
        h_type = prop.get("home_type")
        h_type_upper = h_type.upper() if h_type else "OTHER"
        lat = prop.get("latitude")
        lng = prop.get("longitude")

        # Reverse Query Logic
        stmt = (
            select(Collection)
            .join(CollectionPreferences)
            .options(
                selectinload(Collection.owner),
                joinedload(Collection.preferences)
            )
            .where(Collection.status == 'ACTIVE')
        )

        # 1. Geography Match (Exclusive Logic: Cities/Townships/Districts OR Radius)
        geo_conditions = []
        location_filters = []
        params = {}
        
        if city:
            params["city_name"] = city.upper()
            params["state_name"] = state.upper() if state else ""
            location_filters.append(text("""
                EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(
                        CASE 
                            WHEN jsonb_typeof(collection_preferences.cities) = 'array' 
                            THEN collection_preferences.cities 
                            ELSE '[]'::jsonb 
                        END
                    ) AS pref_city 
                    WHERE 
                        UPPER(TRIM(SPLIT_PART(pref_city, ',', 1))) = :city_name
                        AND (
                            TRIM(SPLIT_PART(pref_city, ',', 2)) = '' 
                            OR UPPER(TRIM(SPLIT_PART(pref_city, ',', 2))) = :state_name
                        )
                )
            """))
        
        if township:
            params["township_name"] = township.upper()
            params["state_name"] = state.upper() if state else ""
            location_filters.append(text("""
                EXISTS (
                    SELECT 1 
                    FROM jsonb_array_elements_text(
                        CASE 
                            WHEN jsonb_typeof(collection_preferences.townships) = 'array' 
                            THEN collection_preferences.townships 
                            ELSE '[]'::jsonb 
                        END
                    ) AS pref_town 
                    WHERE 
                        UPPER(TRIM(REGEXP_REPLACE(SPLIT_PART(pref_town, ',', 1), '\\s+(Township|Twp|Boro|Borough|City|Town)$', '', 'i'))) = :township_name
                        AND (
                            TRIM(SPLIT_PART(pref_town, ',', 2)) = '' 
                            OR UPPER(TRIM(SPLIT_PART(pref_town, ',', 2))) = :state_name
                        )
                )
            """))

        if school_district:
            params["sd_name"] = school_district.upper()
            params["sd_state"] = state.upper() if state else ""
            location_filters.append(text("""
                EXISTS (
                    SELECT 1 
                    FROM jsonb_array_elements_text(
                        CASE 
                            WHEN jsonb_typeof(collection_preferences.school_districts) = 'array' 
                            THEN collection_preferences.school_districts 
                            ELSE '[]'::jsonb 
                        END
                    ) AS sd 
                    WHERE 
                        UPPER(TRIM(SPLIT_PART(sd, ',', 1))) = :sd_name
                        AND (
                            TRIM(SPLIT_PART(sd, ',', 2)) = '' 
                            OR UPPER(TRIM(SPLIT_PART(sd, ',', 2))) = :sd_state
                        )
                )
            """))
        
        # Priority 1: If user has explicit location lists, only match those
        if location_filters:
            geo_conditions.append(or_(*location_filters))
        
        # Priority 2: Fallback to Radius only if no City/Township/District is provided
        if lat and lng:
            lat_offset, long_offset = get_lat_long_offsets(lat, 1.0) # Get offsets for 1 mile
            
            radius_condition = and_(
                # Ensure user hasn't specified other locations (The "Exclusive" part)
                or_(
                    CollectionPreferences.cities == None, 
                    and_(
                        func.jsonb_typeof(CollectionPreferences.cities) == 'array',
                        func.jsonb_array_length(CollectionPreferences.cities) == 0
                    ),
                    func.jsonb_typeof(CollectionPreferences.cities) != 'array'
                ),
                or_(
                    CollectionPreferences.townships == None, 
                    and_(
                        func.jsonb_typeof(CollectionPreferences.townships) == 'array',
                        func.jsonb_array_length(CollectionPreferences.townships) == 0
                    ),
                    func.jsonb_typeof(CollectionPreferences.townships) != 'array'
                ),
                or_(
                    CollectionPreferences.school_districts == None, 
                    and_(
                        func.jsonb_typeof(CollectionPreferences.school_districts) == 'array',
                        func.jsonb_array_length(CollectionPreferences.school_districts) == 0
                    ),
                    func.jsonb_typeof(CollectionPreferences.school_districts) != 'array'
                ),
                # Standard radius box
                CollectionPreferences.lat.isnot(None),
                CollectionPreferences.long.isnot(None),
                CollectionPreferences.diameter.isnot(None),
                func.abs(CollectionPreferences.lat - lat) <= (CollectionPreferences.diameter * lat_offset),
                func.abs(CollectionPreferences.long - lng) <= (CollectionPreferences.diameter * long_offset)
            )
            geo_conditions.append(radius_condition)
        
        if geo_conditions:
            stmt = stmt.where(or_(*geo_conditions))

        # 2. Financial & Size Match (with Smart Exemptions)
        # Types that don't always have traditional bed/bath data (Exempt from numeric filters)
        exempt_types = ['LAND', 'FARM', 'COMMERCIAL', 'OTHER']
        numeric_conditions = []

        if price > 0:
            numeric_conditions.append(and_(
                or_(CollectionPreferences.min_price == None, CollectionPreferences.min_price <= price),
                or_(CollectionPreferences.max_price == None, CollectionPreferences.max_price >= price)
            ))

        # Size Filters (apply even if property has 0 beds/baths)
        numeric_conditions.append(or_(CollectionPreferences.min_beds == None, CollectionPreferences.min_beds <= (beds or 0)))
        numeric_conditions.append(or_(CollectionPreferences.min_baths == None, CollectionPreferences.min_baths <= (baths or 0.0)))

        # Year Built Match
        year = prop.get("year_built")
        if year:
            numeric_conditions.append(and_(
                or_(CollectionPreferences.min_year_built == None, CollectionPreferences.min_year_built <= year),
                or_(CollectionPreferences.max_year_built == None, CollectionPreferences.max_year_built >= year)
            ))

        # Apply: (Is Exempt Type) OR (Matches all numeric filters)
        if h_type_upper in exempt_types:
            # If exempt, we only care about the price (if provided)
            if price > 0:
                stmt = stmt.where(and_(
                    or_(CollectionPreferences.min_price == None, CollectionPreferences.min_price <= price),
                    or_(CollectionPreferences.max_price == None, CollectionPreferences.max_price >= price)
                ))
        else:
            # If not exempt (Residential, Condo, etc.), must match all numeric filters
            stmt = stmt.where(and_(*numeric_conditions))

        # 3. Property Type Match - the same rule as the search itself (property_service): a Showcase
        # with home types picked gets only those; one with none picked ("any") gets every home FOR
        # SALE, never rentals/commercial. (Before, an "any" Showcase matched no new listing at all
        # when the listing had a type, and an untyped listing went to every Showcase.)
        # In standard search, Lot/Land includes both LAND and FARM
        type_flags = (CollectionPreferences.is_single_family, CollectionPreferences.is_town_house, CollectionPreferences.is_condo,
                      CollectionPreferences.is_multi_family, CollectionPreferences.is_lot_land, CollectionPreferences.is_farm,
                      CollectionPreferences.is_commercial, CollectionPreferences.is_apartment)
        no_types_picked = and_(*[func.coalesce(flag, False).is_(False) for flag in type_flags])
        type_match_conditions = []
        if h_type_upper == "SINGLE_FAMILY": type_match_conditions.append(CollectionPreferences.is_single_family == True)
        elif h_type_upper == "TOWNHOUSE": type_match_conditions.append(CollectionPreferences.is_town_house == True)
        elif h_type_upper == "CONDO": type_match_conditions.append(CollectionPreferences.is_condo == True)
        elif h_type_upper == "MULTI_FAMILY": type_match_conditions.append(CollectionPreferences.is_multi_family == True)
        elif h_type_upper == "LAND": type_match_conditions.append(CollectionPreferences.is_lot_land == True)
        elif h_type_upper == "FARM": type_match_conditions.append(or_(CollectionPreferences.is_lot_land == True, CollectionPreferences.is_farm == True))
        elif h_type_upper == "COMMERCIAL": type_match_conditions.append(CollectionPreferences.is_commercial == True)
        elif h_type_upper == "RESIDENTIAL_LEASE": type_match_conditions.append(CollectionPreferences.is_apartment == True)
        if h_type_upper not in ("COMMERCIAL", "RESIDENTIAL_LEASE"):
            type_match_conditions.append(no_types_picked)
        stmt = stmt.where(or_(*type_match_conditions))
        if price and price < 10000 and h_type_upper not in ("LAND", "FARM", "COMMERCIAL", "RESIDENTIAL_LEASE"):
            # Same as the search: a "home" under $10k (mis-tagged rental, timeshare week) only goes
            # to Showcases that asked for rentals/commercial.
            stmt = stmt.where(or_(CollectionPreferences.is_apartment == True, CollectionPreferences.is_commercial == True))

        result = await db.execute(stmt, params)
        collections = result.scalars().all()

        # 4. Circular Post-Filtering (Trim the Corners)
        # If the property has lat/long, ensure it's precisely within the radius of matching collections
        if lat and lng:
            refined_collections = []
            for col in collections:
                # If collection has a radius search, verify exact distance
                if col.preferences and col.preferences.lat and col.preferences.long and col.preferences.diameter:
                    # Skip circular check if collection matches via City/Township (inclusive logic)
                    matches_via_city = False
                    if city and col.preferences.cities:
                        city_names = [c.split(',')[0].strip().upper() for c in col.preferences.cities]
                        if city.upper() in city_names:
                            matches_via_city = True
                    
                    if not matches_via_city and township and col.preferences.townships:
                        # Simple check for townships
                        town_names = [t.split(',')[0].strip().upper() for t in col.preferences.townships]
                        if township.upper() in town_names:
                            matches_via_city = True

                    if matches_via_city:
                        refined_collections.append(col)
                    else:
                        # Must match via Radius - perform precise check
                        if is_within_distance(
                            col.preferences.lat, col.preferences.long, 
                            lat, lng, 
                            col.preferences.diameter
                        ):
                            refined_collections.append(col)
                else:
                    # Match via other criteria, keep it
                    refined_collections.append(col)
            
            return refined_collections

        return collections

    async def _link_property_to_collection(self, db: AsyncSession, collection_id: str, property_id: str) -> bool:
        """Silently links a property to a collection if not already linked. True = it was added now."""
        stmt = select(collection_properties).where(
            and_(
                collection_properties.c.collection_id == collection_id,
                collection_properties.c.property_id == property_id
            )
        )
        existing = await db.execute(stmt)
        if not existing.fetchone():
            # The same house under a second MLS number (often filed under the next town over) -
            # the Showcase already has it.
            from app.utils.same_home import is_same_home
            new_prop = await db.get(Property, property_id)
            if new_prop is not None:
                linked = (await db.execute(
                    select(Property).join(collection_properties, collection_properties.c.property_id == Property.id)
                    .where(collection_properties.c.collection_id == collection_id,
                           Property.street_address.ilike(f"{(new_prop.street_address or '').split(' ')[0]} %"))
                )).scalars().all()
                if any(is_same_home(new_prop, q) for q in linked):
                    return False
                # A Showcase holds at most SHOWCASE_MAX_HOMES for-sale homes (owner, 10/24). Full:
                # the new listing takes the place of the farthest / oldest-listed home nobody acted
                # on (quietly); if every home is protected it isn't added.
                from app.services.showcase_limit import make_room_for_new_listing
                if not await make_room_for_new_listing(db, collection_id, new_prop):
                    return False
            await db.execute(insert(collection_properties).values(
                collection_id=collection_id,
                property_id=property_id,
                added_at=datetime.now(timezone.utc) # Mark as NEW
            ))
            return True
        return False

    def _format_price_compact(self, price: float) -> str:
        """Formats price into a compact string like $750K or $2.1M"""
        if not price:
            return ""
        if price >= 1000000:
            val = price / 1000000
            # If it's a whole number, don't show decimal
            if val == int(val):
                return f"${int(val)}M"
            return f"${val:.1f}M"
        if price >= 1000:
            return f"${int(price / 1000)}K"
        return f"${int(price)}"

    def _generate_subject(self, featured: Dict[str, Any], template: str) -> str:
        """Generates a dynamic subject line to avoid spam filters"""
        import random
        
        price_raw = featured.get('price_raw') or featured.get('price') or 0
        price_compact = self._format_price_compact(float(price_raw))
        city = featured.get('city', '')
        street = featured.get('street_address', '')
        
        if template == "price_drop_alert":
            # For price drops, we want to be clear
            subjects = [
                f"Price drop: {street} in {city} is now {price_compact}",
                f"Great news: Price drop on {street}!",
                f"Price updated for {street}: {price_compact}",
                f"Price reduced for the home in {city}: {street}"
            ]
            return random.choice(subjects)
        
        # New Listing styles
        subjects = [
            f"{price_compact} listing just came up in {city}",
            f"New {price_compact} home in {city} you might like",
            f"New listing: {street} in {city} for {price_compact}",
            f"{street} just hit the market in {city} for {price_compact}"
        ]
        return random.choice(subjects)

    async def _schedule_combined_notification(self, db: AsyncSession, collection: Collection, changes: Dict[str, Any], is_broadcast: bool = False):
        """Queues a new match / price drop for the 4:30 PM daily digest (showcase_digest_service).
        Nothing is emailed here - one change used to mean one email to the buyer AND one to the
        agent, which flooded both when an agent had dozens of Showcases in one area. The rows are
        ScheduledEmails with status QUEUED (the minute-by-minute sender ignores them); _audience,
        _collection_id, _owner_id, _listing_key, _kind and the prices are what the digest reads."""
        from app.services.showcase_digest_service import QUEUED
        frontend_url = os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'http://localhost:3000'))
        new_count = len(changes.get("new_properties", []))
        drop_count = len(changes.get("price_drops", []))
        
        if new_count == 0 and drop_count == 0:
            return

        featured = (changes["new_properties"] + changes["price_drops"])[0]
        
        # Total property count helper
        total_count_query = select(func.count()).select_from(collection_properties).where(collection_properties.c.collection_id == collection.id)
        total_count_res = await db.execute(total_count_query)
        total_count = total_count_res.scalar() or 0

        # Create full address string
        full_address = f"{featured.get('street_address')}, {featured.get('city')}, {featured.get('state')} {featured.get('zipcode', '')}".strip()

        common_vars = {
            "collection_name": collection.name,
            "visitor_name": collection.visitor_name or "Valued Visitor",
            "new_count": new_count,
            "drop_count": drop_count,
            "total_count": total_count,
            "property_address": full_address,
            "property_image": featured.get('img_src'),
            "property_price": f"${featured.get('price', 0):,}",
            "property_beds": featured.get('bedrooms'),
            "property_baths": featured.get('bathrooms'),
            "property_sqft": featured.get('living_area'),
            "today_date": datetime.now(timezone.utc).strftime("%m/%d/%Y")
        }

        digest_meta = {
            "_collection_id": collection.id, "_owner_id": collection.owner_id,
            "_listing_key": str(featured.get("listing_key") or ""),
            "_kind": "new" if new_count else "drop",
            "_old_price": featured.get("old_price_raw"), "_new_price": featured.get("new_price_raw"),
            "_street": featured.get("street_address"), "_city": featured.get("city"), "_state": featured.get("state"),
            "_seq": time.time_ns(),  # exact order (several changes can share one transaction timestamp)
        }

        # 1. Visitor Email (queued for the daily digest)
        if collection.visitor_email and getattr(collection, 'notify_visitor', True):
            # Check if visitor is blacklisted
            if await BlacklistService.is_blacklisted(db, collection.visitor_email):
                logger.info(f"Skipping scheduled email for blacklisted visitor: {collection.visitor_email}")
            else:
                template = "showcase_daily_digest"  # queued for the 4:30 digest, never sent on its own
                
                # Calculate price drop variables if applicable
                old_p = featured.get("old_price_raw")
                new_p = featured.get("new_price_raw")
                savings = 0
                if old_p and new_p:
                    savings = old_p - new_p

                # IDX agents get this client-facing link on their own domain, same as every other
                # showcase link/QR code they hand out (see resolve_owner_public_base_url).
                showcase_base_url = await resolve_owner_public_base_url(db, collection.owner) if collection.owner else frontend_url
                from app.services.showcase_links import showcase_path
                visitor_vars = {
                    **common_vars,
                    "collection_link": f"{showcase_base_url}{await showcase_path(collection)}",
                    "recipient_name": ((collection.visitor_name or "").strip().split(" ")[0].title() or "there"),
                    "agent_name": f"{collection.owner.first_name} {collection.owner.last_name}" if collection.owner else "Your Agent",
                    "agent_email": collection.owner.email if collection.owner else "",
                    "agent_phone": await agent_public_phone(db, collection.owner),
                    "old_price": f"${old_p:,.0f}" if old_p else None,
                    "new_price": f"${new_p:,.0f}" if new_p else None,
                    "savings": f"${savings:,.0f}" if savings > 0 else None,
                    "Unsub": f"{frontend_url}/unsubscribe?email={quote(collection.visitor_email, safe='')}"
                }
                
                subject = self._generate_subject(featured, template)
                
                db.add(ScheduledEmail(
                    recipient_email=collection.visitor_email,
                    subject=subject,
                    template_name=template,
                    template_variables={**visitor_vars, **digest_meta, "_audience": "buyer"},
                    status=QUEUED,
                    scheduled_for=datetime.now(timezone.utc)
                ))

        # 2. Agent Email
        if collection.owner and collection.owner.email and getattr(collection, 'notify_agent', True):
            agent_vars = {
                **common_vars, 
                "collection_link": f"{frontend_url}/showcases?showcase={collection.id}",
                "recipient_name": collection.owner.first_name
            }
            db.add(ScheduledEmail(
                recipient_email=collection.owner.email,
                subject=f"Showcase Updated: {collection.visitor_name or 'Visitor'} - {collection.name}",
                template_name="showcase_daily_digest_agent",
                template_variables={**agent_vars, **digest_meta, "_audience": "agent"},
                status=QUEUED,
                scheduled_for=datetime.now(timezone.utc)
            ))

        # 3. In-app alert: none per change any more - the digest adds ONE bell alert per agent a day.

