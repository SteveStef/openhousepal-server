"""MLS mirror columns - the searched Bright fields as real columns (owner, 11/13).

Sold-home searches, counts and sorts used to read raw_mls_data (the ~10 GB Bright payload store) for
every candidate row just to learn its CloseDate / ClosePrice: a 5-town, 1-year lookup took 5.6 s on
live and the blog writer's first step timed out. The Property.mls_* columns hold the same values,
filled by the database itself (trigger properties_mls_mirror_trg, migration 05333178db78), so:

* SQL readers call the expression helpers here (close_date(), close_price(), internet_display_ok()
  ...). Until every existing row has been filled and the indexes are built (ready()), they return
  the exact raw_mls_data expression the code always used; afterwards, the column.
* Python readers call value(p, "ClosePrice") - the column when that row is mirrored, else the
  payload - so a row's answer never changes, mirrored or not.
* scheduled_mls_mirror_fill (every 10 min, one worker) fills old rows in small batches and then
  builds the indexes CONCURRENTLY - nothing heavy ever runs in a migration or on a deploy.

OHP_MLS_MIRROR=on|off forces the switch (tests and before/after comparisons).
"""
import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Numeric, cast, func, text

from app.config.logging import get_logger

logger = get_logger(__name__)

# column -> (Bright key, kind) - keep identical to migration 05333178db78 FIELDS
FIELDS = [
    ("mls_close_date", "CloseDate", "text"),
    ("mls_close_price", "ClosePrice", "num"),
    ("mls_purchase_contract_date", "PurchaseContractDate", "text"),
    ("mls_expected_on_market_date", "ExpectedOnMarketDate", "text"),
    ("mls_internet_display", "InternetEntireListingDisplayYN", "text"),
    ("mls_internet_address_display", "InternetAddressDisplayYN", "text"),
    ("mls_association_fee2", "AssociationFee2", "num"),
    ("mls_association_fee2_frequency", "AssociationFee2Frequency", "text"),
    ("mls_buyer_financing", "BuyerFinancing", "json"),
    ("mls_property_condition", "PropertyCondition", "json"),
]
COLUMN_OF = {key: col for col, key, _ in FIELDS}
KIND_OF = {key: kind for _, key, kind in FIELDS}
_FN = {"text": "ohp_mls_text", "num": "ohp_mls_num", "json": "ohp_mls_json"}

# Built after the fill (CONCURRENTLY). The first one makes each fill batch a quick index lookup.
UNMIRRORED_INDEX = ("ix_properties_mls_unmirrored", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_properties_mls_unmirrored ON properties (id) WHERE mls_mirrored_at IS NULL")
INDEXES = [
    ("ix_properties_mls_close_date", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_properties_mls_close_date ON properties (mls_close_date)"),
    # Showcase / search "school district" filters are ILIKE - a btree can't serve them (355 MB read per change)
    ("ix_properties_school_district_trgm", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_properties_school_district_trgm ON properties USING gin (school_district_name gin_trgm_ops)"),
    # "19087" in the IDX search box looked its towns up with zipcode LIKE '19087%' - a whole-table read
    # (1.3 GB) on every ZIP search; the plain zipcode index can't serve LIKE outside the C collation.
    ("ix_properties_zipcode_pattern", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_properties_zipcode_pattern ON properties (zipcode varchar_pattern_ops)"),
    # Map-box searches (CMA comps and price map, Showcase radius / drawn area, Open House discovery):
    # three single-column indexes ANDed into a bitmap that spilled to "lossy" on live's 4 MB work_mem
    # and read ~50k pages per CMA. in_box() below lets one map index find just the homes in the box.
    ("ix_properties_geo_point", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_properties_geo_point ON properties USING gist (point(longitude, latitude))"),
    # The IDX search box's town list (every town with homes on the market) read the whole table every
    # 6 hours per worker; with this it reads only the index.
    ("ix_properties_status_state_city", "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_properties_status_state_city ON properties (home_status, state, city)"),
]


def in_box(lat_lo, lat_hi, lng_lo, lng_hi, model=None):
    """point(longitude, latitude) inside the box - the SAME homes as the four latitude/longitude range
    checks it's always ANDed with (Postgres' box test is, if anything, a hair more inclusive, so the
    exact range checks still decide), but the map index ix_properties_geo_point can find them.
    Nothing until ready() (the index is built by the background job)."""
    from sqlalchemy import true
    if not ready():
        return true()
    m = _model(model)
    return func.point(m.longitude, m.latitude).op("<@")(
        func.box(func.point(float(lng_lo), float(lat_lo)), func.point(float(lng_hi), float(lat_hi))))


def zip_prefix(column, zip_code: str):
    """column LIKE 'zip%' written so the planner can use ix_properties_zipcode_pattern (a bound
    parameter hides the prefix from a generic plan). Only for a plain 5-digit ZIP."""
    import re
    from sqlalchemy import literal_column
    if ready() and re.fullmatch(r"\d{5}", zip_code or ""):
        return column.like(literal_column(f"'{zip_code}%'"))
    return column.like(f"{zip_code}%")

# ---------------------------------------------------------------------------------------------
# The switch

_state = {"ready": False, "checked": 0.0}
_RECHECK_S = 300


def ready() -> bool:
    """True once every row is mirrored and the indexes exist - then readers use the columns."""
    forced = os.getenv("OHP_MLS_MIRROR", "").lower()
    if forced in ("on", "1", "true"):
        return True
    if forced in ("off", "0", "false"):
        return False
    return _state["ready"]


async def _index_valid(conn, name: str) -> Optional[bool]:
    """None = missing, False = a failed CONCURRENTLY build left it invalid, True = usable."""
    row = (await conn.execute(text(
        "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid WHERE c.relname = :n"), {"n": name})).first()
    return None if row is None else bool(row[0])


async def refresh_ready(force: bool = False) -> bool:
    """Re-check (at most every 5 minutes; once ready it stays ready for this process)."""
    if _state["ready"] or (not force and time.monotonic() - _state["checked"] < _RECHECK_S):
        return _state["ready"]
    _state["checked"] = time.monotonic()
    from app.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            trig = (await db.execute(text(
                "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = 'properties' AND t.tgname = 'properties_mls_mirror_trg' AND t.tgenabled <> 'D'"))).first()
            if not trig or not await _index_valid(db, UNMIRRORED_INDEX[0]):
                return False
            for name, _ in INDEXES:
                if not await _index_valid(db, name):
                    return False
            # the partial index makes this instant: it only holds unfilled rows
            left = (await db.execute(text("SELECT 1 FROM properties WHERE mls_mirrored_at IS NULL LIMIT 1"))).first()
            _state["ready"] = left is None
    except Exception:
        logger.warning("mls mirror readiness check failed", exc_info=True)
    return _state["ready"]


# ---------------------------------------------------------------------------------------------
# SQL expression helpers - the column when ready(), else exactly the raw_mls_data expression.

def _model(model):
    if model is None:
        from app.models.database import Property
        return Property
    return model


def text_field(key: str, model=None):
    """raw_mls_data->>'Key' (text)."""
    m = _model(model)
    return getattr(m, COLUMN_OF[key]) if ready() else m.raw_mls_data[key].astext


def json_field(key: str, model=None):
    """raw_mls_data->'Key' (JSON)."""
    m = _model(model)
    return getattr(m, COLUMN_OF[key]) if ready() else m.raw_mls_data[key]


def col(column, key: str, model=None, as_json: bool = False):
    """A Bright key that ALREADY has its own column, filled from the same payload by the sync's
    mapper (app/utils/mls_mapper.py) - e.g. Property.price = ListPrice. Checked identical row for row
    (0 differences on 5,121 sampled live homes, 11/13). The column once ready(), else the payload
    expression the code used. NOTE the column is typed (float/int/bool/timestamp) where the payload
    text was a string - only use where the reader parses it (float(), str(), == 'true' on str())."""
    m = _model(model)
    if ready():
        return column
    return m.raw_mls_data[key] if as_json else m.raw_mls_data[key].astext


def close_date(model=None):
    """CloseDate as text 'YYYY-MM-DD' - compares, sorts and maxes exactly like the payload text."""
    return text_field("CloseDate", model)


def close_price(model=None):
    """ClosePrice as NUMERIC (NULL when missing or blank)."""
    m = _model(model)
    return m.mls_close_price if ready() else cast(func.nullif(m.raw_mls_data["ClosePrice"].astext, ""), Numeric)


def num_field(key: str, model=None):
    """A numeric Bright field as NUMERIC (NULL when missing or blank)."""
    m = _model(model)
    return getattr(m, COLUMN_OF[key]) if ready() else cast(func.nullif(m.raw_mls_data[key].astext, ""), Numeric)


def internet_display_ok(model=None):
    """The seller did not opt this listing out of internet display (missing = allowed)."""
    return func.coalesce(text_field("InternetEntireListingDisplayYN", model), "true") != "false"


def internet_address_ok(model=None):
    return func.coalesce(text_field("InternetAddressDisplayYN", model), "true") != "false"


def defer_raw(model=None):
    """Loader options that skip the heavy payload - only once every row is mirrored (before that,
    a row's mirrored values may be missing and readers fall back to the payload)."""
    if not ready():
        return []
    from sqlalchemy.orm import defer
    return [defer(_model(model).raw_mls_data)]


def selectin_without_raw(relationship):
    """selectinload(relationship) for a relationship to Property, skipping the payload once ready()
    (Showcase homes: every list of them loaded each home's whole Bright record)."""
    from sqlalchemy.orm import selectinload
    opt = selectinload(relationship)
    return opt.defer(_model(None).raw_mls_data) if ready() else opt


# ---------------------------------------------------------------------------------------------
# Python helpers - for a loaded row

def _mirrored(p) -> bool:
    try:
        return getattr(p, "mls_mirrored_at", None) is not None
    except Exception:
        return False


def _from_column(key: str, v: Any) -> Any:
    """Column value -> what raw_mls_data.get(key) would have returned."""
    if v is None:
        return None
    kind = KIND_OF[key]
    if kind == "text" and key.endswith("YN"):
        return {"true": True, "false": False}.get(v, v)
    if kind == "num":
        return float(v)
    return v


def value(p, key: str, default: Any = None) -> Any:
    """raw_mls_data.get(key) for a Property row (or anything with the same attributes): from the
    mirror column when the row is mirrored, else from the payload."""
    if key in COLUMN_OF and _mirrored(p):
        v = _from_column(key, getattr(p, COLUMN_OF[key], None))
        return default if v is None else v
    raw = getattr(p, "raw_mls_data", None)
    raw = raw if isinstance(raw, dict) else {}
    return raw.get(key, default)


def hosting_fields(p) -> dict:
    """The only payload field the open-house hosting rules read (open_house_manager_service.
    hosting_window): Coming Soon's go-live date - so a list of homes needn't load the payload."""
    return {"ExpectedOnMarketDate": value(p, "ExpectedOnMarketDate")}


# ---------------------------------------------------------------------------------------------
# The background fill

_LOCK_KEY = 718_204_117
# One-time fill at full speed (owner 11/13: "nobody is using the website right now - do it all now"):
# 2,000 rows a batch, almost no pause, ~9 of every 10 minutes until done. Afterwards the job only
# re-checks readiness, so these never matter again.
_BATCH = int(os.getenv("OHP_MLS_MIRROR_BATCH", "2000"))
_RUN_SECONDS = int(os.getenv("OHP_MLS_MIRROR_RUN_SECONDS", "540"))
_PAUSE_S = float(os.getenv("OHP_MLS_MIRROR_PAUSE", "0.1"))


def fill_sql(batch_limit: bool = True) -> str:
    """One batch: the same functions the trigger uses, straight from each row's own payload."""
    sets = ", ".join(f"{col} = {_FN[kind]}(raw_mls_data, '{key}')" for col, key, kind in FIELDS)
    pick = "SELECT id FROM properties WHERE mls_mirrored_at IS NULL LIMIT :n FOR UPDATE SKIP LOCKED"
    return f"UPDATE properties SET {sets}, mls_mirrored_at = now() WHERE id IN ({pick})"


def _quiet_hours() -> bool:
    """The nightly pg_dump runs at 00:00 UTC (see feedback_no_migrations_during_backup) - stay off
    the disk from 23:30 to 01:30 UTC."""
    now = datetime.now(timezone.utc)
    minutes = now.hour * 60 + now.minute
    return minutes >= 23 * 60 + 30 or minutes < 90


async def _build_index(name: str, sql: str) -> bool:
    """CREATE INDEX CONCURRENTLY (outside a transaction, no write blocking). A failed earlier build
    leaves an INVALID index behind: drop that and build again."""
    from app.database import engine
    async with engine.connect() as raw:
        conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("SET statement_timeout = 0"))
        state = await _index_valid(conn, name)
        if state is True:
            return True
        if state is False:
            await conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
        t0 = time.monotonic()
        await conn.execute(text(sql))
        logger.info("mls mirror index built", extra={"index": name, "seconds": round(time.monotonic() - t0, 1)})
        return bool(await _index_valid(conn, name))


async def fill_step(run_seconds: Optional[float] = None, batch: Optional[int] = None) -> dict:
    """One scheduled run: fill old rows for up to `run_seconds`, then (once none are left) build
    the remaining indexes. Returns what it did."""
    from app.database import AsyncSessionLocal, engine
    stats = {"filled": 0, "batches": 0, "done": False, "indexes": []}
    # The one-worker lock lives on ONE dedicated connection for the whole run (autocommit, so no
    # long transaction). It was taken through a session that handed its connection back to the pool
    # on commit, then "released" on whatever connection came next - so on live the lock stayed held
    # by an idle pooled connection and every later run said "another worker" (11/13). If this
    # connection dies (a database restart), Postgres drops the lock with it.
    async with engine.connect() as raw_lock:
        lockdb = await raw_lock.execution_options(isolation_level="AUTOCOMMIT")
        if not (await lockdb.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY})).scalar():
            return {**stats, "skipped": "another worker"}
        try:
            trig = (await lockdb.execute(text(
                "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = 'properties' AND t.tgname = 'properties_mls_mirror_trg'"))).first()
            if not trig:
                return {**stats, "skipped": "migration not applied"}
            if not await _build_index(*UNMIRRORED_INDEX):
                return {**stats, "skipped": "unmirrored index failed"}
            deadline = time.monotonic() + (run_seconds if run_seconds is not None else _RUN_SECONDS)
            sql = text(fill_sql())
            while time.monotonic() < deadline:
                async with AsyncSessionLocal() as db:
                    res = await db.execute(sql, {"n": batch or _BATCH})
                    await db.commit()
                n = res.rowcount or 0
                stats["filled"] += n
                stats["batches"] += 1
                if n == 0:
                    stats["done"] = True
                    break
                await asyncio.sleep(_PAUSE_S)
            if stats["done"]:
                for name, ddl in INDEXES:
                    if await _build_index(name, ddl):
                        stats["indexes"].append(name)
                if not _state["ready"] and len(stats["indexes"]) == len(INDEXES):
                    # Fresh planner statistics (the expression map index has none until ANALYZE)
                    # before the readers switch over - a sampled read, not a table scan.
                    async with engine.connect() as raw:
                        conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
                        await conn.execute(text("SET statement_timeout = 0"))
                        await conn.execute(text("ANALYZE properties"))
                    stats["analyzed"] = True
                await refresh_ready(force=True)
        finally:
            try:
                await lockdb.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            except Exception:  # connection already gone - the lock went with it
                pass
    return stats


async def scheduled_mls_mirror_fill():
    """Every 10 minutes until the fill is done (then it only re-checks readiness)."""
    if ready() and os.getenv("OHP_MLS_MIRROR", "") == "":
        return
    if _quiet_hours():
        return
    try:
        stats = await fill_step()
        if stats.get("filled") or stats.get("indexes") or stats.get("skipped"):
            logger.info("mls mirror fill", extra={"stats": stats})
    except Exception:
        logger.warning("mls mirror fill failed", exc_info=True)
    await refresh_ready()
