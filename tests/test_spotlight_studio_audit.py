"""Studio launch audit 11/13 - Spotlight list stats: every page card shows its own Leads count (it
used to always say 0, because only the overall total was sent). A page with leads but no visits yet
still gets its count. Runs against the local Postgres in one rolled-back transaction."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402

NOW = datetime.now(timezone.utc)


def _db_url():
    from dotenv import load_dotenv
    load_dotenv(root_dir / ".env")
    return os.getenv("DATABASE_URL")


async def _scenario():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.models.database import Lead, LeadEvent, PropertyWebsite, PropertyWebsiteView, User
    from app.services.property_website_service import analytics_for_owner

    engine = create_async_engine(_db_url())
    try:
        conn = await engine.connect()
    except Exception as e:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"local database not reachable: {e}")
    trans = await conn.begin()
    db = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
    tag = uuid.uuid4().hex[:8]
    try:
        agent = User(id=str(uuid.uuid4()), email=f"sp-audit-{tag}@example.com", hashed_password="x", first_name="Sia",
                     last_name="Test", state="PA", brokerage="Test Realty", plan_tier="IDX", subscription_status="ACTIVE")
        db.add(agent)
        await db.flush()
        a = PropertyWebsite(id=str(uuid.uuid4()), owner_id=agent.id, slug=f"sp-a-{tag}", status="PUBLISHED", property_snapshot={}, draft_data={})
        b = PropertyWebsite(id=str(uuid.uuid4()), owner_id=agent.id, slug=f"sp-b-{tag}", status="PUBLISHED", property_snapshot={}, draft_data={})
        db.add_all([a, b])
        await db.flush()
        db.add_all([PropertyWebsiteView(property_website_id=a.id, visitor_id="v1"),
                    PropertyWebsiteView(property_website_id=a.id, visitor_id="v2")])
        leads = [Lead(id=str(uuid.uuid4()), owner_id=agent.id, name=f"Lead {i}") for i in range(3)]
        db.add_all(leads)
        await db.flush()
        ev = lambda lead, site, kind: LeadEvent(lead_id=lead.id, source_type=kind, source_id=site.id, summary="x", occurred_at=NOW)
        db.add_all([
            ev(leads[0], a, "property_website_inquiry"),
            ev(leads[0], a, "property_website_tour_request"),  # same person twice = 1
            ev(leads[1], a, "property_website_tour_request"),
            ev(leads[2], b, "property_website_inquiry"),       # page b: a lead, no visits yet
        ])
        await db.flush()
        return await analytics_for_owner(db, agent.id), a.id, b.id
    finally:
        await db.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


def test_each_spotlight_card_gets_its_own_lead_count():
    out, a, b = asyncio.run(_scenario())
    assert out["totals"]["leads"] == 3
    assert out["sites"][a]["leads"] == 2
    assert out["sites"][a]["unique_visitors"] == 2
    assert out["sites"][b] == {"unique_visitors": 0, "total_views": 0, "leads": 1}
