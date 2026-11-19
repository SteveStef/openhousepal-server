"""The address lookup (Google geocode) never disturbs the caller's database session.

11/14: over the daily cap the count reservation rolled the CALLER's session back and the home came back
unreadable (a 500). 11/19 (Day 7 audit): saving the map point committed the caller's session, which expired
every object the request had loaded - the agent on a listing page - and the first open of a brand-new
listing's details failed with a 500. Now the reservation and the save each use a short session of their own.
No database and no Google call here: sessions, the reservation and Google are stand-ins.
"""
import asyncio
from types import SimpleNamespace

from app.services import google_geocoding_service as geo


class _Caller:
    """The request's own session - must never be committed, rolled back or refreshed by the geocode."""
    def __init__(self):
        self.refreshed = self.commits = self.rollbacks = 0

    async def refresh(self, obj):
        self.refreshed += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _Own:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        self.log.append(stmt)

    async def commit(self):
        self.log.append("commit")


def _home():
    return SimpleNamespace(id="p1", google_rooftop_latitude=None, google_rooftop_longitude=None, google_geocoded_at=None,
                           google_geocode_location_type=None,
                           listing_key="805383867140", street_address="10918 VALLEY FORGE CIR #918",
                           city="KING OF PRUSSIA", state="PA", zipcode="19406",
                           full_address="10918 Valley Forge Cir #918, King of Prussia, PA 19406")


def _setup(monkeypatch, reserve, log):
    monkeypatch.setenv("GOOGLE_GEOCODING_API_KEY", "test-key")

    async def get_config(_db):
        return {"platform_emergency_daily_cost_ceiling_usd": 100.0, "platform_emergency_monthly_cost_ceiling_usd": 200.0}

    from app.services import usage_budget_service
    from app.services.ai.ai_config_service import ai_config_service
    monkeypatch.setattr(ai_config_service, "get_config", get_config)
    monkeypatch.setattr(usage_budget_service, "reserve", reserve)
    monkeypatch.setattr(geo, "_own_session", lambda: _Own(log))
    import sqlalchemy
    monkeypatch.setattr(sqlalchemy, "inspect", lambda obj: SimpleNamespace(expired_attributes=set()))


def test_over_the_cap_the_callers_session_is_untouched(monkeypatch):
    home, db, log = _home(), _Caller(), []

    async def over_cap(own, buckets, cost, count_delta=0):
        assert isinstance(own, _Own)           # the reservation runs on its own session
        raise RuntimeError("Budget exceeded for bucket='google_geocode' period='day'")

    _setup(monkeypatch, over_cap, log)
    out = asyncio.run(geo.ensure_google_rooftop_geocode(db, home))
    assert out is home and (db.commits, db.rollbacks, db.refreshed) == (0, 0, 0)
    assert home.google_rooftop_latitude is None


def test_a_rooftop_point_is_saved_on_its_own_session(monkeypatch):
    home, db, log = _home(), _Caller(), []

    async def ok(own, buckets, cost, count_delta=0):
        assert isinstance(own, _Own)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"geometry": {"location_type": "ROOFTOP", "location": {"lat": 40.1, "lng": -75.4}}}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp()

    async def no_charge(*a, **k):
        return None

    _setup(monkeypatch, ok, log)
    monkeypatch.setattr(geo.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(geo, "charge_google", no_charge)
    out = asyncio.run(geo.ensure_google_rooftop_geocode(db, home))
    assert out is home and (db.commits, db.rollbacks, db.refreshed) == (0, 0, 0)
    assert (home.google_rooftop_latitude, home.google_rooftop_longitude, home.google_geocode_location_type) == (40.1, -75.4, "ROOFTOP")
    assert "commit" in log and any("UPDATE properties" in str(x) for x in log)


def test_a_home_that_was_not_touched_is_not_re_read(monkeypatch):
    home, db = _home(), _Caller()
    import sqlalchemy
    monkeypatch.setattr(sqlalchemy, "inspect", lambda obj: SimpleNamespace(expired_attributes=set()))
    assert asyncio.run(geo._usable(db, home)) is home and db.refreshed == 0
