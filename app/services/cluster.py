"""How several copies of the API (WEB_WORKERS > 1) agree with each other - all through Postgres, the
one thing every copy shares (owner, 11/14: "ensure people can't do 3 requests when it's supposed to be
one"). Anything that used to be a note in one copy's memory - "this video is being made", "two chats at
once", "8 messages a minute" - lives here instead, so it holds no matter which copy a request lands on.

Three tools (the same ones big sites use, with Postgres in the role Redis usually plays):

- db_lock / try_db_lock - a Postgres advisory lock on its own connection, for SHORT "one at a time"
  steps. Postgres frees it by itself if the copy dies.
- leases (table cluster_leases) - "this is running / this is taken" for longer work. The holder renews
  it while it works (lease() does that for you); a copy that dies stops renewing, so its lease frees
  itself once its time-to-live passes. An optional per-owner check (e.g. "at most 2 chats at once") is
  read and taken in one locked step, so two copies can never both squeeze into the last slot.
- rate_hits / rate_hit (table rate_windows) - speed limits counted across every copy (sliding-window
  counter: this window's count plus the overlapping part of the last one).

Every call runs in its own short database session and commits at once - it never joins, commits or
rolls back the caller's own transaction.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Iterable, Optional

from sqlalchemy import text

from app.database import AsyncSessionLocal, engine

logger = logging.getLogger(__name__)


def lock_id(name: str) -> int:
    """A stable signed 64-bit advisory-lock number for a name."""
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)


# ---------------------------------------------------------------------------------------------------
# Short locks
# ---------------------------------------------------------------------------------------------------

async def _unlock_and_close(conn, key: int) -> None:
    try:
        try:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            await conn.commit()
        except asyncio.CancelledError:
            await conn.invalidate()   # never hand a connection that may still hold the lock back to the pool
            raise
        except Exception:  # noqa: BLE001 - same: throw the connection away, the lock dies with it
            await conn.invalidate()
    finally:
        await conn.close()


@asynccontextmanager
async def db_lock(name: str, *, timeout: Optional[float] = None) -> AsyncIterator[None]:
    """Wait for the named lock (raises TimeoutError after `timeout` seconds, if given), run the block,
    give it back. Hold it only for short steps - it keeps one database connection while held."""
    key = lock_id(name)
    conn = await engine.connect()
    try:
        if timeout is None:
            await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        else:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while not (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar():
                if loop.time() >= deadline:
                    raise TimeoutError(f"lock {name!r} still busy after {timeout}s")
                await asyncio.sleep(0.05)
        await conn.commit()   # end the implicit transaction; the session-level lock stays
    except BaseException:
        # Cancelled or failed while asking: the server may have granted it anyway - throw the
        # connection away so the lock dies with it instead of riding back into the pool.
        try:
            await conn.invalidate()
        finally:
            await conn.close()
        raise
    try:
        yield
    finally:
        await _unlock_and_close(conn, key)


@asynccontextmanager
async def try_db_lock(name: str) -> AsyncIterator[bool]:
    """Take the named lock only if it's free right now. Yields True (held for the block) or False."""
    key = lock_id(name)
    conn = await engine.connect()
    try:
        got = bool((await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar())
        await conn.commit()
    except BaseException:
        try:
            await conn.invalidate()
        finally:
            await conn.close()
        raise
    if not got:
        await conn.close()
        yield False
        return
    try:
        yield True
    finally:
        await _unlock_and_close(conn, key)


# ---------------------------------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------------------------------

Allow = Callable[[int, float], bool]   # (other live leases' count, their summed amount) -> may this one start?


async def acquire(kind: str, key: str, ttl: float, *, owner_id: Optional[str] = None, amount: float = 0.0,
                  allow: Optional[Allow] = None) -> Optional[str]:
    """Take the (kind, key) lease for `ttl` seconds. Returns its token, or None if a live lease already
    holds that key - or if `allow` (given the owner's OTHER live leases of this kind) says no. With
    `allow`, the check and the take happen under one per-(kind, owner) lock, so copies can't race."""
    token = uuid.uuid4().hex
    async with AsyncSessionLocal() as db:
        if allow is not None:
            await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_id(f"lease:{kind}:{owner_id}")})
            n, total = (await db.execute(text(
                "SELECT count(*), coalesce(sum(amount), 0) FROM cluster_leases "
                "WHERE kind = :kind AND owner_id IS NOT DISTINCT FROM :o AND key <> :key AND expires_at > clock_timestamp()"),
                {"kind": kind, "o": owner_id, "key": key})).one()
            if not allow(int(n), float(total)):
                await db.rollback()
                return None
        got = (await db.execute(text(
            "INSERT INTO cluster_leases (kind, key, token, owner_id, amount, expires_at) "
            "VALUES (:kind, :key, :token, :o, :amt, clock_timestamp() + make_interval(secs => :ttl)) "
            "ON CONFLICT (kind, key) DO UPDATE SET token = EXCLUDED.token, owner_id = EXCLUDED.owner_id, "
            "  amount = EXCLUDED.amount, expires_at = EXCLUDED.expires_at, created_at = now() "
            "WHERE cluster_leases.expires_at <= clock_timestamp() "
            "RETURNING token"),
            {"kind": kind, "key": key, "token": token, "o": owner_id, "amt": float(amount), "ttl": float(ttl)})).scalar()
        await db.commit()
    return got


async def renew(kind: str, key: str, token: str, ttl: float) -> bool:
    """Push the lease's expiry out again. False = it isn't ours any more (it expired and was taken)."""
    async with AsyncSessionLocal() as db:
        ok = (await db.execute(text(
            "UPDATE cluster_leases SET expires_at = clock_timestamp() + make_interval(secs => :ttl) "
            "WHERE kind = :kind AND key = :key AND token = :token RETURNING 1"),
            {"kind": kind, "key": key, "token": token, "ttl": float(ttl)})).scalar()
        await db.commit()
    return bool(ok)


async def release(kind: str, key: str, token: Optional[str] = None) -> None:
    """Give the lease back (only if it's still ours, when a token is given)."""
    async with AsyncSessionLocal() as db:
        if token is None:
            await db.execute(text("DELETE FROM cluster_leases WHERE kind = :kind AND key = :key"), {"kind": kind, "key": key})
        else:
            await db.execute(text("DELETE FROM cluster_leases WHERE kind = :kind AND key = :key AND token = :token"),
                             {"kind": kind, "key": key, "token": token})
        await db.commit()


async def alive(kind: str, key: str) -> bool:
    """Is a live lease holding this key (in any copy)?"""
    async with AsyncSessionLocal() as db:
        return bool((await db.execute(text(
            "SELECT 1 FROM cluster_leases WHERE kind = :kind AND key = :key AND expires_at > clock_timestamp()"),
            {"kind": kind, "key": key})).scalar())


async def alive_keys(kind: str, keys: Iterable[str]) -> set[str]:
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        return set()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT key FROM cluster_leases WHERE kind = :kind AND key = ANY(:keys) AND expires_at > clock_timestamp()"),
            {"kind": kind, "keys": keys})).all()
    return {r[0] for r in rows}


async def _keep_alive(kind: str, key: str, token: str, ttl: float) -> None:
    every = max(1.0, ttl / 3)
    while True:
        await asyncio.sleep(every)
        try:
            if not await renew(kind, key, token, ttl):
                logger.warning("cluster lease lost while still working", extra={"kind": kind, "key": key})
                return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - try again next beat; the lease has ttl to spare
            logger.warning("cluster lease renew failed", exc_info=True, extra={"kind": kind, "key": key})


@asynccontextmanager
async def lease(kind: str, key: str, ttl: float = 60, **kw) -> AsyncIterator[Optional[str]]:
    """`async with lease(...) as token:` - token is None if it's taken (skip the work). While the block
    runs the lease is renewed every ttl/3 seconds; it's given back when the block ends, however it ends.
    If this copy dies mid-block, the lease frees itself within `ttl` seconds."""
    token = await acquire(kind, key, ttl, **kw)
    if token is None:
        yield None
        return
    beat = asyncio.create_task(_keep_alive(kind, key, token, ttl))
    try:
        yield token
    finally:
        beat.cancel()
        try:
            await release(kind, key, token)
        except Exception:  # noqa: BLE001 - it expires by itself within ttl
            logger.warning("cluster lease release failed (it expires by itself)", exc_info=True, extra={"kind": kind, "key": key})


class LeaseBusy(Exception):
    """wait_lease gave up: someone else still holds it."""


@asynccontextmanager
async def wait_lease(kind: str, key: str, *, timeout: float = 60, ttl: float = 60) -> AsyncIterator[str]:
    """Take turns: wait (up to `timeout` s, else LeaseBusy) until the lease is free, hold it for the block.
    Unlike db_lock this keeps NO database connection while held or while waiting - safe around slow
    outside calls (a calendar, a video provider) without eating into the connection pool."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    delay = 0.05
    while True:
        token = await acquire(kind, key, ttl)
        if token is not None:
            break
        if loop.time() >= deadline:
            raise LeaseBusy(f"{kind}:{key}")
        await asyncio.sleep(delay)
        delay = min(0.5, delay * 1.5)
    beat = asyncio.create_task(_keep_alive(kind, key, token, ttl))
    try:
        yield token
    finally:
        beat.cancel()
        try:
            await release(kind, key, token)
        except Exception:  # noqa: BLE001 - it expires by itself within ttl
            logger.warning("cluster lease release failed (it expires by itself)", exc_info=True, extra={"kind": kind, "key": key})


def start_keep_alive(kind: str, key: str, token: str, ttl: float) -> asyncio.Task:
    """For a lease taken in one place and given back in another (e.g. a background task): renews it
    until the returned task is cancelled."""
    return asyncio.get_running_loop().create_task(_keep_alive(kind, key, token, ttl))


# ---------------------------------------------------------------------------------------------------
# Speed limits
# ---------------------------------------------------------------------------------------------------

async def rate_hits(limits: list[tuple[str, int, int]]) -> bool:
    """Count one request against every (key, limit, window_seconds) - all or nothing. Returns False
    (nothing counted) if any limit is already reached across all copies. Fails OPEN (True) if the
    database can't be asked, like the old per-copy limiter never stopping a request on its own error."""
    if not limits:
        return True
    try:
        async with AsyncSessionLocal() as db:
            for name in sorted({k for k, _, _ in limits}):   # one order everywhere: no deadlocks
                await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_id("rate:" + name)})
            now = float((await db.execute(text("SELECT extract(epoch FROM clock_timestamp())"))).scalar())
            plan = []
            for key, limit, window in limits:
                window = max(1, int(window))
                cur = int(now // window) * window
                prev = cur - window
                rows = dict((await db.execute(text(
                    "SELECT window_start, count FROM rate_windows WHERE key = :k AND window_start IN (:c, :p)"),
                    {"k": key, "c": cur, "p": prev})).all())
                used = rows.get(prev, 0) * (1 - (now - cur) / window) + rows.get(cur, 0)
                if used + 1 > limit + 1e-9:
                    await db.rollback()
                    return False
                plan.append((key, cur, prev))
            for key, cur, prev in plan:
                await db.execute(text(
                    "INSERT INTO rate_windows (key, window_start, count) VALUES (:k, :c, 1) "
                    "ON CONFLICT (key, window_start) DO UPDATE SET count = rate_windows.count + 1"),
                    {"k": key, "c": cur})
                await db.execute(text("DELETE FROM rate_windows WHERE key = :k AND window_start < :p"), {"k": key, "p": prev})
            await db.commit()
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning("shared speed limit check failed - letting the request through", exc_info=True)
        return True


async def rate_hit(key: str, limit: int, window_seconds: int) -> bool:
    return await rate_hits([(key, limit, window_seconds)])


async def rate_count(key: str, window_seconds: int) -> float:
    """How full the window is right now (same estimate rate_hits uses), without counting anything."""
    async with AsyncSessionLocal() as db:
        now = float((await db.execute(text("SELECT extract(epoch FROM clock_timestamp())"))).scalar())
        cur = int(now // window_seconds) * window_seconds
        prev = cur - window_seconds
        rows = dict((await db.execute(text(
            "SELECT window_start, count FROM rate_windows WHERE key = :k AND window_start IN (:c, :p)"),
            {"k": key, "c": cur, "p": prev})).all())
    return rows.get(prev, 0) * (1 - (now - cur) / window_seconds) + rows.get(cur, 0)


async def cleanup() -> dict:
    """Hourly (scheduler): drop long-expired leases and old speed-limit windows."""
    async with AsyncSessionLocal() as db:
        leases = (await db.execute(text("DELETE FROM cluster_leases WHERE expires_at < clock_timestamp() - interval '1 hour'"))).rowcount
        windows = (await db.execute(text(
            "DELETE FROM rate_windows WHERE window_start < extract(epoch FROM clock_timestamp()) - 2 * 86400"))).rowcount
        await db.commit()
    return {"leases": leases, "windows": windows}


async def scheduled_cleanup() -> None:
    try:
        out = await cleanup()
        if out["leases"] or out["windows"]:
            logger.info("cluster cleanup", extra=out)
    except Exception:  # noqa: BLE001
        logger.warning("cluster cleanup failed", exc_info=True)
