"""Offline checks for the Bright sync safety fixes (10/20) - no network, no database.

- A deletion Bright reports for a listing it still returns must never mark it off-market.
- Listing lookups by key use `in (...)` in chunks of 100 (Bright rejects an `or` chain).
- A merged tax id ("A & B") is looked up one id at a time (Bright rejects the "&").
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402 - loads the app once so service imports resolve in order
from app.services import property_sync_service as pss  # noqa: E402
from app.services.bright_mls_service import bright_mls_service  # noqa: E402


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Row:
    def __init__(self, listing_key, ts):
        self.listing_key = listing_key
        self.modification_timestamp = ts


class _Result:
    rowcount = 0


class FakeDB:
    """Answers the deletion step's one SELECT and records every UPDATE's target keys."""

    def __init__(self, local):
        self.local = local
        self.updated_keys = []

    async def execute(self, stmt):
        sql = str(stmt)
        if sql.lstrip().upper().startswith("SELECT"):
            return _Rows([_Row(k, ts) for k, ts in self.local.items()])
        flat = [k for v in stmt.compile().params.values() if isinstance(v, (list, tuple)) for k in v]
        self.updated_keys += flat
        r = _Result()
        r.rowcount = len(flat)
        return r

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _run(coro):
    return asyncio.run(coro)


def test_still_listed_deletions_are_kept(monkeypatch):
    old = datetime(2026, 10, 16, tzinfo=timezone.utc)
    db = FakeDB({"111": old, "222": old, "333": old})

    async def fake_deletions(_since):
        return [{"key": k, "timestamp": "2026-10-20T13:40:00Z"} for k in ("111", "222", "333")]

    async def fake_lookup(keys):
        # Bright still has 111 (active) and 333 (sold); 222 is really gone.
        return [{"ListingKey": 111, "MlsStatus": "ACTIVE-BRIGHT"}, {"ListingKey": 333, "MlsStatus": "CLOSED-BRIGHT"}]

    reread = []

    async def fake_sync(self, _db, raw, _photos, allow_inactive_insert=False):
        reread.append(str(raw["ListingKey"]))
        return {"type": "UPDATE"}

    monkeypatch.setattr(bright_mls_service, "get_recent_deletions", fake_deletions)
    monkeypatch.setattr(bright_mls_service, "get_listings_by_keys", fake_lookup)
    monkeypatch.setattr(pss.PropertySyncService, "_sync_single_property", fake_sync)

    stats = {"deletions": 0, "errors": 0}
    _run(pss.PropertySyncService()._sync_deletions(db, "2026-10-20T13:35:00Z", stats))

    assert db.updated_keys == ["222"]
    assert sorted(reread) == ["111", "333"]
    assert stats["false_deletions_kept"] == 2
    assert stats["deletions"] == 1


def test_no_deletions_applied_when_bright_cannot_confirm(monkeypatch):
    old = datetime(2026, 10, 16, tzinfo=timezone.utc)
    db = FakeDB({"111": old})

    async def fake_deletions(_since):
        return [{"key": "111", "timestamp": "2026-10-20T13:40:00Z"}]

    async def failing_lookup(keys):
        raise RuntimeError("Bright down")

    monkeypatch.setattr(bright_mls_service, "get_recent_deletions", fake_deletions)
    monkeypatch.setattr(bright_mls_service, "get_listings_by_keys", failing_lookup)

    stats = {"deletions": 0, "errors": 0}
    _run(pss.PropertySyncService()._sync_deletions(db, "2026-10-20T13:35:00Z", stats))

    assert db.updated_keys == []
    assert stats["errors"] == 1


def test_lookup_by_keys_uses_in_and_chunks(monkeypatch):
    calls = []

    async def fake_request(endpoint, params=None):
        calls.append(params["$filter"])
        return {"value": []}

    monkeypatch.setattr(bright_mls_service, "_make_request", fake_request)
    keys = [str(805000000000 + i) for i in range(230)] + ["805000000000", "not-a-key"]
    _run(bright_mls_service.get_listings_by_keys(keys))

    assert len(calls) == 3  # 230 unique numeric keys -> 100 + 100 + 30
    assert all(c.startswith("ListingKey in (") and " or " not in c for c in calls)


def test_merged_tax_id_looked_up_per_part(monkeypatch):
    calls = []

    async def fake_request(endpoint, params=None):
        calls.append(params["$filter"])
        return {"value": [{"ListingKey": 1}]}

    monkeypatch.setattr(bright_mls_service, "_make_request", fake_request)
    rows = _run(bright_mls_service.get_listings_for_tax_id("04-00-00661-218 & 04-00-00661-381"))

    assert calls == ["ListingTaxID eq '04-00-00661-218'", "ListingTaxID eq '04-00-00661-381'"]
    assert len(rows) == 1  # the same listing found under both ids is kept once
