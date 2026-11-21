"""The Agent Hub keeps every closed sale (Sold / Volume / Avg count them all - it used to stop at the
newest 24), and an obvious MLS size typo ("1 sq ft") is left off the card."""
import asyncio
from types import SimpleNamespace

from app.services import agent_hub_live
from app.services.agent_hub_live import HUB_SALES_CAP, _merged_sales, real_sqft


def _sale(i):
    return {"id": f"p{i}", "listing_key": f"K{i}", "address": f"{i} MAIN ST, PHILADELPHIA, PA", "city": "PHILADELPHIA",
            "state": "PA", "price": 300000 + i, "status": "CLOSED-BRIGHT", "photo": None,
            "sold_date": f"20{16 + i % 10}-01-01", "bedrooms": 3, "bathrooms": 2, "living_area": 1 if i == 0 else 1500}


def test_every_sale_is_kept_not_just_24(monkeypatch):
    asked = {}

    async def fake_recent_sales(db, owner, limit=12, **kw):
        asked["limit"] = limit
        return [_sale(i) for i in range(min(limit, 179))]

    import app.services.website.content_adapters as ca
    monkeypatch.setattr(ca, "get_recent_sales", fake_recent_sales)
    owner = SimpleNamespace(mls_id="3192142")
    saved = [{"id": "manual-1", "address": "9 Elm St", "price": 500000, "status": "Sold", "source": "manual", "visible": True}]
    merged = asyncio.run(_merged_sales(None, owner, saved))
    assert asked["limit"] == HUB_SALES_CAP >= 179
    assert len(merged) == 180  # all 179 MLS sales + the hand-added one
    assert sum(1 for s in merged if s.get("source") == "manual") == 1
    typo = next(s for s in merged if s["id"] == "K0")
    assert typo["sqft"] is None
    assert next(s for s in merged if s["id"] == "K1")["sqft"] == 1500


def test_saved_choices_are_not_duplicated(monkeypatch):
    async def fake_recent_sales(db, owner, limit=12, **kw):
        return [_sale(i) for i in range(30)]

    import app.services.website.content_adapters as ca
    monkeypatch.setattr(ca, "get_recent_sales", fake_recent_sales)
    saved = [{"id": "K5", "address": "5 Main St", "price": 300005, "status": "Closed", "source": "bright", "visible": False}]
    merged = asyncio.run(_merged_sales(None, SimpleNamespace(mls_id="X"), saved))
    assert len(merged) == 30
    assert next(s for s in merged if s["id"] == "K5")["visible"] is False  # the agent's hide choice stays


def test_real_sqft():
    assert real_sqft(1) is None
    assert real_sqft(0) is None
    assert real_sqft(None) is None
    assert real_sqft("abc") is None
    assert real_sqft(99) is None
    assert real_sqft(100) == 100
    assert real_sqft(2520.0) == 2520
    assert agent_hub_live.real_sqft("1,034") is None  # not a clean number - leave it off rather than guess
