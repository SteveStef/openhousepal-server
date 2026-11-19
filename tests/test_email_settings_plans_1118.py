"""Studio -> Email shows each plan only the emails it can send (Day 3 audit 11/18).

Basic used to see Showcase, Spotlight, review and AI media switches; Premium saw Spotlight + AI media;
No-MLS agents couldn't see the Daily briefing switch (or its time) even though the briefing still went out.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.api.notifications_routes import _email_settings_payload


class _NoMailbox:
    async def execute(self, *_a, **_k):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


def _payload(plan):
    user = SimpleNamespace(id="u-test", plan_tier=plan, email_settings=None, notification_prefs=None)
    return asyncio.run(_email_settings_payload(_NoMailbox(), user))


def _client(p):
    return {c["key"] for c in p["client"]}


def _cats(p):
    return {c["id"] for t in p["receive"] for c in t["categories"]}


SHOWCASE_CLIENT = {"showcase_invite", "showcase_shared", "showcase_ready", "comment_reply", "showcase_updates"}


def test_basic_has_no_showcase_spotlight_review_or_media_switches():
    p = _payload("BASIC")
    assert not (_client(p) & (SHOWCASE_CLIENT | {"spotlight_alerts"}))
    assert "tour_confirmed" in _client(p)
    assert not (_cats(p) & {"showcase_requests", "liked_homes", "showcase_activity", "spotlight_weekly", "reviews", "ai_media"})
    assert {"leads", "host_requests", "showcase_digest", "team"} <= _cats(p)


def test_premium_keeps_showcases_but_not_spotlight_or_media():
    p = _payload("PREMIUM")
    assert SHOWCASE_CLIENT <= _client(p)
    assert "spotlight_alerts" not in _client(p)
    assert "spotlight_weekly" not in _cats(p) and "ai_media" not in _cats(p)
    assert {"showcase_requests", "reviews"} <= _cats(p)


def test_idx_sees_everything():
    p = _payload("IDX")
    assert SHOWCASE_CLIENT | {"spotlight_alerts", "tour_confirmed"} <= _client(p)
    assert {"spotlight_weekly", "ai_media", "reviews", "showcase_digest"} <= _cats(p)


@pytest.mark.parametrize("plan", ["NO_MLS", "PRO_NO_MLS"])
def test_no_mls_gets_the_daily_briefing_switch_without_showcase_wording(plan):
    p = _payload(plan)
    assert _client(p) == {"tour_confirmed"}
    digest = [c for t in p["receive"] for c in t["categories"] if c["id"] == "showcase_digest"]
    assert digest, "No-MLS agents get the briefing, so they must be able to turn it off / move it"
    assert "Showcase" not in digest[0]["help"]
    assert p["briefing_times"] and p["briefing_frequency"]
    assert not (_cats(p) & {"host_requests", "team", "listing_updates", "spotlight_weekly"})
