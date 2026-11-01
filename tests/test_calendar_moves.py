"""A follow-up moved on the agent's own calendar follows it in the app (owner, 11/01) - and the app
never drags it back. calendar_sync_service._adopt_calendar_move / _start_key."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import calendar_sync_service as cs


class _DB:
    def add(self, _x):
        pass

    async def commit(self):
        pass


def _task(due, all_day=False, done=None, remind=None):
    return SimpleNamespace(id="t1", due_at=due, all_day=all_day, completed_at=done, remind_at=remind, reminder_sent_at=None)


@pytest.fixture(autouse=True)
def _body(monkeypatch):
    # the app's event for the task as it is now: its start follows due_at / all_day
    async def body_for(_db, task):
        if task.all_day:
            return {"summary": "x", "start": {"date": task.due_at.astimezone(cs.EASTERN).date().isoformat()}}
        return {"summary": "x", "start": cs._local(task.due_at)}
    monkeypatch.setattr(cs, "task_body_for", body_for)


def test_start_key_reads_google_and_outlook_the_same():
    assert cs._start_key({"dateTime": "2026-11-03T10:00:00-05:00"}) == cs._start_key({"dateTime": "2026-11-03T15:00:00.0000000", "timeZone": "UTC"}) == "2026-11-03T15:00"
    assert cs._start_key({"date": "2026-11-03"}) == "date:2026-11-03"
    assert cs._start_key(None) is None


@pytest.mark.asyncio
async def test_moved_on_the_calendar_moves_the_followup():
    due = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    task = _task(due, remind=due - timedelta(minutes=15))
    link = SimpleNamespace(pushed_start="2026-11-03T15:00", body_hash="h", updated_at=None)
    moved = await cs._adopt_calendar_move(_DB(), SimpleNamespace(provider="google"), task, link, {"dateTime": "2026-11-05T19:30:00Z"})
    assert moved and task.due_at == datetime(2026, 11, 5, 19, 30, tzinfo=timezone.utc)
    assert task.remind_at == task.due_at - timedelta(minutes=15)
    assert link.pushed_start == "2026-11-05T19:30"   # the app now agrees - nothing gets pushed back


@pytest.mark.asyncio
async def test_same_place_or_done_does_nothing():
    due = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    link = SimpleNamespace(pushed_start="2026-11-03T15:00", body_hash="h", updated_at=None)
    assert not await cs._adopt_calendar_move(_DB(), SimpleNamespace(provider="google"), _task(due), link, {"dateTime": "2026-11-03T10:00:00-05:00"})
    done = _task(due, done=datetime.now(timezone.utc))
    assert not await cs._adopt_calendar_move(_DB(), SimpleNamespace(provider="google"), done, link, {"dateTime": "2026-11-09T15:00:00Z"})


@pytest.mark.asyncio
async def test_all_day_move_keeps_it_all_day_on_the_new_day():
    due = datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc)
    task = _task(due, all_day=True)
    link = SimpleNamespace(pushed_start="date:2026-11-03", body_hash="h", updated_at=None)
    assert await cs._adopt_calendar_move(_DB(), SimpleNamespace(provider="microsoft"), task, link, {"date": "2026-11-06"})
    assert task.all_day and task.due_at.astimezone(cs.EASTERN).date().isoformat() == "2026-11-06"


@pytest.mark.asyncio
async def test_old_link_without_a_saved_start_only_adopts_when_the_app_had_no_change():
    due = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    task = _task(due)
    body = await cs.task_body_for(None, task)
    unchanged = SimpleNamespace(pushed_start=None, body_hash=cs._body_hash(body), updated_at=None)
    assert await cs._adopt_calendar_move(_DB(), SimpleNamespace(provider="google"), task, unchanged, {"dateTime": "2026-11-04T15:00:00Z"})
    task2 = _task(due)
    app_changed = SimpleNamespace(pushed_start=None, body_hash="old-body", updated_at=None)
    assert not await cs._adopt_calendar_move(_DB(), SimpleNamespace(provider="google"), task2, app_changed, {"dateTime": "2026-11-04T15:00:00Z"})
    assert task2.due_at == due
