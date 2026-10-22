"""Showcase Sold/Removed folder (user, 10/21): a home that goes under contract / pending / sold /
off the market is NOT moved there by itself - it stays in the list with a notice until the buyer
(through their link) or the agent moves it. A home back on the market loses its filing.

The rules are checked offline; the routes (who may move what) run against the local Postgres in one
rolled-back transaction - skipped when that database isn't reachable."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services.collections_service import sold_folder_split, _sale_fields  # noqa: E402

NOW = datetime.now(timezone.utc)


def _p(pid, status):
    return SimpleNamespace(id=pid, home_status=status)


# ---- the rules (offline) ----

def test_off_market_homes_are_not_filed_by_themselves():
    props = [_p("a", "ACTIVE UNDER CONTRACT-BRIGHT"), _p("b", "PENDING-BRIGHT"), _p("c", "CLOSED-BRIGHT"),
             _p("d", "OFF_MARKET-BRIGHT"), _p("e", "WITHDRAWN-BRIGHT"), _p("f", "ACTIVE-BRIGHT")]
    filed, back = sold_folder_split(props, {})
    assert filed == set() and back == []


def test_only_moved_homes_still_off_the_market_are_in_the_folder():
    props = [_p("a", "CLOSED-BRIGHT"), _p("b", "PENDING-BRIGHT"), _p("c", "ACTIVE-BRIGHT"), _p("d", "COMING SOON-BRIGHT")]
    filed, back = sold_folder_split(props, {"a": NOW, "c": NOW, "d": NOW})
    assert filed == {"a"}  # b is off the market but nobody moved it
    assert sorted(back) == ["c", "d"]  # moved earlier, for sale again -> normal card, filing cleared


def test_sale_fields_only_for_closed_homes():
    sold = SimpleNamespace(home_status="CLOSED-BRIGHT", raw_mls_data={"ClosePrice": "512000.0", "CloseDate": "2026-09-07"})
    assert _sale_fields(sold) == {"ClosePrice": 512000.0, "CloseDate": "2026-09-07"}
    pending = SimpleNamespace(home_status="PENDING-BRIGHT", raw_mls_data={"ClosePrice": "1"})
    assert _sale_fields(pending) == {"ClosePrice": None, "CloseDate": None}
    no_price = SimpleNamespace(home_status="CLOSED-BRIGHT", raw_mls_data={"ClosePrice": ""})
    assert _sale_fields(no_price)["ClosePrice"] is None


# ---- the routes, against the local database (rolled back) ----

def _db_url():
    from dotenv import load_dotenv
    load_dotenv(root_dir / ".env")
    return os.getenv("DATABASE_URL")


async def _scenario():
    """Two agents; agent A has two clients' Showcases (s1, s2). Homes in s1: one per status."""
    import httpx
    from sqlalchemy import insert, select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.database import get_db
    from app.main import app as fastapi_app
    from app.models.database import Collection, Property, User, collection_properties
    from app.utils import auth

    engine = create_async_engine(_db_url())
    try:
        conn = await engine.connect()
    except Exception as e:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"local database not reachable: {e}")
    trans = await conn.begin()
    db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    tag = uuid.uuid4().hex[:8]
    out = {}
    try:
        def agent(n):
            return User(id=str(uuid.uuid4()), email=f"sf-{n}-{tag}@example.com", hashed_password="x", first_name=n.title(),
                        last_name="Test", state="PA", brokerage="Test Realty", plan_tier="IDX", subscription_status="ACTIVE",
                        broker_authorized=True)
        a, b = agent("ann"), agent("bob")
        db.add_all([a, b])
        await db.flush()
        statuses = {"uc": "ACTIVE UNDER CONTRACT-BRIGHT", "pend": "PENDING-BRIGHT", "sold": "CLOSED-BRIGHT",
                    "off": "OFF_MARKET-BRIGHT", "wd": "WITHDRAWN-BRIGHT", "act": "ACTIVE-BRIGHT"}
        homes = {}
        for k, st in statuses.items():
            homes[k] = Property(id=str(uuid.uuid4()), listing_key=f"SFTEST-{k}-{tag}", street_address=f"1 {k.title()} Ln",
                                city="Testville", state="PA", home_status=st, price=500000,
                                raw_mls_data={"ClosePrice": 512000, "CloseDate": "2026-09-07"} if k == "sold" else {})
        db.add_all(homes.values())
        s1 = Collection(id=str(uuid.uuid4()), name="Buyer One", owner_id=a.id, share_token=f"sf1-{tag}", is_public=True, status="ACTIVE")
        s2 = Collection(id=str(uuid.uuid4()), name="Buyer Two", owner_id=a.id, share_token=f"sf2-{tag}", is_public=True, status="ACTIVE")
        db.add_all([s1, s2])
        await db.flush()
        await db.execute(insert(collection_properties), [{"collection_id": s1.id, "property_id": h.id, "added_at": NOW} for h in homes.values()])
        await db.execute(insert(collection_properties), [{"collection_id": s2.id, "property_id": homes["pend"].id, "added_at": NOW}])
        await db.flush()

        current = {"user": a}

        async def _db():
            yield db

        async def _user():
            return current["user"]

        fastapi_app.dependency_overrides[get_db] = _db
        fastapi_app.dependency_overrides[auth.get_current_user] = _user
        fastapi_app.dependency_overrides[auth.get_current_active_user] = _user
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            async def shared(token):
                r = await c.get(f"/collections/shared/{token}")
                assert r.status_code == 200, r.text
                return {p["id"]: p for p in r.json()["matchedProperties"]}

            async def agent_view(cid):
                r = await c.get(f"/collections/{cid}/properties")
                assert r.status_code == 200, r.text
                return {p["id"]: p for p in r.json()}

            async def filed_at(cid, pid):
                return (await db.execute(select(collection_properties.c.sold_filed_at).where(
                    collection_properties.c.collection_id == cid, collection_properties.c.property_id == pid))).scalar()

            # 1. Nothing moved by itself: every off-market home is still in the list, not filed.
            buyer = await shared(s1.share_token)
            out["no_auto_move"] = all(buyer[h.id]["soldFiled"] is False for h in homes.values())
            out["statuses_kept"] = {k: buyer[h.id]["MlsStatus"] for k, h in homes.items()} == statuses
            out["sold_price"] = (buyer[homes["sold"].id]["ClosePrice"], buyer[homes["sold"].id]["CloseDate"])
            out["no_sold_price_when_pending"] = buyer[homes["pend"].id]["ClosePrice"] is None
            agent_side = await agent_view(s1.id)
            out["agent_no_auto_move"] = all(agent_side[h.id]["soldFiled"] is False for h in homes.values())

            # 2. The buyer moves the sold home through their own link -> both sides see it filed.
            r = await c.put(f"/collections/shared/{s1.share_token}/properties/{homes['sold'].id}/sold-folder", json={"filed": True})
            out["buyer_move"] = (r.status_code, r.json())
            out["buyer_sees_filed"] = (await shared(s1.share_token))[homes["sold"].id]["soldFiled"]
            out["agent_sees_buyer_move"] = (await agent_view(s1.id))[homes["sold"].id]["soldFiled"]

            # 3. The agent moves the under-contract home for the buyer -> the buyer sees it filed.
            r = await c.put(f"/collections/{s1.id}/properties/{homes['uc'].id}/sold-folder", json={"filed": True})
            out["agent_move"] = (r.status_code, r.json())
            out["buyer_sees_agent_move"] = (await shared(s1.share_token))[homes["uc"].id]["soldFiled"]

            # 4. Moving back out of the folder works for both.
            r = await c.put(f"/collections/shared/{s1.share_token}/properties/{homes['uc'].id}/sold-folder", json={"filed": False})
            out["buyer_unmove"] = (r.status_code, await filed_at(s1.id, homes["uc"].id))

            # 5. Permissions. Buyer Two's link can't touch a home that's only in Buyer One's Showcase...
            r = await c.put(f"/collections/shared/{s2.share_token}/properties/{homes['off'].id}/sold-folder", json={"filed": True})
            out["other_buyer_denied"] = (r.status_code, await filed_at(s1.id, homes["off"].id))
            # ...and filing the pending home through Buyer Two's link only files it in Buyer Two's.
            r = await c.put(f"/collections/shared/{s2.share_token}/properties/{homes['pend'].id}/sold-folder", json={"filed": True})
            out["own_copy_only"] = (r.status_code, await filed_at(s2.id, homes["pend"].id) is not None,
                                    await filed_at(s1.id, homes["pend"].id))
            # A made-up link, and a link that's turned off, get nothing.
            r = await c.put(f"/collections/shared/nope-{tag}/properties/{homes['off'].id}/sold-folder", json={"filed": True})
            out["bad_token"] = r.status_code
            s1.is_public = False
            await db.flush()
            r = await c.put(f"/collections/shared/{s1.share_token}/properties/{homes['off'].id}/sold-folder", json={"filed": True})
            out["private_link"] = r.status_code
            s1.is_public = True
            await db.flush()
            # Another agent can't move homes in Ann's client's Showcase.
            current["user"] = b
            r = await c.put(f"/collections/{s1.id}/properties/{homes['off'].id}/sold-folder", json={"filed": True})
            out["other_agent_denied"] = (r.status_code, await filed_at(s1.id, homes["off"].id))
            current["user"] = a
            # A home that's still for sale can't be filed; a home not in the Showcase is a 404.
            r = await c.put(f"/collections/{s1.id}/properties/{homes['act'].id}/sold-folder", json={"filed": True})
            out["active_refused"] = (r.status_code, await filed_at(s1.id, homes["act"].id))
            r = await c.put(f"/collections/{s2.id}/properties/{homes['wd'].id}/sold-folder", json={"filed": True})
            out["not_in_showcase"] = r.status_code

            # 6. Back on the market: the filed sold home relists -> normal card, filing cleared.
            homes["sold"].home_status = "ACTIVE-BRIGHT"
            await db.flush()
            relisted = (await shared(s1.share_token))[homes["sold"].id]
            out["relisted"] = (relisted["soldFiled"], await filed_at(s1.id, homes["sold"].id))
            # ...and if it goes off the market again it gets a fresh notice (not silently filed).
            homes["sold"].home_status = "PENDING-BRIGHT"
            await db.flush()
            out["off_again"] = (await agent_view(s1.id))[homes["sold"].id]["soldFiled"]
            # Nothing else in the walk-through changed the other buyer's Showcase.
            out["s2_untouched"] = (await shared(s2.share_token))[homes["pend"].id]["soldFiled"]
    finally:
        fastapi_app.dependency_overrides.clear()
        await db.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()
    return out


@pytest.fixture(scope="module")
def scenario():
    return asyncio.run(_scenario())


def test_nothing_moves_by_itself(scenario):
    assert scenario["no_auto_move"] and scenario["agent_no_auto_move"]
    assert scenario["statuses_kept"]
    assert scenario["sold_price"] == (512000.0, "2026-09-07")
    assert scenario["no_sold_price_when_pending"]


def test_buyer_move_shows_for_both(scenario):
    assert scenario["buyer_move"] == (200, {"success": True, "soldFiled": True})
    assert scenario["buyer_sees_filed"] is True
    assert scenario["agent_sees_buyer_move"] is True


def test_agent_move_shows_for_buyer(scenario):
    assert scenario["agent_move"] == (200, {"success": True, "soldFiled": True})
    assert scenario["buyer_sees_agent_move"] is True
    assert scenario["buyer_unmove"] == (200, None)


def test_buyer_can_only_move_homes_in_their_own_showcase(scenario):
    assert scenario["other_buyer_denied"] == (404, None)
    status_code, filed_in_s2, filed_in_s1 = scenario["own_copy_only"]
    assert status_code == 200 and filed_in_s2 and filed_in_s1 is None
    assert scenario["bad_token"] == 404
    assert scenario["private_link"] == 404
    assert scenario["s2_untouched"] is True  # Buyer Two's own move above stands


def test_only_the_owning_agent_can_move(scenario):
    assert scenario["other_agent_denied"] == (404, None)


def test_for_sale_or_missing_homes_are_refused(scenario):
    assert scenario["active_refused"] == (409, None)
    assert scenario["not_in_showcase"] == 404


def test_back_on_the_market_clears_the_filing(scenario):
    assert scenario["relisted"] == (False, None)
    assert scenario["off_again"] is False
