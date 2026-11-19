"""Today's Rundown: what you already took care of gets a green check (owner, 11/19).

"What happened today" showed a follow-up reminder for a text already sent, with nothing saying it was done.
A reminder whose follow-up is checked off says "Done"; a tour request you confirmed says "Confirmed"."""
import uuid
from datetime import datetime, timezone

import pytest


@pytest.mark.asyncio
async def test_done_reminders_and_confirmed_tours_are_marked():
    import app.main  # noqa: F401
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models.database import AgentTask, Collection, Notification, Property, PropertyTour, User
    from app.services.rundown_service import build_rundown
    tag = f"zqdone-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as db:
            agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Peter", last_name="Stefan", state="PA",
                         brokerage="COMPASS", mls_id=tag.upper(), plan_tier="BASIC", subscription_status="ACTIVE")
            db.add(agent)
            await db.commit()
            done = AgentTask(owner_id=agent.id, title="Text Nicole - grandmother's first-floor bedroom search", kind="text",
                             completed_at=now)
            open_ = AgentTask(owner_id=agent.id, title="Call Janet", kind="call")
            due_today = AgentTask(owner_id=agent.id, title="Email Sam about 12 Oak St", kind="email", due_at=now, all_day=True)
            db.add_all([done, open_, due_today])
            await db.commit()
            prop_id = (await db.execute(select(Property.id).limit(1))).scalar()
            col = Collection(name="Nicole", owner_id=agent.id, status="ACTIVE", share_token=tag)
            db.add(col)
            await db.commit()
            tours = []
            if prop_id:
                tours = [PropertyTour(collection_id=col.id, property_id=prop_id, visitor_name="Nicole", visitor_email=f"{tag}-n@example.com",
                                      preferred_date="2026-11-22", preferred_time="10:00", confirmed_date="2026-11-22", confirmed_time="10:00"),
                         PropertyTour(collection_id=col.id, property_id=prop_id, visitor_name="Nicole", visitor_email=f"{tag}-n@example.com",
                                      preferred_date="2026-11-23", preferred_time="11:00")]
                db.add_all(tours)
                await db.commit()

            def note(kind, rtype, rid, title, msg):
                return Notification(agent_id=agent.id, type=kind, reference_type=rtype, reference_id=rid, title=title, message=msg,
                                    link="/leads/tasks", is_read=False)
            db.add_all([note("TASK_REMINDER", "TASK", done.id, "Follow-up reminder", done.title),
                        note("TASK_REMINDER", "TASK", open_.id, "Follow-up reminder", open_.title),
                        note("TASK_REMINDER", "TASK", due_today.id, "Follow-up reminder", due_today.title)]
                       + [note("TOUR_REQUEST", "TOUR", t.id, "Tour request", f"tour {i}") for i, t in enumerate(tours)])
            await db.commit()

            r = await build_rundown(db, agent)
            got = {i["message"]: i.get("done") for g in r["happened"] for i in g["items"]}
            assert got[done.title] == "Done" and got["Call Janet"] is None, got
            # due today = already under Things to do: not repeated under What happened today (owner, 11/19)
            assert any(f["title"].startswith("Email Sam") for f in r["followups"]), r["followups"]
            assert "Email Sam about 12 Oak St" not in got, got
            if tours:
                assert got["tour 0"] == "Confirmed" and got["tour 1"] is None, got
    finally:
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(select(User.id).where(User.email.like(f"{tag}%")))).scalars().all()
            if ids:
                await db.execute(delete(Notification).where(Notification.agent_id.in_(ids)))
                cols = (await db.execute(select(Collection.id).where(Collection.owner_id.in_(ids)))).scalars().all()
                if cols:
                    await db.execute(delete(PropertyTour).where(PropertyTour.collection_id.in_(cols)))
                await db.execute(delete(Collection).where(Collection.owner_id.in_(ids)))
                await db.execute(delete(AgentTask).where(AgentTask.owner_id.in_(ids)))
                await db.execute(delete(User).where(User.id.in_(ids)))
                await db.commit()
