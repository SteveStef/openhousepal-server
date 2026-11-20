"""No AI: for many Showcase searches (towns, townships in any spelling, school districts, high schools, price, beds), the
Showcase search, its home count and the new-listing match must agree home by home (owner, 11/19: "make sure all
bugs are taken care of"). A home the search finds must also be matched when it arrives as a new listing, and a home it
leaves out must not be."""
import random
import uuid
from types import SimpleNamespace as NS

import pytest

FS = ("ACTIVE-BRIGHT", "COMING SOON-BRIGHT")


@pytest.mark.asyncio
@pytest.mark.parametrize("state,seed", [("PA", 1119), ("PA", 7), ("PA", 2026), ("NJ", 11), ("MD", 12), ("VA", 13), ("DE", 14)])
async def test_search_count_and_new_listing_match_agree(state, seed):
    import app.main  # noqa: F401
    from sqlalchemy import delete, select, text
    from app.database import AsyncSessionLocal
    from app.models.database import Collection, CollectionPreferences, Property, User
    from app.services import home_school_service as hs
    from app.services.property_service import property_service
    from app.services.property_sync_service import PropertySyncService

    rnd = random.Random(seed)
    tag = f"zqag-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        # PA districts with a fair number of homes for sale, their townships and (worked out) high schools
        dists = (await db.execute(text(
            "SELECT state, school_district_name d, count(*) n FROM properties WHERE home_status IN ('ACTIVE-BRIGHT','COMING SOON-BRIGHT') "
            "AND state = :st AND school_district_name IS NOT NULL AND is_manual IS NOT TRUE GROUP BY 1, 2 HAVING count(*) BETWEEN 40 AND 900 "
            "ORDER BY md5(school_district_name || :seed) LIMIT 10"), {"seed": str(seed), "st": state})).all()
        if len(dists) < 3:
            pytest.skip("not enough listings")
        searches = []
        for st, d, _n in dists:
            await hs.fill_unit(db, st, hs.norm_district(d))
            twps = [r[0] for r in (await db.execute(text(
                "SELECT township FROM properties WHERE school_district_name = :d AND township <> ''"" AND home_status IN ('ACTIVE-BRIGHT','COMING SOON-BRIGHT') "
                "AND township IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC LIMIT 3"), {"d": d})).all()]
            schools = [s for s in await hs.schools_in(db, [f"{d}, {st}"]) if s["key"] != "unknown"]
            dname = f"{d.title()}, {st}"
            towns = [r[0] for r in (await db.execute(text(
                "SELECT city FROM properties WHERE school_district_name = :d AND home_status IN ('ACTIVE-BRIGHT','COMING SOON-BRIGHT') "
                "AND city IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC LIMIT 2"), {"d": d})).all()]
            if towns:
                # towns ("city"), alone and with the district, any capitalization
                searches.append({"cities": [f"{towns[0].title()}, {st}"]})
                searches.append({"cities": [f"{towns[-1].upper()}, {st}"], "school_districts": [dname], "max_price": 800000})
            searches.append({"school_districts": [dname]})
            if twps:
                spelled = rnd.choice([f"{twps[0].title()}, {st}", f"{twps[0].title()} Township, {st}", twps[0], f"{twps[0].title()} Twp"])
                searches.append({"townships": [spelled]})
                searches.append({"school_districts": [dname], "townships": [spelled], "min_beds": 3})
            if len(schools) >= 2:
                searches.append({"school_districts": [dname], "high_schools": [schools[0]["key"]]})
                searches.append({"school_districts": [dname], "high_schools": [schools[1]["key"]], "min_price": 300000, "max_price": 900000})
                if twps:
                    searches.append({"school_districts": [dname], "townships": [twps[-1].title()], "high_schools": [schools[0]["key"]]})
            # a school without a district does nothing (never pulls unknown homes from everywhere)
            if schools:
                searches.append({"townships": [twps[0].title()] if twps else [], "high_schools": [schools[0]["key"]]} if twps else {"school_districts": [dname]})

        agent = User(email=f"{tag}@example.com", hashed_password="x", first_name="Z", last_name="Q", state="PA", brokerage="B",
                     mls_id=tag.upper(), plan_tier="IDX", subscription_status="ACTIVE")
        db.add(agent)
        await db.commit()
        cols = []
        base = {k: None for k in CollectionPreferences.__table__.columns.keys()}
        for i, srch in enumerate(searches):
            c = Collection(name=f"{tag}-{i}", owner_id=agent.id, status="ACTIVE", share_token=f"{tag}{i}")
            db.add(c)
            await db.commit()
            db.add(CollectionPreferences(collection_id=c.id, cities=srch.get("cities") or [], counties=[], townships=srch.get("townships") or [],
                                         school_districts=srch.get("school_districts") or [], high_schools=srch.get("high_schools") or [],
                                         min_beds=srch.get("min_beds"), min_price=srch.get("min_price"), max_price=srch.get("max_price")))
            await db.commit()
            cols.append((c.id, srch))
        problems = []
        try:
            sync = PropertySyncService()
            checked = 0
            for cid, srch in cols:
                prefs = NS(**{**base, **{"cities": [], "counties": [], "townships": [], "school_districts": [], "high_schools": []}, **srch})
                prefs.state = None
                got = await property_service.get_properties_by_preferences(db, prefs, max_properties=5000)
                found = {p.id for p in got}
                # one house listed twice (two MLS numbers) shows once - linking skips the twin too (_link_property_to_collection)
                houses = {(str(getattr(p, "street_address", "") or "").upper(), str(getattr(p, "city", "") or "").upper()) for p in got}
                n = await property_service.get_properties_count_by_preferences(db, prefs)
                if abs(n - len(found)) > max(1, len(found) // 50):    # the count folds one house listed twice
                    problems.append(f"count {n} vs homes {len(found)} for {srch}")
                # homes it found + nearby homes it didn't (same districts / townships), as new listings
                dn = [x.split(",")[0] for x in srch.get("school_districts") or []]
                near = (Property.school_district_name.ilike(f"%{dn[0]}%") if dn else
                        Property.city.ilike(srch["cities"][0].split(",")[0]) if srch.get("cities") else Property.township.isnot(None))
                pool = (await db.execute(select(Property).where(Property.home_status.in_(FS), Property.is_manual.is_(False), Property.state == state,
                        near).limit(400))).scalars().all()
                sample = rnd.sample(pool, min(25, len(pool))) + [p for p in pool if p.id in found][:15]
                for p in sample:
                    prop = {"id": p.id, "price": p.price, "bedrooms": p.bedrooms, "bathrooms": p.bathrooms, "city": p.city, "state": p.state,
                            "township": p.township, "school_district_name": p.school_district_name, "home_type": p.home_type,
                            "latitude": p.latitude, "longitude": p.longitude, "county": p.county, "living_area": p.living_area,
                            "lot_size_acres": getattr(p, "lot_size_acres", None), "year_built": p.year_built}
                    matched = cid in {c.id for c in await sync._discover_matching_collections(db, prop)}
                    same_house = (str(p.street_address or "").upper(), str(p.city or "").upper()) in houses
                    if matched != (p.id in found) and not (matched and same_house):
                        problems.append(f"{'matched but not found' if matched else 'found but not matched'}: {p.street_address} "
                                        f"({p.township}, {p.school_district_name}, hs={(await db.execute(text("select hs_key from home_schools where property_id = :p"), {"p": p.id})).scalar()}) for {srch}")
                    checked += 1
            assert checked > 150, checked
            assert not problems, "\n".join(problems[:25])
        finally:
            ids = [c for c, _ in cols]
            await db.execute(delete(CollectionPreferences).where(CollectionPreferences.collection_id.in_(ids)))
            await db.execute(delete(Collection).where(Collection.id.in_(ids)))
            await db.execute(delete(User).where(User.id == agent.id))
            await db.commit()
