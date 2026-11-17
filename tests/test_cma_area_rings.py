"""The comp search's area look asks close first and widens only when needed (live 11/17, 119 East Ln: one 20-mile look
read hundreds of thousands of homes off the disk, 100+ seconds, and the agent's first search came back empty).
Checked on 120 real homes on the local database: identical results to the one 20-mile look."""
import asyncio
from types import SimpleNamespace as NS

from app.services import cma_similarity_service as sim


class _Q:
    def __init__(self, log): self.log = log
    def where(self, *a): return self
    def limit(self, n): self.log.append(n); return self


def _db(counts):
    calls = []

    class DB:
        async def execute(self, q):
            calls.append(1)
            n = counts[len(calls) - 1]
            return NS(mappings=lambda: NS(all=lambda: [{"i": i} for i in range(n)]))
    return DB(), calls


def test_a_full_close_ring_stops_the_look():
    db, calls = _db([1000])
    rows = asyncio.run(sim.nearest_rows(db, _Q([]), 39.9, -75.3, sim.Property.latitude, sim.DENSITY_RINGS_MILES, 1000))
    assert len(rows) == 1000 and len(calls) == 1


def test_a_thin_area_widens_ring_by_ring_to_the_old_20_miles():
    db, calls = _db([29, 248, 900, 1000])
    rows = asyncio.run(sim.nearest_rows(db, _Q([]), 39.9, -75.3, sim.Property.latitude, sim.DENSITY_RINGS_MILES, 1000))
    assert len(rows) == 1000 and len(calls) == 4 and sim.DENSITY_RINGS_MILES[-1] == sim.MAX_DENSITY_SEARCH_MILES == 20.0
    db, calls = _db([3, 10, 40, 77])     # a rural home: whatever 20 miles holds, as before
    assert len(asyncio.run(sim.nearest_rows(db, _Q([]), 39.9, -75.3, sim.Property.latitude, sim.DENSITY_RINGS_MILES, 1000))) == 77


def test_the_recent_sales_look_is_still_one_6_mile_look():
    assert sim.PRICE_SURFACE_RINGS_MILES == (sim.PRICE_SURFACE_MILES,) == (6.0,)
