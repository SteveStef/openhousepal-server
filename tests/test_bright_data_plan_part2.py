"""Offline checks for the Bright data plan, Part 2 (10/21) - no network, no database.

- A price and a status change seen together log two events; a home first seen under contract logs one.
- Every changed field is logged old -> new (only real changes; the gallery as a count).
- A partial gallery never replaces a fuller stored one; a complete shorter one does.
- A failed media page drops that chunk from the map and queues it for a retry.
- The daily stale recheck re-reads homes Bright still has and takes the rest off the market.
- The off-market recheck starts a new pass a week after the last one.
- The photo refill waits for sold_3y, then refills only galleries shorter than Bright's TotalPhotos.
- The price history shows pending / back on market, and hides the Oct 15-20 false revivals.
"""
import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402 - loads the app once so service imports resolve in order
from app.models.database import Property, PropertyFieldChange, PropertyPriceEvent  # noqa: E402
from app.services import bright_data_pulls_service as pulls  # noqa: E402
from app.services import price_history_service as phs  # noqa: E402
from app.services import property_sync_service as pss  # noqa: E402
from app.services.bright_mls_service import bright_mls_service  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ one listing through the sync

class _Res:
    def __init__(self, value=None):
        self.value = value
        self.rowcount = 1

    def scalar_one_or_none(self):
        return self.value


class _SyncDB:
    def __init__(self, existing):
        self.existing = existing
        self.added = []

    async def execute(self, stmt, params=None):
        return _Res(self.existing if str(stmt).lstrip().upper().startswith("SELECT") else None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = "new-id"

    async def rollback(self):
        pass


def _raw(**over):
    raw = {
        "ListingKey": 805000000001, "MlsStatus": "ACTIVE-BRIGHT", "ListPrice": 500000, "FullStreetAddress": "1 Oak St",
        "City": "Malvern", "StateOrProvince": "PA", "PostalCode": "19355", "PropertyType": "Residential",
        "BedroomsTotal": 4, "LivingArea": 2400, "ModificationTimestamp": "2026-10-21T12:00:00Z", "TotalPhotos": 30,
    }
    raw.update(over)
    return raw


def _stored(**over):
    mapped = pss.map_reso_to_internal(_raw(), {"805000000001": [f"p{i}.jpg" for i in range(30)]})
    mapped.update(over)
    return Property(id="p1", **mapped)


def _sync(existing, raw, photos=None):
    db = _SyncDB(existing)
    _run(pss.PropertySyncService()._sync_single_property(db, raw, photos or {}, allow_inactive_insert=False))
    return db


def test_price_and_status_change_together_log_two_events():
    db = _sync(_stored(), _raw(ListPrice=480000, MlsStatus="PENDING-BRIGHT"))
    events = [a for a in db.added if isinstance(a, PropertyPriceEvent)]
    assert sorted(e.kind for e in events) == ["price", "status"]
    changes = {c.field: (c.old_value, c.new_value) for c in db.added if isinstance(c, PropertyFieldChange)}
    assert changes["price"] == ("500000", "480000")
    assert changes["home_status"] == ("ACTIVE-BRIGHT", "PENDING-BRIGHT")


def test_unchanged_read_logs_nothing():
    db = _sync(_stored(), _raw())
    assert not [a for a in db.added if isinstance(a, (PropertyFieldChange, PropertyPriceEvent))]


def test_first_seen_under_contract_logs_an_event():
    db = _sync(None, _raw(MlsStatus="ACTIVE UNDER CONTRACT-BRIGHT", StatusChangeTimestamp="2026-10-18T09:00:00Z"))
    (ev,) = [a for a in db.added if isinstance(a, PropertyPriceEvent)]
    assert (ev.kind, ev.status, ev.previous_status) == ("status", "ACTIVE UNDER CONTRACT-BRIGHT", None)
    assert ev.observed_at == datetime(2026, 10, 18, 9, tzinfo=timezone.utc)


def test_partial_gallery_never_replaces_a_fuller_one():
    existing = _stored()
    _sync(existing, _raw(), {"805000000001": ["a.jpg", "b.jpg"]})  # 2 of 30 came back
    assert len(existing.photos) == 30


def test_complete_shorter_gallery_is_taken():
    existing = _stored()
    db = _sync(existing, _raw(TotalPhotos=12), {"805000000001": [f"n{i}.jpg" for i in range(12)]})
    assert len(existing.photos) == 12  # the agent really removed photos
    changes = {c.field: (c.old_value, c.new_value) for c in db.added if isinstance(c, PropertyFieldChange)}
    assert changes["photos_count"] == ("30", "12")


def test_field_text_compares_like_with_like():
    assert pss._field_text(500000.0) == pss._field_text(500000)
    assert pss._field_text(datetime(2026, 10, 21, 12, tzinfo=timezone.utc)) == pss._field_text("2026-10-21T12:00:00Z")
    assert pss._field_text("") is None and pss._field_text([]) is None
    assert pss._field_text(True) == "true"


# ------------------------------------------------------------------ media fetch

def test_failed_media_page_drops_and_queues_the_chunk(monkeypatch):
    calls = []

    async def fake_request(endpoint, params=None):
        keys = params["$filter"].split("(")[1].split(")")[0].split(",")
        calls.append(keys[0])
        if "26" in keys:
            raise RuntimeError("Bright blip")
        return {"value": [{"ResourceRecordKey": k, "MediaURL": f"{k}.jpg"} for k in keys]}

    async def no_sleep(_s):
        pass

    monkeypatch.setattr(bright_mls_service, "_make_request", fake_request)
    monkeypatch.setattr(pss.bright_mls_service, "media_retry_keys", set())
    import app.services.bright_mls_service as bms
    monkeypatch.setattr(bms.asyncio, "sleep", no_sleep)
    keys = [str(i) for i in range(1, 51)]  # chunks 1-25 and 26-50; the second always fails
    out = _run(bright_mls_service.get_media_for_properties(keys))
    assert sorted(out, key=int) == [str(i) for i in range(1, 26)]
    assert bright_mls_service.media_retry_keys == {str(i) for i in range(26, 51)}
    assert calls == ["1", "26", "26"]  # the failed page was tried twice


# ------------------------------------------------------------------ daily stale recheck

class _Rows:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items

    def __iter__(self):
        return iter(self._items)


class _StaleDB:
    def __init__(self, pages):
        self.pages = list(pages)

    async def execute(self, stmt, params=None):
        return _Rows(self.pages.pop(0) if self.pages else [])

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _fake_state(monkeypatch, initial=None):
    store = dict(initial or {})

    async def fake_get(db, key):
        return dict(store.get(key) or {})

    async def fake_set(db, key, value):
        store[key] = dict(value)

    monkeypatch.setattr(pulls, "_get", fake_get)
    monkeypatch.setattr(pulls, "_set", fake_set)
    return store


def test_stale_recheck_rereads_and_takes_missing_off_market(monkeypatch):
    store = _fake_state(monkeypatch)
    reread, marked = [], []

    async def fake_lookup(keys):
        return [{"ListingKey": 11, "MlsStatus": "CLOSED-BRIGHT"}]  # 12 is gone from Bright

    async def fake_sync(self, _db, raw, photos, allow_inactive_insert=False):
        reread.append(str(raw["ListingKey"]))

    async def fake_mark(self, _db, removed_at):
        marked.append(dict(removed_at))
        return len(removed_at)

    monkeypatch.setattr(bright_mls_service, "get_listings_by_keys", fake_lookup)
    monkeypatch.setattr(pss.PropertySyncService, "_sync_single_property", fake_sync)
    monkeypatch.setattr(pss.PropertySyncService, "mark_off_market", fake_mark)
    out = _run(pulls.stale_recheck(_StaleDB([["11", "12"]]), pause=0))
    assert reread == ["11"] and marked == [{"12": None}]
    assert out["done"] and (out["checked"], out["updated"], out["off_market"]) == (2, 1, 1)
    assert store["data_pull_stale_recheck"]["day"] == date.today().isoformat()


def test_stale_recheck_marks_nothing_when_bright_cannot_be_asked(monkeypatch):
    _fake_state(monkeypatch)
    marked = []

    async def failing(keys):
        raise RuntimeError("Bright down")

    async def fake_mark(self, _db, removed_at):
        marked.append(removed_at)
        return 0

    monkeypatch.setattr(bright_mls_service, "get_listings_by_keys", failing)
    monkeypatch.setattr(pss.PropertySyncService, "mark_off_market", fake_mark)
    with pytest.raises(RuntimeError):
        _run(pulls.stale_recheck(_StaleDB([["11", "12"]]), pause=0))
    assert marked == []


def test_off_market_recheck_restarts_weekly(monkeypatch):
    week_ago = (date.today() - timedelta(days=8)).isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _fake_state(monkeypatch, {"data_pull_recheck_off_market": {"done": True, "finished": yesterday}})
    assert _run(pulls.recheck_off_market(_StaleDB([]), pause=0))["done"] is True
    store = _fake_state(monkeypatch, {"data_pull_recheck_off_market": {"done": True, "finished": week_ago}})
    out = _run(pulls.recheck_off_market(_StaleDB([[]]), pause=0))  # new pass; nothing off-market -> done again
    assert out["done"] and out["last_pass_finished"] == week_ago
    assert store["data_pull_recheck_off_market"]["finished"] == date.today().isoformat()


# ------------------------------------------------------------------ photo refill

def test_photo_refill_waits_for_the_sold_backfill(monkeypatch):
    _fake_state(monkeypatch, {"data_pull_sold_3y": {"month": "2025-09-01"}})

    async def no_queue(db, keys):
        pass

    monkeypatch.setattr(pulls, "_queue_photo_retries", no_queue)
    monkeypatch.setattr(pss.bright_mls_service, "media_retry_keys", set())
    out = _run(pulls.photo_refill(_StaleDB([]), pause=0))
    assert out == {"waiting_for": "sold_3y", "queue_filled": 0}


def test_refill_only_where_bright_has_more_and_photos_not_hidden(monkeypatch):
    fetched = []

    async def counts(keys):
        return {"1": {"total": 30, "status": "CLOSED-BRIGHT", "hide_off_market": False},
                "2": {"total": 3, "status": "CLOSED-BRIGHT", "hide_off_market": False},  # we already hold 3
                "3": {"total": 40, "status": "CLOSED-BRIGHT", "hide_off_market": True},  # seller hid them
                "4": {"total": 25, "status": "ACTIVE-BRIGHT", "hide_off_market": True}}  # hidden only once off market

    async def media(keys):
        fetched.append(sorted(keys))
        return {k: [f"{k}-{i}.jpg" for i in range(20)] for k in keys}

    class _DB:
        def __init__(self):
            self.updates, self.added = [], []

        async def execute(self, stmt, params=None):
            self.updates.append(params["k"])
            return _Res()

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            pass

    monkeypatch.setattr(bright_mls_service, "get_photo_counts", counts)
    monkeypatch.setattr(bright_mls_service, "get_media_for_properties", media)
    db = _DB()
    res = _run(pulls._refill_photos(db, {"1": 1, "2": 3, "3": 1, "4": 2}))
    assert fetched == [["1", "4"]]
    assert db.updates == ["1", "4"] and res == {"checked": 4, "short": 2, "filled": 2}


# ------------------------------------------------------------------ price history lines

def _ev(kind, status, prev, day, price=500000.0, prev_price=None):
    return SimpleNamespace(kind=kind, status=status, previous_status=prev, price=price, previous_price=prev_price,
                           observed_at=datetime.fromisoformat(day).replace(tzinfo=timezone.utc))


def test_history_lines_hide_false_revivals_and_show_real_changes():
    logged = [
        _ev("status", "ACTIVE-BRIGHT", "OFF_MARKET-BRIGHT", "2026-10-16"),  # false-removal bug: hidden
        _ev("status", "PENDING-BRIGHT", "ACTIVE-BRIGHT", "2026-10-22"),  # same move as the contract date: dropped
        _ev("status", "ACTIVE-BRIGHT", "PENDING-BRIGHT", "2026-11-02"),
        _ev("status", "OFF_MARKET-BRIGHT", "ACTIVE-BRIGHT", "2026-11-20"),
        _ev("status", "ACTIVE-BRIGHT", "OFF_MARKET-BRIGHT", "2027-01-05"),  # after a logged removal: shown
    ]
    lines = phs._status_lines(logged, contract_date="2026-10-20")
    assert [(x["date"], x["event"]) for x in lines] == [
        ("2026-11-02", "Back on market"), ("2026-11-20", "Taken off market"), ("2027-01-05", "Back on market")]


def test_history_shows_a_later_pending_after_under_contract():
    logged = [_ev("status", "ACTIVE UNDER CONTRACT-BRIGHT", "ACTIVE-BRIGHT", "2026-10-21"),
              _ev("status", "PENDING-BRIGHT", "ACTIVE UNDER CONTRACT-BRIGHT", "2026-10-30")]
    lines = phs._status_lines(logged, contract_date="2026-10-20")
    assert [(x["date"], x["event"]) for x in lines] == [("2026-10-30", "Pending")]
