"""Background Bright jobs made cheap (owner, 11/13) - offline, no network, no database.

- A re-read that changes nothing writes nothing; any real difference (or a value we can't compare) is written.
- The price-history tick looks only at listings changed since its bookmark; once a night it looks at
  every on-market parcel. Parcels a time limit stopped are carried to the next tick; failed ones are not
  retried in a loop.
- The weekly off-market recheck reads its key list once per pass and pages through it in memory.
"""
import asyncio
import enum
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import bright_data_pulls_service as pulls  # noqa: E402
from app.services import price_history_service as phs  # noqa: E402
from app.services import property_sync_service as pss  # noqa: E402
from app.services.bright_mls_service import bright_mls_service  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ unchanged values

class _Kind(str, enum.Enum):
    CONDO = "CONDO"


def test_same_value_is_strict():
    same = pss._same_value
    assert same(5, 5) and same("a", "a") and same(None, None)
    assert same({"a": [1, 2]}, {"a": [1, 2]}) and same(["x", "y"], ["x", "y"])
    assert same(_Kind.CONDO, "CONDO")
    t = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    assert same(t, t)
    # any real difference - or doubt - counts as changed
    assert not same(5, 6) and not same("a", "b") and not same(None, "") and not same("", None)
    assert not same({"a": 1}, {"a": 2}) and not same(["x"], ["x", "y"])
    assert not same(True, 1) and not same(0, False)
    # a date-only Bright value parses without a timezone; the UTC database stores it as that UTC moment
    assert same(t, t.replace(tzinfo=None))
    assert not same(t, t.replace(tzinfo=None) + timedelta(hours=5))
    assert not same(Decimal("100.10"), 100.1)  # inexact -> written, as before
    assert not same(pss._MISSING, 5)


class _Stored:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_changed_columns_skips_the_read_stamp_unless_it_matters():
    old_read = datetime(2026, 11, 1, tzinfo=timezone.utc)
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    stored = dict(price=500000, days_on_market=30, home_status="PENDING-BRIGHT", dom_read_at=old_read)
    # nothing changed on a pending home -> nothing written (not even the read stamp)
    assert pss._changed_columns(_Stored(**stored), dict(price=500000, days_on_market=30, home_status="PENDING-BRIGHT", dom_read_at=now)) == []
    # a real change is written; the stamp moves with the day count
    assert pss._changed_columns(_Stored(**stored), dict(price=490000, days_on_market=30, home_status="PENDING-BRIGHT", dom_read_at=now)) == ["price"]
    assert sorted(pss._changed_columns(_Stored(**stored), dict(price=500000, days_on_market=31, home_status="PENDING-BRIGHT", dom_read_at=now))) == ["days_on_market", "dom_read_at"]
    # an Active home's shown count = stored count + days since read, so every read keeps the stamp current
    active = dict(stored, home_status="ACTIVE-BRIGHT")
    assert pss._changed_columns(_Stored(**active), dict(price=500000, days_on_market=30, home_status="ACTIVE-BRIGHT", dom_read_at=now)) == ["dom_read_at"]


# ------------------------------------------------------------------ price history tick

class _Rows:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def scalar(self):
        return self._items

    def all(self):
        return self._items

    def __iter__(self):
        return iter(self._items)


class _DB:
    """Answers each execute() with the next canned result, and keeps the SQL it was asked."""
    def __init__(self, answers):
        self.answers = list(answers)
        self.sql = []

    async def execute(self, stmt, params=None):
        self.sql.append((str(stmt), params or {}))
        return _Rows(self.answers.pop(0) if self.answers else [])

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _state(monkeypatch, initial=None):
    store = dict(initial or {})

    async def fake_get(db, key):
        return dict(store.get(key) or {})

    async def fake_set(db, key, value):
        store[key] = dict(value)

    monkeypatch.setattr(pulls, "_get", fake_get)
    monkeypatch.setattr(pulls, "_set", fake_set)
    return store


HI = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)


def test_price_history_tick_looks_only_at_recent_changes(monkeypatch):
    store = _state(monkeypatch, {"data_pull_price_history": {"since": "2026-11-14T01:50:00+00:00", "night_day": date.today().isoformat()}})
    monkeypatch.setattr(pulls, "_night_now", lambda: True)  # already done tonight -> still the recent look
    got = {}

    async def fake_batch(db, tax_ids=None, time_budget=None, **kw):
        got["ids"] = list(tax_ids)
        return {"parcels": 2, "listings_added": 3, "failed": 0, "not_reached": []}

    monkeypatch.setattr(phs, "backfill_batch", fake_batch)
    db = _DB([HI, ["T1", "T2"]])
    out = _run(pulls.price_history_tick(db))
    assert out["mode"] == "recent" and got["ids"] == ["T1", "T2"]
    sql, params = db.sql[1]
    assert "modification_timestamp > :since" in sql and "WITH cur" not in sql
    assert params["since"] == datetime(2026, 11, 14, 1, 20, tzinfo=timezone.utc)  # bookmark - 30 min overlap
    assert store["data_pull_price_history"]["since"] == HI.isoformat()  # bookmark moves to the newest change


def test_price_history_tick_carries_unreached_but_not_failed(monkeypatch):
    store = _state(monkeypatch, {"data_pull_price_history": {"since": HI.isoformat(), "night_day": date.today().isoformat(),
                                                             "queue": ["Q1", "Q2", "Q3"]}})
    got = {}

    async def fake_batch(db, tax_ids=None, time_budget=None, **kw):
        got["ids"] = list(tax_ids)
        return {"parcels": 1, "listings_added": 1, "failed": 1, "not_reached": ["N1"]}

    monkeypatch.setattr(phs, "backfill_batch", fake_batch)
    # bookmark max, recent rows, then which queued parcels were pulled meanwhile (Q2)
    db = _DB([HI, ["N1"], ["Q2"]])
    out = _run(pulls.price_history_tick(db))
    assert got["ids"] == ["Q1", "Q3", "N1"]
    # only the parcel the time limit never reached is carried; the failed one waits for the night pass
    assert store["data_pull_price_history"]["queue"] == ["N1"] and out["left_for_next_tick"] == 1


def test_price_history_tick_nightly_pass_once_a_night(monkeypatch):
    store = _state(monkeypatch, {"data_pull_price_history": {"since": HI.isoformat(), "night_day": "2000-01-01"}})
    monkeypatch.setattr(pulls, "_night_now", lambda: True)

    async def fake_batch(db, tax_ids=None, time_budget=None, **kw):
        return {"parcels": len(tax_ids), "listings_added": 0, "failed": 0, "not_reached": []}

    monkeypatch.setattr(phs, "backfill_batch", fake_batch)
    db = _DB([HI, ["A", "B", "C"]])
    out = _run(pulls.price_history_tick(db))
    assert out["mode"] == "nightly" and "WITH cur AS MATERIALIZED" in db.sql[1][0]
    assert store["data_pull_price_history"]["night_day"] == date.today().isoformat()
    # the next tick the same night is the cheap look again
    db2 = _DB([HI, []])
    assert _run(pulls.price_history_tick(db2))["mode"] == "recent"


def test_price_history_tick_daytime_never_runs_the_night_pass(monkeypatch):
    _state(monkeypatch, {"data_pull_price_history": {"since": HI.isoformat(), "night_day": "2000-01-01"}})
    monkeypatch.setattr(pulls, "_night_now", lambda: False)
    db = _DB([HI, []])
    assert _run(pulls.price_history_tick(db))["mode"] == "recent"


def test_price_history_first_run_starts_a_day_back(monkeypatch):
    store = _state(monkeypatch)
    monkeypatch.setattr(pulls, "_night_now", lambda: False)
    db = _DB([HI, []])
    out = _run(pulls.price_history_tick(db))
    assert out["mode"] == "recent"
    assert db.sql[1][1]["since"] == HI - timedelta(days=1, minutes=30)
    assert store["data_pull_price_history"]["since"] == HI.isoformat()


def test_backfill_batch_reports_parcels_not_reached(monkeypatch):
    async def fake_lookup(group, per_request=100):
        return {t: [] for t in group}

    async def fake_insert(db, syncer, found):
        return 0

    monkeypatch.setattr(bright_mls_service, "get_listings_for_tax_ids", fake_lookup)
    monkeypatch.setattr(phs, "_insert_parcel_listings", fake_insert)

    class _AddDB(_DB):
        def add(self, obj):
            pass

    ids = [f"T{i}" for i in range(250)]
    out = _run(phs.backfill_batch(_AddDB([]), tax_ids=ids, per_request=100, pause_seconds=0, time_budget=1e-9))
    assert out["parcels"] == 0 and out["not_reached"] == ids  # no time at all -> nothing asked, all carried
    out = _run(phs.backfill_batch(_AddDB([]), tax_ids=ids, per_request=100, pause_seconds=0))
    assert out["parcels"] == 250 and out["not_reached"] == []


# ------------------------------------------------------------------ weekly off-market recheck

def test_off_market_recheck_reads_its_list_once_per_pass(monkeypatch):
    store = _state(monkeypatch, {"data_pull_recheck_off_market": {"pass_started": "2026-11-17"}})
    pulls._OFF_MARKET_KEYS.clear()
    asked = []

    async def fake_lookup(keys):
        asked.append(list(keys))
        return []

    monkeypatch.setattr(bright_mls_service, "get_listings_by_keys", fake_lookup)
    db = _DB([["3", "1", "2", "5", "4"]])
    _run(pulls.recheck_off_market(db, max_keys=2, pause=0))
    _run(pulls.recheck_off_market(db, max_keys=2, pause=0))
    _run(pulls.recheck_off_market(db, max_keys=2, pause=0))
    out = _run(pulls.recheck_off_market(db, max_keys=2, pause=0))
    assert len(db.sql) == 1 and "OFF_MARKET-BRIGHT" in db.sql[0][0] and "listing_key >" not in db.sql[0][0]
    assert asked == [["1", "2"], ["3", "4"], ["5"]]
    assert out["done"] and store["data_pull_recheck_off_market"]["after_key"] == "5"
