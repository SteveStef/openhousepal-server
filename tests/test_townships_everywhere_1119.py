"""Townships and must-haves in every way a Showcase is made or changed (owner, 11/19): the search, the count and the
new-listing match read a township the same way whatever its spelling; the update form keeps townships; a form (and
the IDX "Build your Showcase", which submits the same form) carries townships + must-haves into the Showcase; the
client's own save can change townships; the chat can make a Showcase with must-haves."""
import uuid
from types import SimpleNamespace as NS

import pytest


def _base():
    from app.models.database import CollectionPreferences
    return {k: None for k in CollectionPreferences.__table__.columns.keys()}


@pytest.mark.asyncio
async def test_township_spellings_find_the_same_homes():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.services.property_service import property_service
    async with AsyncSessionLocal() as db:
        got = {}
        for spelled in ("UWCHLAN, PA", "Uwchlan Township, PA", "Uwchlan Twp, PA", "uwchlan"):
            prefs = NS(**{**_base(), "townships": [spelled]})
            homes = await property_service.get_properties_by_preferences(db, prefs, max_properties=2000)
            n = await property_service.get_properties_count_by_preferences(db, prefs)
            got[spelled] = (len(homes), n)
    if not got["UWCHLAN, PA"][0]:
        pytest.skip("no Uwchlan listings locally")
    assert len(set(got.values())) == 1, got
    assert all(h == n for h, n in got.values()), got


@pytest.mark.asyncio
async def test_district_plus_township_narrows():
    """A school district with one township in it = only that township's homes inside the district (Lisa's case)."""
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.services.property_service import property_service
    async with AsyncSessionLocal() as db:
        whole = await property_service.get_properties_by_preferences(db, NS(**{**_base(), "school_districts": ["Downingtown Area, PA"]}), max_properties=3000)
        part = await property_service.get_properties_by_preferences(
            db, NS(**{**_base(), "school_districts": ["Downingtown Area, PA"], "townships": ["Uwchlan, PA"]}), max_properties=3000)
    if not whole:
        pytest.skip("no Downingtown listings locally")
    assert 0 < len(part) < len(whole)
    assert all((p.township or "").upper() == "UWCHLAN" for p in part)


def test_client_save_can_change_townships():
    from app.schemas.collection_preferences import ShowcaseSelfServePreferencesUpdate
    from app.api.collections_routes import _describe_search_change, _self_serve_payload
    body = ShowcaseSelfServePreferencesUpdate(townships=["Uwchlan, PA"]).model_dump(exclude_unset=True)
    assert body == {"townships": ["Uwchlan, PA"]}
    out = _self_serve_payload(dict(body), saved=NS(townships=[], cities=[], address=None, search_polygon=None))
    assert out["townships"] == ["Uwchlan, PA"] and out.get("search_polygon", "x") is None   # a new area replaces a drawn one
    rows = _describe_search_change({"townships": ["Uwchlan, PA"]})
    assert any(r["label"] == "Townships" and "Uwchlan" in r["value"] for r in rows), rows


def test_form_answers_carry_townships_and_must_haves():
    from app.services.collections_service import CollectionsService
    m = CollectionsService._preferences_from_showcase_form_answers
    got = m({"cities": [], "townships": ["Uwchlan, PA"], "school_districts": ["Downingtown Area, PA"],
             "key_features": ["Pool", "In Uwchlan Township", "Finished basement", "Garage"]})
    assert got["townships"] == ["Uwchlan, PA"]
    assert got["_must_haves"] == ["Pool", "In Uwchlan Township", "Finished basement"]   # at most 3
    # an older form that never asked about townships doesn't wipe the agent's
    old = m({"cities": ["Exton, PA"]})
    assert "townships" not in old and "_must_haves" not in old


@pytest.mark.asyncio
async def test_form_submission_builds_showcase_with_townships_and_must_haves(monkeypatch):
    """Setup My Own Showcase / IDX Build your Showcase -> the Showcase has the township and the must-haves."""
    import app.main  # noqa: F401
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models.database import AgentForm, Collection, CollectionPreferences, FormSubmission, Lead, User, collection_properties
    from app.services import showcase_smart_filter as sf
    from app.services.collections_service import CollectionsService

    async def no_tidy(db, user, wants, known=None):
        return wants, {}
    monkeypatch.setattr(sf, "tidy_wants", no_tidy)
    monkeypatch.setattr(sf, "schedule", lambda *a, **k: None)
    tag = f"zqtw-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Z", last_name="Q", state="PA", brokerage="B",
                     mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
        db.add(agent)
        await db.commit()
        form = AgentForm(owner_id=agent.id, form_type="showcase_request", slug=tag, title="Setup My Own Showcase", questions=[], success_heading="ok")
        db.add(form)
        await db.commit()
        sub = FormSubmission(form_id=form.id, owner_id=agent.id, form_type="showcase_request", answers=[],
                             contact_name="Lisa Testcase", contact_email=f"{tag}-lisa@example.com", contact_phone="6105550111")
        db.add(sub)
        await db.commit()
        answers = {"has_agent": "No, I'm not working with an agent", "school_districts": ["Downingtown Area, PA"],
                   "townships": ["Uwchlan, PA"], "budget_min": "800000", "budget_max": "1600000", "key_features": ["Pool", "In Uwchlan Township"]}
        try:
            res = await CollectionsService.create_or_update_from_form_submission(db, form, sub, answers)
            assert res and res["created"]
            pref = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == res["collection_id"]))).scalar_one()
            assert pref.townships == ["Uwchlan, PA"] and pref.school_districts == ["Downingtown Area, PA"]
            assert (pref.ai_wants or {}).get("wants") == ["Pool", "In Uwchlan Township"]
            homes = (await db.execute(select(collection_properties.c.property_id).where(collection_properties.c.collection_id == res["collection_id"]))).all()
            from app.models.database import Property
            twps = {(await db.get(Property, h[0])).township for h in homes}
            assert twps <= {"UWCHLAN"}, twps
        finally:
            cids = (await db.execute(select(Collection.id).where(Collection.owner_id == agent.id))).scalars().all()
            if cids:
                await db.execute(collection_properties.delete().where(collection_properties.c.collection_id.in_(cids)))
                await db.execute(delete(CollectionPreferences).where(CollectionPreferences.collection_id.in_(cids)))
            from sqlalchemy import text
            await db.execute(text("delete from lead_events where lead_id in (select id from leads where owner_id = :u)"), {"u": agent.id})
            await db.execute(text("delete from notifications where agent_id = :u"), {"u": agent.id})
            await db.execute(delete(Lead).where(Lead.owner_id == agent.id))
            await db.execute(delete(Collection).where(Collection.owner_id == agent.id))
            await db.execute(delete(FormSubmission).where(FormSubmission.owner_id == agent.id))
            await db.execute(delete(AgentForm).where(AgentForm.owner_id == agent.id))
            await db.execute(delete(User).where(User.id == agent.id))
            await db.commit()


@pytest.mark.asyncio
async def test_saving_a_search_keeps_its_townships():
    """The update path (editor, chat, quick edit) used to drop townships without a word."""
    import app.main  # noqa: F401
    from sqlalchemy import delete, select
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, CollectionPreferences, User, collection_properties
    from app.schemas.collection_preferences import CollectionPreferencesUpdate
    from app.services.collection_preferences_service import CollectionPreferencesService
    tag = f"zqtu-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Z", last_name="Q", state="PA", brokerage="B",
                     mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
        db.add(agent)
        await db.commit()
        c = Collection(name=tag, owner_id=agent.id, status="ACTIVE", share_token=tag)
        db.add(c)
        await db.commit()
        db.add(CollectionPreferences(collection_id=c.id, school_districts=["Downingtown Area, PA"]))
        await db.commit()
        try:
            r = await CollectionPreferencesService.update_preferences_and_refresh_properties(
                db, c.id, CollectionPreferencesUpdate(townships=["Uwchlan, PA"], min_price=800000))
            assert r["success"], r
            pref = (await db.execute(select(CollectionPreferences).where(CollectionPreferences.collection_id == c.id))).scalar_one()
            await db.refresh(pref)
            assert pref.townships == ["Uwchlan, PA"]
        finally:
            await db.execute(collection_properties.delete().where(collection_properties.c.collection_id == c.id))
            await db.execute(delete(CollectionPreferences).where(CollectionPreferences.collection_id == c.id))
            await db.execute(delete(Collection).where(Collection.id == c.id))
            await db.execute(delete(User).where(User.id == agent.id))
            await db.commit()


@pytest.mark.asyncio
async def test_chat_makes_a_showcase_with_must_haves():
    import app.main  # noqa: F401
    from sqlalchemy import delete
    from app.database import AsyncSessionLocal
    from app.models.database import User
    from app.services.ai.ai_actions_catalog import _p_new_showcase
    tag = f"zqtc-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Z", last_name="Q", state="PA", brokerage="B",
                     mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
        db.add(agent)
        await db.commit()
        try:
            prep = await _p_new_showcase(db, agent, {"client_name": "Lisa Testcase", "client_email": f"{tag}-l@example.com",
                                                      "school_districts": ["DASD"], "townships": ["Uwchlan"],
                                                      "must_haves": ["Pool", "In Uwchlan Township"]})
            body = prep.payload["body"]
            assert body["school_districts"] == ["Downingtown Area"] and body["townships"] == ["Uwchlan"]
            assert prep.payload["wants"] == ["Pool", "In Uwchlan Township"]
            assert any(l.startswith("Must-haves: Pool, In Uwchlan Township") for l in prep.details), prep.details
        finally:
            await db.execute(delete(User).where(User.id == agent.id))
            await db.commit()


def test_client_save_carries_a_high_school_pick():
    """Clients pick their own high school in Change your search (owner, 11/19) - the save keeps it."""
    from app.schemas.collection_preferences import ShowcaseSelfServePreferencesUpdate
    body = ShowcaseSelfServePreferencesUpdate(school_districts=["Downingtown Area, PA"], high_schools=["nces:1"]).model_dump(exclude_unset=True)
    assert body["high_schools"] == ["nces:1"]


@pytest.mark.asyncio
async def test_buyer_email_search_line_names_the_high_school():
    """Lisa's "your search is now" email says East only, not just the district."""
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.services import home_school_service as hs
    from app.services.showcase_digest_service import school_names_for, search_summary
    async with AsyncSessionLocal() as db:
        from sqlalchemy import text
        r = (await db.execute(text("SELECT state, upper(btrim(school_district_name)) d FROM properties WHERE school_district_name ILIKE '%downingtown%' "
                                   "AND home_status IN ('ACTIVE-BRIGHT','COMING SOON-BRIGHT') GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 1"))).first()
        if r is None:
            pytest.skip("no Downingtown listings locally")
        await hs.fill_unit(db, r.state, r.d)
        east = next(s for s in await hs.schools_in(db, ["Downingtown Area, PA"]) if "east" in s["name"].lower())
        prefs = NS(search_polygon=None, address=None, diameter=None, cities=[], townships=[], school_districts=["Downingtown Area, PA"],
                   high_schools=[east["key"]], min_price=850000, max_price=1500000, min_beds=3, min_baths=None)
        names = await school_names_for(db, prefs)
        line = search_summary(prefs, None, names)
        assert "Downingtown High School East Campus only" in line and "Downingtown Area, PA school district" in line, line
        # a school with no district is ignored here too
        assert await school_names_for(db, NS(high_schools=[east["key"]], school_districts=[])) == []
        assert "only" not in search_summary(NS(**{**vars(prefs), "high_schools": []}), None, [])
