"""Only one copy of the API runs the scheduled jobs (app/services/scheduler_leader.py) - against the real
local database, with a test-only lock number so it never touches the real one."""
import asyncio
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from app.services.scheduler_leader import _catch_up_one_time_jobs, run_scheduler_when_leader

TEST_KEY = 990_011_14


def _sched():
    s = AsyncIOScheduler()
    s.add_job(lambda: None, "interval", minutes=60, id="tick")
    return s


def _running(s):
    return s.running and s.state == 1   # STATE_RUNNING (not paused)


def test_exactly_one_copy_runs_jobs_and_another_takes_over():
    async def go():
        a, b, c = _sched(), _sched(), _sched()
        tasks = [asyncio.create_task(run_scheduler_when_leader(s, retry_seconds=0.3, lock_key=TEST_KEY)) for s in (a, b, c)]
        await asyncio.sleep(1.2)
        leaders = [s for s in (a, b, c) if _running(s)]
        assert len(leaders) == 1, "exactly one copy must run the jobs"

        # The leader's process goes away (task cancelled -> lock given back): another takes over.
        i = (a, b, c).index(leaders[0])
        tasks[i].cancel()
        try:
            await tasks[i]
        except asyncio.CancelledError:
            pass
        leaders[0].shutdown(wait=False)
        await asyncio.sleep(1.5)
        rest = [s for j, s in enumerate((a, b, c)) if j != i]
        assert sum(1 for s in rest if _running(s)) == 1, "one of the others must take over"

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for s in (a, b, c):
            if s.running:
                s.shutdown(wait=False)
    asyncio.run(go())


def test_late_takeover_runs_past_due_one_time_jobs():
    async def go():
        s = AsyncIOScheduler()
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        s.add_job(lambda: None, DateTrigger(run_date=past), id="once")
        s.start(paused=True)
        assert _catch_up_one_time_jobs(s) == 1
        job = s.get_job("once")
        assert job is not None and job.next_run_time > datetime.now(timezone.utc) - timedelta(seconds=1)
        s.shutdown(wait=False)
    asyncio.run(go())
