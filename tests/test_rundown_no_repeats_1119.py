"""Today's Rundown shows nothing twice (owner, 11/19): a request still waiting on the agent is under Things to do
(with the names) and not again under What happened today; once handled it's back in the day's log, checked."""
import uuid
from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
async def test_waiting_requests_show_once_with_names():
    import app.main  # noqa: F401
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, Notification, Property, PropertyTour, User
    from app.services.rundown_service import build_rundown
    tag = f"zqrep-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            prop = (await db.execute(select(Property).where(Property.home_status == "ACTIVE-BRIGHT").limit(1))).scalars().first()
            if prop is None:
                pytest.skip("no listings")
            agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Peter", last_name="Stefan", state="PA",
                         brokerage="COMPASS", mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
            db.add(agent)
            await db.commit()
            nicole = Collection(name="Nicole", owner_id=agent.id, status="ACTIVE", share_token=tag + "n", visitor_name="Nicole Testcase")
            lisa = Collection(name="Lisa", owner_id=agent.id, status="ACTIVE", share_token=tag + "l", visitor_name="Lisa Testcase",
                              refine_request_message="Only DASD East please", refine_request_at=datetime.now(timezone.utc))
            db.add_all([nicole, lisa])
            await db.commit()
            waiting = PropertyTour(collection_id=nicole.id, property_id=prop.id, visitor_name="Nicole Testcase", visitor_email=f"{tag}@example.com",
                                   preferred_date="2026-11-22", preferred_time="10:00")
            confirmed = PropertyTour(collection_id=nicole.id, property_id=prop.id, visitor_name="Nicole Testcase", visitor_email=f"{tag}@example.com",
                                     preferred_date="2026-11-23", preferred_time="10:00", confirmed_date="2026-11-23", confirmed_time="10:00")
            db.add_all([waiting, confirmed])
            await db.commit()

            def note(kind, rtype, rid, msg, col=None):
                return Notification(agent_id=agent.id, type=kind, reference_type=rtype, reference_id=rid, title=kind, message=msg,
                                    link="/showcases", is_read=False, collection_id=col)
            db.add_all([note("TOUR_REQUEST", "TOUR", waiting.id, "waiting tour", nicole.id),
                        note("TOUR_REQUEST", "TOUR", confirmed.id, "confirmed tour", nicole.id),
                        note("REFINE_SEARCH_REQUEST", "COLLECTION", lisa.id, "lisa refine", lisa.id)])
            await db.commit()

            r = await build_rundown(db, agent)
            got = {i["message"]: i.get("done") for g in r["happened"] for i in g["items"]}
            asks = {a["title"]: a.get("detail") for a in r["client_activity"]}
            # waiting -> Things to do only, with who and which home
            assert "waiting tour" not in got and "lisa refine" not in got, got
            showing = next(v for k, v in asks.items() if "showing request" in k)
            assert showing.startswith("Nicole Testcase - "), showing
            assert next(v for k, v in asks.items() if "search changed" in k) == "Lisa Testcase"
            # handled -> in the day's log, checked
            assert got["confirmed tour"] == "Confirmed", got
            assert r["happened_count"] == len(got)
    finally:
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(select(User.id).where(User.email.like(f"{tag}%")))).scalars().all()
            if ids:
                await db.execute(delete(Notification).where(Notification.agent_id.in_(ids)))
                cols = (await db.execute(select(Collection.id).where(Collection.owner_id.in_(ids)))).scalars().all()
                if cols:
                    await db.execute(delete(PropertyTour).where(PropertyTour.collection_id.in_(cols)))
                await db.execute(delete(Collection).where(Collection.owner_id.in_(ids)))
                await db.execute(delete(User).where(User.id.in_(ids)))
                await db.commit()
