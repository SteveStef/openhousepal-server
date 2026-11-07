"""What a guest does on their Guest Pass, and what reaches the agent (owner, 11/07): plain words for every
tap, a home alert at five seconds, everything else after the first hour, never for just landing on it."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import guest_pass_service as gp

NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("kind,key,label,words", [
    ("similar", "K1", "12 Oak St, Wayne", "Looked at 12 Oak St, Wayne"),
    ("similar", "K1", None, "Looked at a similar home"),
    ("similar_list", None, None, "Looked through the Similar Homes"),
    ("cheat", None, None, "Opened the Cheat Sheet"),
    ("showcase", None, None, "Opened their Showcase"),
    ("map", None, None, "Moved around the neighborhood map"),
    ("nearby", "coffee", None, "Looked up nearby coffee shops"),
    ("nearby", "medical", None, "Looked up nearby hospitals and doctors"),
    ("spotlight", None, None, "Opened the full home tour"),
    ("directions", None, None, "Opened the home in Maps"),
    ("photos", None, None, "Looked through the home's photos"),
    ("contact", "call", None, "Tapped to call you"),
    ("contact", "text", None, "Tapped to text you"),
    ("contact", "email", None, "Tapped to email you"),
    ("contact", "hub", None, "Opened your Agent Hub"),
    ("contact", "website", None, "Opened your website"),
    ("request", "disclosure", None, "Asked for the seller disclosure"),
    ("request", "second_visit", "Sat 2 PM", "Asked for a second tour - Sat 2 PM"),
    ("question", None, "Is it on well water?", 'Asked you: "Is it on well water?"'),
])
def test_every_tap_has_plain_words(kind, key, label, words):
    assert gp.describe(kind, key, label) == words


def test_the_quiet_hour_counts_from_their_sign_in():
    assert not gp._past_quiet_hour(SimpleNamespace(created_at=NOW - timedelta(minutes=59)), NOW)
    assert gp._past_quiet_hour(SimpleNamespace(created_at=NOW - timedelta(minutes=60)), NOW)


def test_a_sign_in_time_without_a_timezone_still_works():
    naive = (NOW - timedelta(hours=2)).replace(tzinfo=None)
    assert gp._past_quiet_hour(SimpleNamespace(created_at=naive), NOW)


class Db:
    """Stands in for the session: track() below only talks to the pieces it is handed."""


def _patch(monkeypatch, row, fresh, was, label="12 Oak St, Wayne"):
    calls = []

    async def fake_home(db, oh, key):
        return label, "P1"

    async def fake_log(db, visitor, oh, kind, key=None, label=None, session=None, seconds=None):
        return row, fresh, was

    async def fake_surface(db, visitor, oh, host, kind, key, label, property_id, alert):
        calls.append((kind, key, alert))

    async def fake_note(*a, **k):
        return True

    monkeypatch.setattr(gp, "_home", fake_home)
    monkeypatch.setattr(gp, "log_event", fake_log)
    monkeypatch.setattr(gp, "_surface", fake_surface)
    monkeypatch.setattr(gp, "note_activity", fake_note)
    return calls


VISITOR_NEW = SimpleNamespace(id="v", created_at=datetime.now(timezone.utc) - timedelta(minutes=10))
VISITOR_OLD = SimpleNamespace(id="v", created_at=datetime.now(timezone.utc) - timedelta(hours=3))
OH = SimpleNamespace(id="oh", listing_key="TOURED")


@pytest.mark.asyncio
async def test_a_tap_on_a_home_and_straight_back_out_is_not_an_alert(monkeypatch):
    calls = _patch(monkeypatch, object(), True, 0)
    await gp.track(Db(), VISITOR_NEW, OH, None, "similar", "K1", "s")
    assert calls == []


@pytest.mark.asyncio
async def test_five_seconds_on_a_home_alerts_even_during_the_open_house(monkeypatch):
    calls = _patch(monkeypatch, object(), False, 0)
    await gp.track(Db(), VISITOR_NEW, OH, None, "similar", "K1", "s", 5)
    assert calls == [("similar", "K1", True)]


@pytest.mark.asyncio
async def test_more_time_on_the_same_home_does_not_alert_again(monkeypatch):
    calls = _patch(monkeypatch, object(), False, 5)
    await gp.track(Db(), VISITOR_NEW, OH, None, "similar", "K1", "s", 25)
    assert calls == []


@pytest.mark.asyncio
async def test_the_home_they_toured_is_not_an_alert(monkeypatch):
    calls = _patch(monkeypatch, object(), False, 0)
    await gp.track(Db(), VISITOR_NEW, OH, None, "similar", "TOURED", "s", 9)
    assert calls == []


@pytest.mark.asyncio
async def test_other_taps_stay_quiet_for_the_first_hour_but_go_under_the_lead(monkeypatch):
    calls = _patch(monkeypatch, object(), True, 0)
    await gp.track(Db(), VISITOR_NEW, OH, None, "cheat", None, "s")
    assert calls == [("cheat", None, False)]


@pytest.mark.asyncio
async def test_other_taps_alert_after_the_first_hour(monkeypatch):
    calls = _patch(monkeypatch, object(), True, 0)
    await gp.track(Db(), VISITOR_OLD, OH, None, "cheat", None, "s")
    assert calls == [("cheat", None, True)]


@pytest.mark.asyncio
async def test_the_same_tap_again_in_one_visit_does_nothing_new(monkeypatch):
    calls = _patch(monkeypatch, object(), False, 0)
    await gp.track(Db(), VISITOR_OLD, OH, None, "cheat", None, "s")
    assert calls == []


@pytest.mark.asyncio
async def test_the_list_of_similar_homes_is_its_own_kind(monkeypatch):
    calls = _patch(monkeypatch, object(), True, 0)
    await gp.track(Db(), VISITOR_OLD, OH, None, "similar", None, "s")
    assert calls == [("similar_list", None, True)]


@pytest.mark.asyncio
async def test_a_full_log_stops_quietly(monkeypatch):
    calls = _patch(monkeypatch, None, False, 0)
    assert await gp.track(Db(), VISITOR_OLD, OH, None, "cheat", None, "s") == {"noted": False}
    assert calls == []
