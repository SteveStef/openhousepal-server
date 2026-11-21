"""Admin "Reset AI usage" (11/20): spend before the reset stops counting for the rest of the period; the period's
end day is unchanged; a reset from an earlier period (or the future) changes nothing."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.ai import ai_rate_limit_service as rl


class _Db:
    def __init__(self, user):
        self.user = user

    async def get(self, _model, _id):
        return self.user


def _user(**kw):
    now = datetime.now(timezone.utc)
    base = dict(next_billing_date=now + timedelta(days=7), trial_ends_at=None, subscription_started_at=None,
                created_at=now - timedelta(days=200), ai_usage_reset_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _period(user):
    return asyncio.run(rl.usage_period(_Db(user), "u1"))


def test_no_reset_is_the_billing_period():
    u = _user()
    assert _period(u) == rl.billing_period(u)


def test_reset_inside_period_starts_count_there_and_keeps_end():
    reset = datetime.now(timezone.utc) - timedelta(minutes=5)
    u = _user(ai_usage_reset_at=reset)
    start, end = _period(u)
    assert start == reset
    assert end == rl.billing_period(u)[1]


def test_reset_from_an_earlier_period_is_ignored():
    u = _user(ai_usage_reset_at=datetime.now(timezone.utc) - timedelta(days=60))
    assert _period(u) == rl.billing_period(u)


def test_naive_reset_time_is_read_as_utc():
    reset = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
    start, _ = _period(_user(ai_usage_reset_at=reset))
    assert start == reset.replace(tzinfo=timezone.utc)
