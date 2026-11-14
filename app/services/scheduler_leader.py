"""Only ONE copy of the API runs the scheduled jobs (owner, 11/14).

The live server can run several copies of the API side by side (WEB_WORKERS in the Dockerfile) so one
slow request never holds up everyone else. Every copy builds the same job list, but only the copy that
holds a Postgres advisory lock starts it - MLS syncs, reminder emails and digests must never run twice.

- The lock lives on ONE dedicated database connection, held open for as long as this copy is the
  leader (an advisory lock belongs to its connection - taking it through the normal connection pool
  would hand it back with the connection and lose track of it; see project memory "MLS mirror lock").
- The others try again every minute, so if the leader's process dies (its connection closes and the
  lock is released by Postgres) another copy takes over within about a minute.
- The leader checks its connection every minute; if it's gone, it stops its jobs and goes back to
  waiting, so a database restart can never leave two copies running jobs.
- A copy that takes over late (a deploy handover) runs the one-time startup jobs whose time has
  already passed straight away instead of skipping them.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.config.logging import get_logger

logger = get_logger(__name__)

LOCK_KEY = 740_511_13          # any fixed number - the same in every copy of the API
RETRY_SECONDS = 60


def _catch_up_one_time_jobs(scheduler) -> int:
    """One-time (DateTrigger) jobs already past their time would be skipped as misfired - run them now."""
    from apscheduler.triggers.date import DateTrigger
    soon = datetime.now(timezone.utc) + timedelta(seconds=2)
    n = 0
    for job in scheduler.get_jobs():
        if isinstance(job.trigger, DateTrigger) and job.trigger.run_date and job.trigger.run_date < soon:
            job.modify(next_run_time=soon)
            n += 1
    return n


async def run_scheduler_when_leader(scheduler, *, retry_seconds: int = RETRY_SECONDS, lock_key: int = LOCK_KEY) -> None:
    """Runs for the life of the process: become the leader when the lock is free, start the jobs,
    keep watching the lock's connection; give the lock back on shutdown (task cancelled)."""
    from app.database import engine

    started_once = False
    while True:
        conn = None
        try:
            conn = await engine.connect()
            got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key})).scalar()
            await conn.commit()
            if not got:
                await conn.close()
                conn = None
                if not started_once:
                    logger.info("Scheduled jobs run in another copy of the API - this copy serves requests only",
                                extra={"pid": os.getpid()})
                    started_once = True
                await asyncio.sleep(retry_seconds)
                continue

            # This copy is now in charge of the scheduled jobs.
            if scheduler.running:
                scheduler.resume()
            else:
                late = _catch_up_one_time_jobs(scheduler) if started_once else 0
                scheduler.start()
                if late:
                    logger.info("Took over the scheduled jobs late - ran past-due startup jobs now", extra={"count": late})
            started_once = True
            logger.info("This copy of the API runs the scheduled jobs", extra={"pid": os.getpid()})

            # Keep the lock's connection alive; if it drops, the lock is gone - stop and wait again.
            while True:
                await asyncio.sleep(retry_seconds)
                try:
                    await conn.execute(text("SELECT 1"))
                    await conn.commit()
                except Exception:  # noqa: BLE001
                    logger.warning("Lost the scheduler lock's database connection - pausing scheduled jobs", exc_info=True)
                    if scheduler.running:
                        scheduler.pause()
                    break
        except asyncio.CancelledError:
            if conn is not None:
                try:
                    await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
                    await conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            raise
        except Exception:  # noqa: BLE001 - never let this kill the app; just try again later
            logger.warning("Scheduler leader check failed - retrying", exc_info=True)
            if scheduler.running:
                scheduler.pause()
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = None
        await asyncio.sleep(retry_seconds)
