"""11/19 paid test: tapping Video Creation again for the same home paid for the same AI photo read again. A photo the
agent already had read in an earlier video project is copied over for free; another agent's reads are never used."""
import uuid

import pytest


@pytest.mark.asyncio
async def test_earlier_reads_are_reused_only_for_the_same_agent():
    import app.main  # noqa: F401
    from sqlalchemy import delete
    from app.database import AsyncSessionLocal
    from app.models.database import StudioMediaAsset, User
    from app.services.studio_motion_service import _reuse_earlier_reads
    tag = f"zqreuse-{uuid.uuid4().hex[:8]}"
    a_photo, b_photo = f"https://cdn.example.com/{tag}/front.jpg", f"https://cdn.example.com/{tag}/kitchen.jpg"
    read = {"room": "front", "kind": "exterior", "label": "Stately front", "kicker": "Front", "sure": True, "score": 4.5,
            "focusX": 0.4, "analyzed": True}
    async with AsyncSessionLocal() as db:
        me = User(email=f"{tag}-me@example.com", hashed_password="x", first_name="Me", last_name="T", state="PA", brokerage="Test", plan_tier="IDX", subscription_status="ACTIVE")
        other = User(email=f"{tag}-other@example.com", hashed_password="x", first_name="O", last_name="T", state="PA", brokerage="Test", plan_tier="IDX", subscription_status="ACTIVE")
        db.add_all([me, other]); await db.commit()
        try:
            def asset(owner, pool):
                return StudioMediaAsset(owner_id=owner.id, group_id=str(uuid.uuid4()), kind="video", action="motion", status="draft",
                                        source_photo_url=pool[0]["source"], source_kind="listing",
                                        options={"motion": {"pool": pool}})
            db.add(asset(me, [{"source": a_photo, **read}]))
            db.add(asset(other, [{"source": b_photo, **read, "label": "Someone else's read"}]))
            await db.commit()
            new_pool = [{"source": a_photo}, {"source": b_photo}]
            n = await _reuse_earlier_reads(db, me.id, "not-this-one", new_pool)
            assert n == 1
            assert new_pool[0]["analyzed"] and new_pool[0]["label"] == "Stately front" and new_pool[0]["focusX"] == 0.4
            assert not new_pool[1].get("analyzed")          # only another agent read it - paid for normally
            assert await _reuse_earlier_reads(db, me.id, "x", [{"source": a_photo, "analyzed": True, "label": "mine"}]) == 0
        finally:
            await db.execute(delete(StudioMediaAsset).where(StudioMediaAsset.owner_id.in_([me.id, other.id])))
            await db.execute(delete(User).where(User.id.in_([me.id, other.id])))
            await db.commit()
