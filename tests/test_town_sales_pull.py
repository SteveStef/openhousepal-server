"""One-time past-year sales pull for the towns around the CMA test home (10/22) - offline."""
import asyncio
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import bright_data_pulls_service as pulls  # noqa: E402


def test_rides_the_sold_switch_and_runs_first(monkeypatch):
    monkeypatch.setenv("BRIGHT_DATA_PULLS", "sold_3y,price_history")
    assert pulls.enabled("town_sales_1y")
    assert pulls.SALES_PULLS[0] == "town_sales_1y"
    monkeypatch.setenv("BRIGHT_DATA_PULLS", "")
    assert not pulls.enabled("town_sales_1y")


def test_walks_every_town_then_marks_done(monkeypatch):
    state = {}

    async def fake_get(db, key):
        return dict(state.get(key, {}))

    async def fake_set(db, key, value):
        state[key] = dict(value)

    calls = []

    class FakeBright:
        async def get_sold_properties(self, city, st, date_from, top=200, skip=0):
            calls.append((city, skip))
            return [{"ListingKey": 1, "PropertyType": "Residential Lease"}] if city == "Downingtown" else []

        async def get_media_for_properties(self, keys):
            return {}

    import app.services.bright_mls_service as bms
    monkeypatch.setattr(pulls, "_get", fake_get)
    monkeypatch.setattr(pulls, "_set", fake_set)
    monkeypatch.setattr(bms, "bright_mls_service", FakeBright())
    out = asyncio.run(pulls.pull_town_sales_1y(None, time_budget=30, pause=0))
    assert out["done"] and [c for c, _ in calls] == [t for t, _ in pulls.TOWN_SALES_1Y]
    assert state["data_pull_town_sales_1y"]["read_total"] == 0   # the lease was skipped
