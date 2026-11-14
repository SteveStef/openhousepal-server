"""MLS mirror columns (owner 11/13): the searched Bright fields as real columns, filled by the
database itself. These checks make sure they can never differ from raw_mls_data:
- the trigger fills them on insert and on every write of raw_mls_data, from any code path;
- writes that don't touch raw_mls_data leave them alone;
- the background fill computes exactly what the trigger does;
- value() returns what raw_mls_data.get() would, mirrored row or not;
- the SQL helpers give the same answers with the switch on or off.
Runs against the local Postgres in one rolled-back transaction (skipped when it isn't reachable)."""
import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import mls_mirror  # noqa: E402

RAW = {"ListingKey": "x", "CloseDate": "2026-03-14", "ClosePrice": 612500.0, "PurchaseContractDate": "2026-02-01",
       "ExpectedOnMarketDate": None, "InternetEntireListingDisplayYN": True, "InternetAddressDisplayYN": False,
       "AssociationFee2": 125.5, "AssociationFee2Frequency": "Monthly", "BuyerFinancing": ["Conventional"],
       "PropertyCondition": [], "PublicRemarks": "Lovely."}


def _db_url():
    from dotenv import load_dotenv
    load_dotenv(root_dir / ".env")
    return os.getenv("DATABASE_URL")


class _Db:
    async def __aenter__(self):
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        self.engine = create_async_engine(_db_url())
        try:
            self.conn = await self.engine.connect()
        except Exception as e:  # noqa: BLE001
            await self.engine.dispose()
            pytest.skip(f"local database not reachable: {e}")
        trig = (await self.conn.exec_driver_sql("SELECT 1 FROM pg_trigger WHERE tgname = 'properties_mls_mirror_trg'")).first()
        await self.conn.rollback()
        if not trig:
            await self.conn.close(); await self.engine.dispose()
            pytest.skip("mirror migration not applied to the local database")
        self.trans = await self.conn.begin()
        self.db = AsyncSession(bind=self.conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        return self.db

    async def __aexit__(self, *exc):
        await self.db.close()
        await self.trans.rollback()
        await self.conn.close()
        await self.engine.dispose()


def _home(raw):
    from app.models.database import Property
    return Property(listing_key=f"MIRROR-TEST-{uuid.uuid4().hex[:10]}", street_address="1 Test Ln", city="WAYNE", state="PA",
                    home_status="CLOSED-BRIGHT", raw_mls_data=raw)


def test_fields_match_the_migration():
    mig = next((root_dir / "alembic" / "versions").glob("*_mls_mirror_columns.py"))
    spec = importlib.util.spec_from_file_location("mig", mig)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.FIELDS == mls_mirror.FIELDS


@pytest.mark.asyncio
async def test_trigger_fills_on_insert_and_on_every_raw_write_only():
    from sqlalchemy import text, update
    from app.models.database import Property
    async with _Db() as db:
        p = _home(dict(RAW))
        db.add(p)
        await db.flush()
        # the ORM object carries what the database filled (eager_defaults) - no stale None
        assert p.mls_close_date == "2026-03-14" and p.mls_close_price == 612500.0
        assert p.mls_internet_display == "true" and p.mls_internet_address_display == "false"
        assert p.mls_buyer_financing == ["Conventional"] and p.mls_property_condition == []
        assert p.mls_expected_on_market_date is None and p.mls_association_fee2 == 125.5
        assert p.mls_mirrored_at is not None
        # a new payload (the sync replaces it whole) -> recomputed
        p.raw_mls_data = {**RAW, "CloseDate": "2026-04-01", "ClosePrice": "599000", "BuyerFinancing": None}
        await db.flush()
        assert (p.mls_close_date, p.mls_close_price, p.mls_buyer_financing) == ("2026-04-01", 599000.0, None)
        # a raw SQL patch of the payload (the off-market marker does this) -> recomputed too
        await db.execute(text("UPDATE properties SET raw_mls_data = raw_mls_data || '{\"ClosePrice\": 1}'::jsonb WHERE id = :i"), {"i": p.id})
        assert (await db.execute(text("SELECT mls_close_price FROM properties WHERE id = :i"), {"i": p.id})).scalar() == 1
        # a write that doesn't touch the payload leaves the mirror alone
        await db.execute(update(Property).where(Property.id == p.id).values(price=1.0))
        assert (await db.execute(text("SELECT mls_close_date FROM properties WHERE id = :i"), {"i": p.id})).scalar() == "2026-04-01"
        # junk numbers never error - NULL instead
        p2 = _home({"ClosePrice": "about 500k", "AssociationFee2": "", "CloseDate": ""})
        db.add(p2)
        await db.flush()
        assert (p2.mls_close_price, p2.mls_association_fee2, p2.mls_close_date) == (None, None, "")
        # no payload at all
        p3 = _home(None)
        db.add(p3)
        await db.flush()
        assert p3.mls_close_date is None and p3.mls_mirrored_at is not None


@pytest.mark.asyncio
async def test_background_fill_matches_the_trigger_on_real_rows():
    """Real local homes: what the fill would write == what the trigger wrote == the payload."""
    from sqlalchemy import text
    async with _Db() as db:
        sets = ", ".join(f"{fn}(raw_mls_data, '{key}') AS {col}" for col, key, fn in
                         [(c, k, mls_mirror._FN[kind]) for c, k, kind in mls_mirror.FIELDS])
        rows = (await db.execute(text(f"SELECT id, raw_mls_data, {sets} FROM properties TABLESAMPLE SYSTEM (2) LIMIT 400"))).mappings().all()
        assert rows
        for r in rows:
            raw = r["raw_mls_data"] if isinstance(r["raw_mls_data"], dict) else {}
            for col, key, kind in mls_mirror.FIELDS:
                got = r[col]
                want = raw.get(key)
                if kind == "text":
                    assert got == (None if want is None else ("true" if want is True else "false" if want is False else str(want) if not isinstance(want, float) else got)), (key, got, want)
                elif kind == "num":
                    assert (got is None and (want in (None, "") or not isinstance(want, (int, float)))) or float(got) == float(want), (key, got, want)
                else:
                    assert got == want, (key, got, want)


@pytest.mark.asyncio
async def test_value_reads_like_the_payload_mirrored_or_not():
    from types import SimpleNamespace
    mirrored = SimpleNamespace(mls_mirrored_at=1, raw_mls_data=None, **{c: None for c, _, _ in mls_mirror.FIELDS})
    mirrored.mls_close_date, mirrored.mls_close_price, mirrored.mls_internet_display = "2026-03-14", 612500, "true"
    mirrored.mls_buyer_financing = ["Cash"]
    assert mls_mirror.value(mirrored, "CloseDate") == RAW["CloseDate"]
    assert mls_mirror.value(mirrored, "ClosePrice") == 612500.0 and isinstance(mls_mirror.value(mirrored, "ClosePrice"), float)
    assert mls_mirror.value(mirrored, "InternetEntireListingDisplayYN") is True
    assert mls_mirror.value(mirrored, "BuyerFinancing") == ["Cash"]
    assert mls_mirror.value(mirrored, "ExpectedOnMarketDate", "none") == "none"
    # not mirrored yet: straight from the payload; and keys that aren't mirrored always come from it
    plain = SimpleNamespace(mls_mirrored_at=None, raw_mls_data=dict(RAW))
    for key in ("CloseDate", "ClosePrice", "InternetAddressDisplayYN", "PublicRemarks"):
        assert mls_mirror.value(plain, key) == RAW[key]
    assert mls_mirror.value(SimpleNamespace(raw_mls_data=None), "CloseDate") is None


@pytest.mark.asyncio
async def test_fill_lock_is_really_released_between_runs():
    """Live 11/13: the one-worker lock was taken on one pooled connection and 'released' on another,
    so it stayed held and every later run skipped with "another worker". Two runs in a row must both
    run, and afterwards nobody may hold the lock."""
    async with _Db():
        pass  # skips when the local database / migration isn't there
    from sqlalchemy import text
    from app.database import engine
    first = await mls_mirror.fill_step(run_seconds=0.2, batch=1)
    second = await mls_mirror.fill_step(run_seconds=0.2, batch=1)
    assert "skipped" not in first and "skipped" not in second, (first, second)
    async with engine.connect() as c:
        held = (await c.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = :k AND granted"),
                                {"k": mls_mirror._LOCK_KEY})).scalar()
        await c.rollback()
    assert held == 0


@pytest.mark.asyncio
async def test_sql_helpers_give_the_same_answers_switch_on_or_off(monkeypatch):
    from sqlalchemy import func, select
    from app.models.database import Property
    async with _Db() as db:
        out = {}
        for mode in ("off", "on"):
            monkeypatch.setenv("OHP_MLS_MIRROR", mode)
            cd, cp = mls_mirror.close_date(), mls_mirror.close_price()
            q = select(func.count(), func.max(cd), func.sum(cp), func.percentile_cont(0.5).within_group(cp.asc()),
                       func.count().filter(mls_mirror.internet_display_ok()), func.count().filter(mls_mirror.internet_address_ok())).where(
                Property.city.in_(["WAYNE", "BRYN MAWR", "ARDMORE"]), cd > "2025-01-01", cd <= "2026-12-31")
            # numbers compared as numbers (the column reads back as float, the payload cast as Decimal)
            out[mode] = tuple(float(v) if v is not None and not isinstance(v, str) else v for v in (await db.execute(q)).one())
        assert out["on"] == out["off"]
        assert out["on"][0] > 0
