"""Showcase sleep rule (user, 10/24): a client who ignores the emails but still uses their Showcase
keeps it awake. Pure checks on the per-Showcase rule + the favorite/wake contract."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import showcase_sleep


def _col(seen):
    return SimpleNamespace(last_visitor_activity_at=seen, favorited_at=None, last_agent_dismissed_at=None)


def test_used_recently_counts():
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    assert showcase_sleep.using_recently(_col(now - timedelta(days=3)), now)
    assert showcase_sleep.using_recently(_col(now - timedelta(days=20)), now)


def test_not_used_lately_or_never():
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    assert not showcase_sleep.using_recently(_col(now - timedelta(days=22)), now)
    assert not showcase_sleep.using_recently(_col(None), now)


def test_naive_timestamps_are_utc():
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    assert showcase_sleep.using_recently(_col(datetime(2026, 10, 20, 12)), now)


def test_window_is_three_weekly_emails():
    assert showcase_sleep.CLIENT_USING == timedelta(days=21)
