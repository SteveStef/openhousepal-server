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


def test_a_ring_that_already_decides_the_density_answer_stops_the_look():
    """Live 11/17, 3558 Sawmill Rd: the 10-mile ring took 34 s to find 12 homes the closer ring already had."""
    db, calls = _db([300, 900, 1000, 1000])
    rows = asyncio.run(sim.nearest_rows(db, _Q([]), 39.9, -75.3, sim.Property.latitude, sim.DENSITY_RINGS_MILES, 1000,
                                        settled=lambda r: len(r) >= 300))
    assert len(rows) == 300 and len(calls) == 1
    db, calls = _db([5, 40, 77, 90])      # never settled: on out to the last ring, as before
    rows = asyncio.run(sim.nearest_rows(db, _Q([]), 39.9, -75.3, sim.Property.latitude, sim.DENSITY_RINGS_MILES, 1000,
                                        settled=lambda r: False))
    assert len(rows) == 90 and len(calls) == 4


def test_settled_only_when_the_newest_window_has_the_12_homes_the_answer_reads():
    first = sim.DENSITY_RECENCY_WINDOWS_DAYS[0]
    assert sim.density_settled({"recency_window_days": first, "fallback_used": False, "reference_count": 12})
    assert not sim.density_settled({"recency_window_days": first, "fallback_used": False, "reference_count": 11})
    # an older window was needed: a farther home could still give the newest window enough - keep looking
    assert not sim.density_settled({"recency_window_days": sim.DENSITY_RECENCY_WINDOWS_DAYS[1], "fallback_used": False, "reference_count": 40})
    assert not sim.density_settled({"recency_window_days": None, "fallback_used": True, "reference_count": 0})
