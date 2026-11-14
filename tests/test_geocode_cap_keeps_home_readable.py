"""Over the daily address-lookup cap, a home must still load (11/14: it returned a 500).

The count reservation rolls the session back when the cap is reached; a rollback expires every loaded field of
the home, and the caller then read one outside an await. No database and no Google call here: the session and
the reservation are stand-ins.
"""
import asyncio
from types import SimpleNamespace

from app.services import google_geocoding_service as geo


class _Session:
    def __init__(self):
        self.refreshed = 0

    async def refresh(self, obj):
        self.refreshed += 1
        obj.expired = False


def _home():
    return SimpleNamespace(google_rooftop_latitude=None, google_rooftop_longitude=None, google_geocoded_at=None,
                           listing_key="805383867140", street_address="10918 VALLEY FORGE CIR #918",
                           city="KING OF PRUSSIA", state="PA", zipcode="19406", expired=False,
                           full_address="10918 Valley Forge Cir #918, King of Prussia, PA 19406")


def test_over_the_cap_the_home_is_re_read_not_returned_expired(monkeypatch):
    home, db = _home(), _Session()
    monkeypatch.setenv("GOOGLE_GEOCODING_API_KEY", "test-key")

    async def get_config(_db):
        return {"platform_emergency_daily_cost_ceiling_usd": 100.0, "platform_emergency_monthly_cost_ceiling_usd": 200.0}

    async def over_cap(_db, buckets, cost, count_delta=0):
        home.expired = True            # what the rollback inside the real reservation does
        raise RuntimeError("Budget exceeded for bucket='google_geocode' period='day'")

    from app.services import usage_budget_service
    from app.services.ai.ai_config_service import ai_config_service
    monkeypatch.setattr(ai_config_service, "get_config", get_config)
    monkeypatch.setattr(usage_budget_service, "reserve", over_cap)
    # stand-in for SQLAlchemy's "which fields are waiting to be re-read"
    import sqlalchemy
    monkeypatch.setattr(sqlalchemy, "inspect", lambda obj: SimpleNamespace(expired_attributes={"x"} if obj.expired else set()))

    out = asyncio.run(geo.ensure_google_rooftop_geocode(db, home))
    assert out is home and db.refreshed == 1 and home.expired is False


def test_a_home_that_was_not_touched_is_not_re_read(monkeypatch):
    home, db = _home(), _Session()
    import sqlalchemy
    monkeypatch.setattr(sqlalchemy, "inspect", lambda obj: SimpleNamespace(expired_attributes=set()))
    assert asyncio.run(geo._usable(db, home)) is home and db.refreshed == 0
