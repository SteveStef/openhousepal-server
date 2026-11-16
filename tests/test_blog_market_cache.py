"""11/15 night, from the live five-article test: "Our servers are very busy right now" with nobody else on the site.
The cause was one whole-market count (45 seconds on live) kept only in each API copy's memory for an hour. It is now
kept in the database and a slightly older copy is used rather than counting again. No network and no database."""
import inspect
from datetime import datetime, timedelta, timezone

from app.api import blog_routes
from app.services.areas import market_stats_service as ms

NOW = datetime(2026, 11, 15, 23, 0, tzinfo=timezone.utc)
ago = lambda h: (NOW - timedelta(hours=h)).isoformat()                       # noqa: E731


def test_a_recent_count_for_nearly_the_same_window_is_used_instead_of_counting_again():
    row = {"end": "2026-11-14", "slice_days": 30, "n": 12, "value": list(range(12, 0, -1)), "at": ago(5)}
    assert ms.usable_slices(row, "2026-11-14", 30, 12, NOW) == list(range(12, 0, -1))
    assert ms.usable_slices(row, "2026-11-15", 30, 12, NOW) is not None           # the newest close date moved by a day
    assert ms.usable_slices(row, "2026-11-17", 30, 12, NOW) is not None           # ... or three
    assert ms.usable_slices(row, "2026-11-18", 30, 12, NOW) is None               # four days: count again
    assert ms.usable_slices({**row, "at": ago(37)}, "2026-11-14", 30, 12, NOW) is None       # too old
    assert ms.usable_slices({**row, "slice_days": 7}, "2026-11-14", 30, 12, NOW) is None
    assert ms.usable_slices({**row, "value": [1, 2, 3]}, "2026-11-14", 30, 12, NOW) is None  # fewer slices than asked for
    assert ms.usable_slices(None, "2026-11-14", 30, 12, NOW) is None and ms.usable_slices({"at": "not a time"}, "2026-11-14", 30, 12, NOW) is None


def test_last_years_window_is_matched_by_length_and_nearness():
    row = {"entries": {"2025-08-16|2025-11-14": {"value": 61000, "at": ago(4)},        # 90 days
                       "2025-10-15|2025-11-14": {"value": 21000, "at": ago(4)},        # 30 days
                       "2025-08-10|2025-11-08": {"value": 59000, "at": ago(40)}}}      # 90 days, but too old
    assert ms.usable_count(row, "2025-08-16", "2025-11-14", NOW) == 61000
    assert ms.usable_count(row, "2025-08-17", "2025-11-15", NOW) == 61000              # the same 90 days, one day on
    assert ms.usable_count(row, "2025-10-16", "2025-11-15", NOW) == 21000              # never a window of another length
    assert ms.usable_count(row, "2025-08-24", "2025-11-22", NOW) is None               # a week on: count again
    assert ms.usable_count(None, "2025-08-16", "2025-11-14", NOW) is None and ms.usable_count({}, "2025-08-16", "2025-11-14", NOW) is None


def test_the_counts_are_kept_in_the_database_and_refreshed_in_the_background():
    slices = inspect.getsource(ms._closed_counts_by_slice)
    assert slices.index("_remembered(key)") < slices.index('_stored("closed_slices")') < slices.index("_count_slices(db, end, slice_days, n)")   # memory, the kept copy, then count
    assert '_store("closed_slices"' in slices
    one = inspect.getsource(ms._closed_count_all)
    assert one.index("_remembered(key)") < one.index('_stored("closed_all")') < one.index("_count_closed(db, start, stop)")
    assert "SystemSettings" in inspect.getsource(ms._store) and "AsyncSessionLocal" in inspect.getsource(ms._store)       # its own connection, no migration
    assert ms._persist() is False                                                   # a test never reads or writes the real table
    import app.main as main
    assert "refresh_whole_market_counts" in inspect.getsource(main) and 'id="whole_market_counts"' in inspect.getsource(main)


def test_ready_to_write_is_not_ready_when_its_own_look_up_timed_out():
    probe = inspect.getsource(blog_routes._writer_probe)
    assert 'not (looked or {}).get("sales_snapshot") and time.monotonic() - t_dossier > 15' in probe and 'out["busy"] = True' in probe
