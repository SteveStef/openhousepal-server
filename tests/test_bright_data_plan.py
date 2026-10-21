"""Offline checks for the Bright data plan, Part 1 (10/21) - no network, no database.

- Deleting a Showcase never deletes a home from the MLS mirror.
- A failed deletions read raises (the caller keeps its checkpoint) instead of returning a partial list.
- The sold backfill stops starting pages once its time budget is spent.
- Price history lookups go 100 parcels per request, paged, with merged ("&") parcels split.
- The price history backfill fetches photos for the listings it adds, and marks every parcel done.
- A home Bright removed shows "Taken off market" on the day it was removed.
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402 - loads the app once so service imports resolve in order
from app.services import bright_data_pulls_service as pulls  # noqa: E402
from app.services import price_history_service as phs  # noqa: E402
from app.services import property_sync_service as pss  # noqa: E402
from app.services.bright_mls_service import bright_mls_service  # noqa: E402
from app.services.collections_service import CollectionsService  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ Showcase delete

class _One:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar(self):
        return 0


class _ShowcaseDB:
    def __init__(self, collection):
        self.collection = collection
        self.statements = []
        self.deleted = []

    async def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        return _One(self.collection)

    async def delete(self, obj):
        self.deleted.append(obj)

    def add(self, obj):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


def test_showcase_delete_keeps_the_homes(monkeypatch):
    homes = [SimpleNamespace(id="p1"), SimpleNamespace(id="p2")]
    collection = SimpleNamespace(id="c1", owner_id="u1", properties=homes, visitor_email=None, visitor_phone=None)
    db = _ShowcaseDB(collection)

    async def no_lead(*_a, **_k):
        return None

    async def no_followups(*_a, **_k):
        return []

    import app.services.leads_service as leads_service
    monkeypatch.setattr(leads_service, "find_lead_by_contact", no_lead)
    monkeypatch.setattr(CollectionsService, "_showcase_only_followups", staticmethod(no_followups))

    assert _run(CollectionsService.delete_collection(db, "c1", "u1")) is True
    assert db.deleted == [collection]
    assert not [s for s in db.statements if s.lstrip().upper().startswith("DELETE")]


# ------------------------------------------------------------------ deletions feed

def test_deletions_read_raises_instead_of_partial(monkeypatch):
    calls = []

    async def fake_request(endpoint, params=None):
        calls.append(params["$skip"])
        if params["$skip"] == 0:
            return {"value": [{"DeleteKey": i, "DeletionTimestamp": "2026-10-21T10:00:00Z"} for i in range(200)]}
        raise RuntimeError("Bright blip")

    monkeypatch.setattr(bright_mls_service, "_make_request", fake_request)
    with pytest.raises(RuntimeError):
        _run(bright_mls_service.get_recent_deletions("2026-10-21T09:00:00Z"))
    assert calls == [0, 200]


def test_deletions_page_cap_trims_the_last_timestamp(monkeypatch):
    async def fake_request(endpoint, params=None):
        page = params["$skip"] // 200
        ts = "2026-10-21T10:00:00Z" if page < 49 else "2026-10-21T11:00:00Z"
        return {"value": [{"DeleteKey": page * 200 + i, "DeletionTimestamp": ts} for i in range(200)]}

    monkeypatch.setattr(bright_mls_service, "_make_request", fake_request)
    out = _run(bright_mls_service.get_recent_deletions("2026-10-21T09:00:00Z"))
    assert len(out) == 49 * 200  # the 11:00 rows come next cycle (a checkpoint at 10:00 can't skip them)
    assert out[-1]["timestamp"] == "2026-10-21T10:00:00Z"


def test_failed_deletions_read_keeps_checkpoint(monkeypatch):
    async def failing(_since):
        raise RuntimeError("Bright down")

    saved = []

    async def fake_update(self, db, timestamp, key=None):
        saved.append(key)

    async def start(self, db, last_sync):
        return last_sync

    monkeypatch.setattr(bright_mls_service, "get_recent_deletions", failing)
    monkeypatch.setattr(pss.PropertySyncService, "update_last_sync_time", fake_update)
    monkeypatch.setattr(pss.PropertySyncService, "_deletion_checkpoint", start)
    with pytest.raises(RuntimeError):
        _run(pss.PropertySyncService()._sync_deletions(object(), "2026-10-21T09:00:00Z", {"deletions": 0, "errors": 0}))
    assert saved == []


# ------------------------------------------------------------------ sold backfill budget

def test_sold_backfill_stops_at_time_budget(monkeypatch):
    clock = [0.0]
    pages = []

    async def fake_get(db, key):
        return {"month": "2024-01-01", "skip": 0}

    async def fake_set(db, key, value):
        pass

    async def fake_window(start, end, top, skip):
        pages.append(skip)
        clock[0] += 100  # each page "takes" 100 s
        return [{"ListingKey": 1}] * 0

    monkeypatch.setattr(pulls, "_get", fake_get)
    monkeypatch.setattr(pulls, "_set", fake_set)
    monkeypatch.setattr(bright_mls_service, "get_closed_window", fake_window)
    monkeypatch.setattr(pulls.time, "monotonic", lambda: clock[0])
    _run(pulls.pull_sold_3y(None, max_pages=60, pause=0, time_budget=480))
    assert len(pages) == 5  # 0,100,...,400 started; at 500 s no new page


# ------------------------------------------------------------------ price history lookups

def test_tax_ids_batched_paged_and_merged_split(monkeypatch):
    calls = []

    async def fake_request(endpoint, params=None):
        calls.append((params["$filter"], params["$skip"]))
        if "'P0'" in params["$filter"] and params["$skip"] == 0:
            # a full page for the first chunk -> asked for the next page
            return {"value": [{"ListingKey": 10_000 + i, "ListingTaxID": "P0"} for i in range(1000)]}
        rows = [{"ListingKey": 1, "ListingTaxID": "04-00-00661-218"}, {"ListingKey": 2, "ListingTaxID": "36-018-,236-,0000-"}]
        return {"value": rows if "'04-00-00661-218'" in params["$filter"] else []}

    monkeypatch.setattr(bright_mls_service, "_make_request", fake_request)
    ids = [f"P{i}" for i in range(150)] + ["04-00-00661-218 & 04-00-00661-381", "36-018-,236-,0000-"]
    out = _run(bright_mls_service.get_listings_for_tax_ids(ids))

    assert all(f.startswith("ListingTaxID in (") for f, _ in calls)
    assert [s for _, s in calls] == [0, 1000, 0]  # chunk 1 paged once more; chunk 2 one page
    second = calls[-1][0]
    assert "'04-00-00661-218'" in second and "'04-00-00661-381'" in second and "&" not in second
    assert "'36-018-,236-,0000-'" in second  # a comma id is asked for whole
    assert len(out["P0"]) == 1000
    assert [r["ListingKey"] for r in out["04-00-00661-218 & 04-00-00661-381"]] == [1]
    assert [r["ListingKey"] for r in out["36-018-,236-,0000-"]] == [2]


class _Scalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items

    def __iter__(self):
        return iter(self._items)


class _Res:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _Scalars(self._items)


class _HistoryDB:
    def __init__(self, parcels, known):
        self.parcels, self.known = parcels, known
        self.added = []

    async def execute(self, stmt, params=None):
        if "price_history_parcels" in str(stmt):
            return _Res(self.parcels)
        return _Res(self.known)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def rollback(self):
        pass


def test_history_backfill_fetches_photos_for_new_listings(monkeypatch):
    db = _HistoryDB(parcels=["T1", "T2", "T3"], known=["100"])
    lookups, media, synced = [], [], []

    async def fake_lookup(group, per_request=100):
        lookups.append(list(group))
        return {"T1": [{"ListingKey": 100}, {"ListingKey": 101}], "T2": [{"ListingKey": 102}], "T3": []}

    async def fake_media(keys):
        media.append(list(keys))
        return {k: [f"{k}-1.jpg", f"{k}-2.jpg"] for k in keys}

    async def fake_sync(self, _db, raw, photos, allow_inactive_insert=False):
        synced.append((str(raw["ListingKey"]), len(photos.get(str(raw["ListingKey"]), []))))
        return {"type": "UPDATE"}

    monkeypatch.setattr(bright_mls_service, "get_listings_for_tax_ids", fake_lookup)
    monkeypatch.setattr(bright_mls_service, "get_media_for_properties", fake_media)
    monkeypatch.setattr(pss.PropertySyncService, "_sync_single_property", fake_sync)

    result = _run(phs.backfill_batch(db, max_parcels=100, pause_seconds=0))
    assert lookups == [["T1", "T2", "T3"]]  # one request for the group
    assert media == [["101", "102"]]  # only listings we don't hold yet
    assert dict(synced) == {"100": 0, "101": 2, "102": 2}
    assert sorted((p.tax_id, p.listings_found) for p in db.added) == [("T1", 2), ("T2", 1), ("T3", 0)]
    assert result["parcels"] == 3 and result["listings_added"] == 2


# ------------------------------------------------------------------ "Taken off market"

def test_history_shows_taken_off_market_on_removal_day(monkeypatch):
    home = SimpleNamespace(
        id="p1", listing_key="222", listing_tax_id=None, street_address="1 Oak St", zipcode="19380",
        home_status="OFF_MARKET-BRIGHT", home_type="SINGLE_FAMILY", price=400000.0, mls_list_date=None,
        price_change_timestamp=None, modification_timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc),
        raw_mls_data={"OhpOffMarketTimestamp": "2026-10-20T13:40:00Z"},
    )

    class _DB:
        async def execute(self, stmt, params=None):
            return _HomeRes()

    class _HomeRes:
        def scalar_one_or_none(self):
            return home

        def scalars(self):
            return _Scalars([])

    async def siblings(_db, subject):
        return [subject]

    monkeypatch.setattr(phs, "_sibling_listings", siblings)
    out = _run(phs.get_history(_DB(), "222"))
    assert out == [{"date": "2026-10-20", "event": "Taken off market", "price": 400000.0}]
