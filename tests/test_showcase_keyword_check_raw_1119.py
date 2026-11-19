"""The chat's keyword check on a Showcase (search_showcase_homes) crashed on live (owner, 11/19, Lisa's Showcase:
"The tool returned a system error" twice). Showcase homes load without their whole Bright record
(mls_mirror.selectin_without_raw once the mirror is ready), and the check read it anyway. Now it loads it first."""
import uuid

import pytest


@pytest.mark.asyncio
async def test_keyword_check_reads_homes_loaded_without_raw(monkeypatch):
    import app.main  # noqa: F401
    from sqlalchemy import delete, insert, select
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, Lead, Property, User, collection_properties
    from app.services import mls_mirror
    from app.services.ai import ai_tools

    monkeypatch.setattr(mls_mirror, "ready", lambda: True)          # as on live: homes come without raw_mls_data

    async def no_ai(db, user, config, question, homes, words):      # the word check only - no AI spend
        return words
    monkeypatch.setattr(ai_tools, "_ai_read", no_ai)

    tag = f"zqkw-{uuid.uuid4().hex[:8]}"
    try:
        async with AsyncSessionLocal() as db:
            agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Peter", last_name="Stefan", state="PA",
                         brokerage="COMPASS", mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
            db.add(agent)
            await db.commit()
            lead = Lead(owner_id=agent.id, name="Lisa Testcase", email=f"{tag}-lisa@example.com", represented="no", status="new", tags=[])
            col = Collection(name="Lisa", owner_id=agent.id, status="ACTIVE", share_token=tag, visitor_email=lead.email, visitor_name="Lisa Testcase")
            db.add_all([lead, col])
            await db.commit()
            ids = (await db.execute(select(Property.id).where(Property.raw_mls_data.isnot(None), Property.description.isnot(None)).limit(6))).scalars().all()
            assert ids, "local DB has no listings with MLS data"
            await db.execute(insert(collection_properties), [{"collection_id": col.id, "property_id": i} for i in ids])
            await db.commit()
            lead_id, user_id = lead.id, agent.id

        async with AsyncSessionLocal() as db:     # a fresh session, like a chat turn
            user = await db.get(User, user_id)
            out = await ai_tools._search_showcase_homes(db, user, {"lead_id": lead_id, "look_for": ["east", "garage"]}, {})
            assert out.get("has_showcase") is True and out.get("homes_checked") == len(ids), out
            assert "error" not in out
    finally:
        async with AsyncSessionLocal() as db:
            uids = (await db.execute(select(User.id).where(User.email.like(f"{tag}%")))).scalars().all()
            if uids:
                cols = (await db.execute(select(Collection.id).where(Collection.owner_id.in_(uids)))).scalars().all()
                if cols:
                    await db.execute(delete(collection_properties).where(collection_properties.c.collection_id.in_(cols)))
                await db.execute(delete(Collection).where(Collection.owner_id.in_(uids)))
                await db.execute(delete(Lead).where(Lead.owner_id.in_(uids)))
                await db.execute(delete(User).where(User.id.in_(uids)))
                await db.commit()
