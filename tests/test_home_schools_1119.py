"""Every for-sale home's high school (owner, 11/19): Bright's answer, else the government school map / nearby listings,
kept in home_schools; the Showcase search can keep only some high schools; a place must-have ("In Uwchlan Township")
is checked from the listing itself, free, and its line says it plainly. Uses the local listings (Downingtown Area)."""
import uuid
from types import SimpleNamespace

import pytest

from app.services import home_school_service as hs


def test_one_spelling_per_school():
    assert hs.norm_school("DOWNINGTOWN HIGH SCHOOL EAST CAMPUS") == hs.norm_school("Downingtown H.S. East Campus") == "DOWNINGTOWN HS EAST CAMPUS"
    assert hs.norm_school("Conestoga Senior High School") == "CONESTOGA HS"
    for junk in ("", "  ", "N/A", "call school board", "Unknown", None):
        assert hs.norm_school(junk) is None
    assert hs.nice_name("DOWNINGTOWN HS EAST CAMPUS") == "Downingtown High School East Campus"


def test_grid_vote_needs_agreement():
    g = hs._Grid()
    for i in range(5):
        g.add(f"e{i}", 40.0 + i * 0.001, -75.6, "east")
    g.add("w0", 40.002, -75.6005, "west")
    assert g.vote(40.002, -75.6) == "east" or g.vote(40.002, -75.6) is None
    g2 = hs._Grid()
    for i in range(3):
        g2.add(f"e{i}", 40.0 + i * 0.001, -75.6, "east")
        g2.add(f"w{i}", 40.0 + i * 0.001, -75.6001, "west")
    assert g2.vote(40.001, -75.6) is None       # split down the middle: unknown, never a guess
    assert hs._Grid().vote(40, -75) is None     # nothing nearby


async def _dasd(db):
    from sqlalchemy import text
    r = (await db.execute(text(
        "SELECT state, upper(btrim(school_district_name)) d FROM properties WHERE school_district_name ILIKE '%downingtown%' "
        "AND home_status IN ('ACTIVE-BRIGHT','COMING SOON-BRIGHT') GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 1"))).first()
    return r


@pytest.mark.asyncio
async def test_fill_keeps_bright_first_and_places_blank_homes():
    import app.main  # noqa: F401
    from sqlalchemy import text
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        u = await _dasd(db)
        if u is None:
            pytest.skip("no Downingtown listings locally")
        st = await hs.fill_unit(db, u.state, u.d)
        assert st["homes"] > 0 and st["bright"] > 0
        rows = (await db.execute(text(
            "SELECT h.hs_key, h.hs_name, h.source, h.sure, p.high_school FROM home_schools h JOIN properties p ON p.id = h.property_id "
            "WHERE h.district = :d"), {"d": u.d})).all()
        # listings that name a school (not just the district: "DOWNINGTOWN" is placed per home - 11/19 live data)
        named = [r for r in rows if hs.norm_school(r.high_school) and any(w in (r.high_school or "").upper() for w in ("EAST", "WEST"))]
        assert named and all(r.source == "bright" and r.sure for r in named)
        # every spelling of the East campus lands on one key
        east = {r.hs_key for r in named if "EAST" in (r.high_school or "").upper()}
        assert len(east) == 1, east
        # some blank listings get a school - only where township, map and nearby listings all agree (the rest stay unknown)
        blank = [r for r in rows if not hs.norm_school(r.high_school)]
        if blank:
            assert any(r.hs_key for r in blank)
            assert all(r.source == "agreed" and r.sure for r in blank if r.hs_key)


@pytest.mark.asyncio
async def test_search_keeps_only_that_school_including_blank_listings():
    import app.main  # noqa: F401
    from sqlalchemy import text
    from app.database import AsyncSessionLocal
    from app.models.database import CollectionPreferences
    from app.services.property_service import property_service
    async with AsyncSessionLocal() as db:
        u = await _dasd(db)
        if u is None:
            pytest.skip("no Downingtown listings locally")
        await hs.fill_unit(db, u.state, u.d)
        schools = await hs.schools_in(db, ["Downingtown Area, PA"])
        east = next(s for s in schools if "east" in s["name"].lower())
        assert east["homes"] >= east["named"]
        cols = CollectionPreferences.__table__.columns.keys()
        base = {k: None for k in cols}
        whole = SimpleNamespace(**{**base, "school_districts": ["Downingtown Area, PA"]})
        only = SimpleNamespace(**{**base, "school_districts": ["Downingtown Area, PA"], "high_schools": [east["key"]]})
        all_homes = await property_service.get_properties_by_preferences(db, whole, max_properties=2000)
        east_homes = await property_service.get_properties_by_preferences(db, only, max_properties=2000)
        assert 0 < len(east_homes) < len(all_homes)
        keys = dict((await db.execute(text("SELECT property_id, hs_key FROM home_schools WHERE property_id = ANY(:ids)"),
                                      {"ids": [p.id for p in east_homes]})).all())
        # East homes, plus homes whose school can't be told (kept, marked "School unknown") - never a West home
        assert all(keys.get(p.id) in (east["key"], None) for p in east_homes)
        assert any(keys.get(p.id) == east["key"] for p in east_homes)
        unknown = await hs.unknown_school_ids(db, only, [p.id for p in east_homes])
        # the card marks (owner, 11/20): East homes get the gold pill with the school's name, the rest the amber note
        unk2, match = await hs.school_marks(db, only, [p.id for p in east_homes])
        assert unk2 == unknown and set(match) == {p.id for p in east_homes if keys.get(p.id) == east["key"]}
        assert set(match.values()) == {east["name"]}
        assert unknown == {p.id for p in east_homes if keys.get(p.id) is None}
        # a school alone (no district) never filters - it would pull in every unknown home anywhere
        no_district = SimpleNamespace(**{**base, "high_schools": [east["key"]]})
        assert hs.search_filter(no_district) is None
        # the count the editor shows agrees with the homes it gets
        n = await property_service.get_properties_count_by_preferences(db, only)
        assert abs(n - len(east_homes)) <= max(2, len(east_homes) // 20)
        # listings that leave the school out are in it
        srcs = dict((await db.execute(text("SELECT property_id, source FROM home_schools WHERE property_id = ANY(:ids)"),
                                      {"ids": [p.id for p in east_homes]})).all())
        assert set(srcs.values()) <= {"bright", "agreed", None}      # None = school unknown, kept in (marked)


@pytest.mark.asyncio
async def test_new_listing_quick_key_matches_the_pass():
    import app.main  # noqa: F401
    from sqlalchemy import text
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        u = await _dasd(db)
        if u is None:
            pytest.skip("no Downingtown listings locally")
        await hs.fill_unit(db, u.state, u.d)
        named = (await db.execute(text("SELECT property_id, hs_key FROM home_schools WHERE district = :d AND source = 'bright' "
                                       "AND hs_key IS NOT NULL ORDER BY property_id LIMIT 5"), {"d": u.d})).all()
        placed = (await db.execute(text("SELECT property_id, hs_key FROM home_schools WHERE district = :d AND source <> 'bright' AND sure "
                                        "AND hs_key IS NOT NULL ORDER BY property_id LIMIT 10"), {"d": u.d})).all()
    for r in named:
        assert await hs.quick_key(r.property_id) == r.hs_key
    # a listing that leaves the school out: the quick look agrees with the full pass (township + map + nearby agreed)
    if placed:
        same = [await hs.quick_key(r.property_id) == r.hs_key for r in placed]
        assert sum(same) >= len(same) - 1, same


@pytest.mark.asyncio
async def test_place_must_have_is_exact_and_free():
    import app.main  # noqa: F401
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.database import Property
    from app.services.showcase_smart_filter import match_line, place_verdict, place_wants
    async with AsyncSessionLocal() as db:
        twp = (await db.execute(select(Property.township).where(Property.township.isnot(None), Property.township != "")
                                .where(Property.school_district_name.ilike("%downingtown%")).limit(1))).scalar()
        if not twp:
            pytest.skip("no townships locally")
        name = twp.upper().replace(" TWP", "").replace(" TOWNSHIP", "")
        places = await place_wants(db, [f"In {name.title()} Township", "Pool"])
    assert list(places) == [f"In {name.title()} Township"]
    p = places[f"In {name.title()} Township"]
    assert p["say"].startswith(f"In {name.title()}")
    yes = place_verdict(SimpleNamespace(township=twp, city="X"), p)
    no = place_verdict(SimpleNamespace(township="SOMEWHERE ELSE", city="X"), p)
    assert yes["v"] == "yes" and no["v"] == "no"
    line = match_line({"v": "yes", "found": [{"w": f"In {name.title()} Township", "q": "", "why": p["say"], "place": p["say"]}]})
    assert line["facts"] == [p["say"]] and line["place_wants"] == [f"In {name.title()} Township"]


@pytest.mark.asyncio
async def test_chat_dasd_east_becomes_the_east_high_school():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.services.ai.ai_actions_catalog import _real_districts, _real_high_schools
    async with AsyncSessionLocal() as db:
        u = await _dasd(db)
        if u is None:
            pytest.skip("no Downingtown listings locally")
        await hs.fill_unit(db, u.state, u.d)
        search, lines = {"school_districts": ["DASD East"]}, []
        await _real_districts(db, search, lines)
        await _real_high_schools(db, search, lines, search["school_districts"])
        names = await hs.names_for(db, search["high_schools"])
    assert search["school_districts"] == ["Downingtown Area"]
    assert len(search["high_schools"]) == 1 and "east" in names[search["high_schools"][0]].lower()
    assert any(l.startswith("High school:") and "for sale" in l for l in lines), lines


@pytest.mark.asyncio
async def test_district_card_names_its_high_schools_without_picking():
    import app.main  # noqa: F401
    from app.database import AsyncSessionLocal
    from app.services.ai.ai_actions_catalog import _real_districts, _real_high_schools
    async with AsyncSessionLocal() as db:
        u = await _dasd(db)
        if u is None:
            pytest.skip("no Downingtown listings locally")
        await hs.fill_unit(db, u.state, u.d)
        search, lines = {"school_districts": ["Downingtown Area"]}, []
        await _real_districts(db, search, lines)
        await _real_high_schools(db, search, lines, search["school_districts"])
    assert "high_schools" not in search
    assert any("high schools (" in l and "set high_schools" in l for l in lines), lines


@pytest.mark.asyncio
async def test_matcher_respects_high_schools():
    """A new listing joins a Showcase limited to its high school, not one limited to another."""
    import app.main  # noqa: F401
    from sqlalchemy import delete, select, text
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, CollectionPreferences, Property, User
    from app.services.property_sync_service import PropertySyncService
    tag = f"zqhs-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        u = await _dasd(db)
        if u is None:
            pytest.skip("no Downingtown listings locally")
        await hs.fill_unit(db, u.state, u.d)
        schools = await hs.schools_in(db, ["Downingtown Area, PA"])
        if len(schools) < 2:
            pytest.skip("one school")
        r = (await db.execute(text("SELECT h.property_id, h.hs_key FROM home_schools h WHERE h.district = :d AND h.hs_key = :k "
                                   "AND h.source = 'bright' ORDER BY h.property_id LIMIT 1"),
                              {"d": u.d, "k": schools[0]["key"]})).first()
        p = await db.get(Property, r.property_id)
        agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Z", last_name="Q", state="PA", brokerage="B",
                     mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
        db.add(agent)
        await db.commit()
        made = {}
        for label, key in (("same", schools[0]["key"]), ("other", schools[1]["key"])):
            c = Collection(name=f"{tag}-{label}", owner_id=agent.id, status="ACTIVE", share_token=f"{tag}{label}")
            db.add(c)
            await db.commit()
            db.add(CollectionPreferences(collection_id=c.id, school_districts=["Downingtown Area, PA"], high_schools=[key]))
            await db.commit()
            made[label] = c.id
        try:
            prop = {"id": p.id, "price": p.price, "bedrooms": p.bedrooms, "bathrooms": p.bathrooms, "city": p.city, "state": p.state,
                    "township": p.township, "school_district_name": p.school_district_name, "home_type": p.home_type,
                    "latitude": p.latitude, "longitude": p.longitude, "county": p.county, "living_area": p.living_area,
                    "lot_size_acres": getattr(p, "lot_size_acres", None)}
            got = {c.id for c in await PropertySyncService()._discover_matching_collections(db, prop)}
            assert made["same"] in got and made["other"] not in got
        finally:
            await db.execute(delete(CollectionPreferences).where(CollectionPreferences.collection_id.in_(list(made.values()))))
            await db.execute(delete(Collection).where(Collection.id.in_(list(made.values()))))
            await db.execute(delete(User).where(User.id == agent.id))
            await db.commit()
