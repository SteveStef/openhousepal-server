"""IDX town pages held to the Cheat Sheet's rules (user, 10/26) - app/services/areas/town_places_check.py.
Every case here is a real place from the 10/26 check of the Main Line towns."""
from types import SimpleNamespace

import pytest

from app.services import nearby_amenities_service as nas
from app.services.areas import town_places_check as tpc
from app.services.areas.fair_housing_service import clean_area_payload, population_area_label


def gplace(name, primary, types=(), address="1 Main St, Wayne, PA 19087, USA", status="OPERATIONAL", reviews=None, lat=40.04, lng=-75.39):
    p = {"displayName": {"text": name}, "primaryType": primary, "types": [primary, *types], "formattedAddress": address,
         "businessStatus": status, "location": {"latitude": lat, "longitude": lng}}
    if reviews is not None:
        p["userRatingCount"] = reviews
    return p


@pytest.mark.parametrize("ours, theirs, town", [
    ("Taquero", "Taquero MX BYOB", "Media"),
    ("Brick & Brew Gastropub - Malvern", "Brick & Brew Malvern", "Malvern"),
    ("Lankenau Hospital", "Lankenau Medical Center", "Villanova"),
    ("Tired Hands Brewing Company", "Tired Hands Fermentaria Tap Room", "Ardmore"),
    ("Pescatore", "pescatorebyob", "Bala Cynwyd"),
    ("Dua Mediterranean Kitchen", "Dua Restaurant", "Bryn Mawr"),
    ("Chanticleer", "Chanticleer, a pleasure garden", "Wayne"),
    ("Wayne Station", "Wayne", "Wayne"),
])
def test_same_place_matches_the_real_listing(ours, theirs, town):
    assert tpc._same_place(ours, gplace(theirs, "restaurant"), town)


@pytest.mark.parametrize("ours, theirs, town", [
    ("Downtown Media (State Street corridor)", "State Street Pub", "Media"),
    ("Margaret Kuo's", "Margaret Chen Dental", "Wayne"),
    ("Valley Creek Park", "Valley Forge Park", "Malvern"),
])
def test_same_place_rejects_a_different_place(ours, theirs, town):
    assert not tpc._same_place(ours, gplace(theirs, "restaurant"), town)


def test_wrong_kind_of_listing_is_not_a_match():
    apartments = gplace("Apartments at Wayne Station", "apartment_complex")
    station = gplace("Wayne", "train_station", ("transit_station",))
    assert not tpc._kind_fits("transportation", apartments)
    assert tpc._kind_fits("transportation", station)
    assert tpc._kind_fits("parks", gplace("Radnor Trail", "route"))  # a rail-trail is mapped as a route
    # a shopping center lists its restaurants among its types - still a shopping center
    assert tpc._kind_fits("shopping", gplace("Gateway Shopping Center", "shopping_mall", ("restaurant", "grocery_store")))
    assert not tpc._kind_fits("shopping", gplace("State Street Pub", "bar"))
    assert not tpc._kind_fits("downtown_centers", gplace("State Street Pub", "bar"))


def test_google_tagline_is_not_part_of_the_name():
    assert tpc._placed({}, gplace("Chanticleer, a pleasure garden", "garden"), "parks")["name"] == "Chanticleer"
    assert tpc._placed({}, gplace("Stoneleigh: a natural garden", "garden"), "parks")["name"] == "Stoneleigh"
    assert tpc._placed({}, gplace("Bryn Mawr, PA Farmers Market", "market"), "shopping")["name"] == "Bryn Mawr, PA Farmers Market"


def test_matched_station_is_labeled_by_what_it_is():
    station = gplace("Doylestown", "train_station", ("transit_station",), address="Doylestown, PA 18901, USA")
    assert tpc._placed({"name": "Doylestown (SEPTA)", "category": "Bus route"}, station, "transportation")["category"] == "Train station"


@pytest.mark.asyncio
async def test_office_park_and_mall_never_count_as_a_downtown(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")

    async def no_lookup(*a, **k):
        return None

    async def none(*a, **k):
        return []

    async def no_schools(*a, **k):
        return []

    async def no_drive(*a, **k):
        return None

    monkeypatch.setattr(tpc, "_lookup", no_lookup)
    monkeypatch.setattr(tpc, "_hospitals", none)
    monkeypatch.setattr(tpc, "_check_private_schools", no_schools)
    monkeypatch.setattr(tpc, "_drive_miles", no_drive)

    class Db:
        def add(self, _):
            pass

        async def commit(self):
            pass

    area = SimpleNamespace(slug="radnor-pa", name="Radnor", state="PA", centroid_lat=40.04, centroid_lng=-75.36, facts={
        "downtown_centers": {"entities": [
            {"name": "Downtown Wayne", "category": "Town Center/Historic District"},
            {"name": "Radnor Corporate Center", "category": "Commercial/Office Park District"},
            {"name": "King of Prussia Town Center", "category": "Mixed-use retail center"},
            {"name": "Historic Downtown Mall", "category": "Pedestrian shopping, dining, and entertainment district"}]},
    })
    monkeypatch.setattr(tpc, "flag_modified", lambda *a: None)
    await tpc.check_town_places(Db(), area)
    assert [e["name"] for e in area.facts["downtown_centers"]["entities"]] == ["Downtown Wayne", "Historic Downtown Mall"]
    assert [e["name"] for e in area.facts["shopping"]["entities"]] == ["King of Prussia Town Center"]


def test_rules_match_the_cheat_sheet():
    assert tpc._rule_failure("grocery", {}, gplace("Kovacs Market", "convenience_store")).startswith("not a grocery store")
    assert tpc._rule_failure("grocery", {}, gplace("ACME Markets", "supermarket")) is None
    assert tpc._rule_failure("restaurants", {}, gplace("Coyote Crossing", "mexican_restaurant", status="CLOSED_PERMANENTLY")) == "closed (Google)"
    assert tpc._rule_failure("restaurants", {}, gplace("Union League Guard House", "association_or_organization")).startswith("not a place to eat")
    assert tpc._rule_failure("restaurants", {}, gplace("Tired Hands Fermentaria Tap Room", "brewery", ("food",))) is None
    # a real garden with a lower-case Google name is not a "hand-typed listing"
    assert tpc._rule_failure("parks", {}, gplace("Stoneleigh: a natural garden", "garden", reviews=900, address="1829 County Line Rd, Villanova, PA 19085, USA")) is None
    assert tpc._rule_failure("parks", {}, gplace("Chanticleer, a pleasure garden", "garden", reviews=1491, address="786 Church Rd, Wayne, PA 19087, USA")) is None
    # a mapped rail-trail: no street address, no reviews - still a real place
    assert tpc._rule_failure("parks", {}, gplace("Great Allegheny Passage", "route", address="Great Allegheny Passage, Cumberland, MD, USA")) is None


def test_children_hospital_is_not_a_general_hospital():
    chop = gplace("CHOP King of Prussia Hospital, Middleman Family Pavilion", "hospital", address="550 S Goddard Blvd, King of Prussia, PA 19406, USA")
    assert not nas._is_legitimate_full_service_hospital(chop)
    ent = {"category": "HOSPITAL"}
    assert tpc._rule_failure("medical", ent, chop) == "not a general hospital with an emergency room"
    paoli = gplace("Paoli Hospital", "hospital", address="255 W Lancaster Ave, Paoli, PA 19301, USA")
    ent = {"category": "HOSPITAL"}
    assert tpc._rule_failure("medical", ent, paoli) is None and ent["category"] == "HOSPITAL"


def test_office_suite_is_not_a_hospital():
    office = gplace("Medical Center of Princeton", "general_hospital", ("hospital",), address="88 Princeton Hightstown Rd #202, Princeton Junction, NJ 08550, USA")
    assert not nas._is_legitimate_full_service_hospital(office)
    assert tpc._rule_failure("medical", {"category": "HOSPITAL"}, office) is not None  # not even when the research calls it a hospital
    suite = gplace("Riverside Hospital", "hospital", address="10 Main St Suite 300, Dover, DE 19901, USA")
    assert not nas._is_legitimate_full_service_hospital(suite)
    real = gplace("Riddle Hospital", "hospital", address="1068 W Baltimore Pike, Media, PA 19063, USA")
    assert nas._is_legitimate_full_service_hospital(real)
    # Still being built (opens 2028), already on Google with no street address.
    building = gplace("University of Maryland Shore Regional Medical Center", "hospital", address="Easton, MD 21601, USA", lat=38.79, lng=-76.03)
    assert not nas._is_legitimate_full_service_hospital(building)


def test_high_school_does_not_take_the_grade_school_grades():
    """Easton: "Saints Peter & Paul High School" was matched to the PK-8 elementary school."""
    ours, theirs = nas._school_name_tokens("Saints Peter & Paul High School"), nas._school_name_tokens("SAINTS PETER & PAUL ELEMENTARY SCHOOL")
    assert ours & tpc._SCHOOL_LEVEL_WORDS and theirs & tpc._SCHOOL_LEVEL_WORDS and not (ours & theirs & tpc._SCHOOL_LEVEL_WORDS)


def test_survey_early_childhood_centers_drop_out():
    assert tpc._is_early_childhood({"lowest": 2, "highest": 5})       # PK-K
    assert tpc._is_early_childhood({"lowest": 5, "highest": 5})       # one grade
    assert not tpc._is_early_childhood({"lowest": 2, "highest": 17})  # PK-12
    assert tpc._school_label({"lowest": 2, "highest": 13, "typology": 1}) == "Grades PK-8"


def test_research_text_is_cleaned_for_visitors():
    shopping = ('(cite index="10-1">Malvern Borough is a walkable small town with boutique shopping</cite>. '
                'Searched for "Malvern PA grocery stores". All entities are genuine full-service supermarkets. '
                'Paoli Hospital is the nearest, approximately 1.4 miles away. The 2.5-mile Radnor Trail is popular.')
    out = tpc.tidy_research_text(shopping)
    assert "cite" not in out and "Searched" not in out and "entities" not in out and "1.4 miles" not in out
    assert out.startswith("Malvern Borough is a walkable small town")
    assert "The Radnor Trail is popular." in out and "2.5-mile" not in out  # lengths the check can't confirm go (10/27 audit)
    grocery = ("State College has strong grocery access with multiple full-service supermarkets in the IN_AREA category. "
               "Wegmans and Giant are regional chains. All are within walking distance of downtown. "
               "Searched comprehensively for private schools. Nearby state forests have trails.")
    out = tpc.tidy_research_text(grocery)
    assert out == "Wegmans and Giant are regional chains. Nearby state forests have trails."
    assert tpc.tidy_research_text("# Gettysburg, Pennsylvania Gettysburg offers historic charm.") == "Gettysburg offers historic charm."
    assert tpc.tidy_research_text("A park with comprehensive sports and comprehensive sports and recreational amenities.") \
        == "A park with comprehensive sports and recreational amenities."
    assert tpc.tidy_research_text("Homes are close to the the river.") == "Homes are close to the river."


def test_research_downtown_that_is_one_store_moves_to_shopping():
    assert tpc._kind_fits("downtown_centers", gplace("The Outlet Shoppes at Gettysburg", "store"))
    assert tpc._kind_fits("downtown_centers", gplace("Gettysburg Marketplace", "market"))
    assert not tpc._kind_fits("downtown_centers", gplace("Gettysburg", "locality"))


def test_new_hospital_both_sources_agree_on_passes():
    """Penn Highlands State College (opened 2024): not on Medicare's list yet, Google adds pharmacy/store labels."""
    ph = gplace("Penn Highlands State College", "hospital", ("pharmacy", "store", "medical_clinic", "health"),
                address="239 Colonnade Blvd, State College, PA 16803, USA")
    assert tpc._rule_failure("medical", {"category": "HOSPITAL"}, ph) is None
    er = gplace("Penn Highlands State College: Emergency Department", "service", ("health",), address="239 Colonnade Blvd, State College, PA 16803, USA")
    assert tpc._rule_failure("medical", {"category": "HOSPITAL"}, er) is None
    assert tpc._placed({"category": "HOSPITAL"}, er, "medical")["name"] == "Penn Highlands State College"
    kindred = gplace("Kindred Hospital Philadelphia", "hospital", ("store",), address="6129 Palmetto St, Philadelphia, PA 19111, USA")
    assert tpc._rule_failure("medical", {"category": "HOSPITAL"}, kindred) == "not a general hospital with an emergency room"


def test_page_payload_hides_guesses_outside_research_and_admin_lists():
    facts = {
        "market_context": {"stats": [{"label": "Median home price", "value": "$795,000"}]},
        "medical": {"entities": [{"name": "Paoli Hospital", "category": "HOSPITAL", "distance_miles": 1.4}],
                    "_removed": [{"name": "Kindred"}]},
        "demographics": {"population": {"value": 3427, "geography": "place:46792 (state:42) - Malvern borough, Pennsylvania"},
                         "median_age": {"value": 48.9}},
    }
    out, _ = clean_area_payload(facts, None)
    assert "market_context" not in out
    med = out["medical"]
    assert "_removed" not in med and "distance_miles" not in med["entities"][0]  # an unchecked town's guessed "1.4 mi"
    assert med["entities"][0]["category"] == "Hospital"
    assert out["demographics"]["population"]["label"] == "Malvern borough" and "median_age" not in out["demographics"]

    checked = {"_places_check_version": 1, "medical": {"entities": [{"name": "Paoli Hospital", "category": "HOSPITAL", "distance_miles": 5.9, "google_checked": True}]}}
    out, _ = clean_area_payload(checked, None)
    assert out["medical"]["entities"][0]["distance_miles"] == 5.9


def test_school_district_share_is_one_number_on_the_page():
    facts = {"public_schools": {"districts": [{"name": "Radnor Township School District", "share": 48}, {"name": "Tredyffrin/Easttown School District", "share": 38}],
                                "boundary_caveat": "Wayne addresses are split between Radnor Township School District (48% of homes) and Tredyffrin/Easttown School District (38% of homes) - the exact address decides."},
             "boundaries": {"school_districts": [{"name": "Radnor Township School District", "share": 45}, {"name": "Tredyffrin-Easttown School District", "share": 39}]}}
    out, _ = clean_area_payload(facts, None)
    assert [d["share"] for d in out["public_schools"]["districts"]] == [45, 39]
    assert "(45% of homes)" in out["public_schools"]["boundary_caveat"] and "(39% of homes)" in out["public_schools"]["boundary_caveat"]


def test_population_label_says_which_area():
    assert population_area_label("place:81752 (state:42) - Wayne CDP, Pennsylvania") == "Wayne census area"
    assert population_area_label("ZCTA average (19035)") == "ZIP 19035"
    assert population_area_label("ZIP 19035") == "ZIP 19035"
    assert population_area_label("ZCTA average (19341, 19355, 19460)") is None  # averaged head counts - wrong, hidden


@pytest.mark.asyncio
async def test_unresearched_town_is_left_alone(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    area = SimpleNamespace(centroid_lat=40.0, centroid_lng=-75.4, facts={"amenities": {"items": []}, "parks": {"items": []}})
    assert await tpc.check_town_places(None, area) == {"checked": 0}


@pytest.mark.asyncio
async def test_free_calls_running_out_leaves_the_town_waiting_to_finish(monkeypatch):
    import httpx
    from app.services.google_free_tier import GoogleFreeTierPaused

    async def paused(*_a, **_k):
        raise GoogleFreeTierPaused("free calls used up", request=httpx.Request("POST", "https://places.googleapis.com/v1/places:searchText"))

    async def no_hospitals(*_a, **_k):
        raise GoogleFreeTierPaused("free calls used up", request=httpx.Request("POST", "https://places.googleapis.com/v1/places:searchText"))

    class Db:
        def add(self, _o):
            pass

        async def commit(self):
            pass

    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setattr(nas, "_search_text", paused)
    monkeypatch.setattr(nas, "_fetch_hospital_from_google", no_hospitals)
    monkeypatch.setattr(tpc, "flag_modified", lambda *_a: None)
    area = SimpleNamespace(slug="malvern-pa", name="Malvern", state="PA", centroid_lat=40.04, centroid_lng=-75.51,
                           facts={"restaurants": {"entities": [{"name": "Brick & Brew", "city": "Malvern", "distance_miles": 0.2}]}})
    await tpc.check_town_places(Db(), area)
    kept = area.facts["restaurants"]["entities"]
    assert area.facts["_places_check_waiting"] is True
    assert [e["name"] for e in kept] == ["Brick & Brew"] and "distance_miles" not in kept[0]  # kept, no guessed miles


def _fake_db():
    class Db:
        def add(self, _o):
            pass

        async def commit(self):
            pass
    return Db()


@pytest.mark.asyncio
async def test_same_lookup_is_paid_for_once_across_towns(monkeypatch):
    """Neighbouring towns name the same places - the second town reuses the first town's answer."""
    calls = []

    async def search(_c, _k, _lat, _lng, query, _r, **_kw):
        calls.append(query)
        return [gplace("Bryn Mawr Hospital", "hospital", ("general_hospital",), address="130 S Bryn Mawr Ave, Bryn Mawr, PA 19010, USA")]

    async def no_hospitals(*_a, **_k):
        return []

    async def no_drive(*_a, **_k):
        return [None] * len(_a[4])

    tpc._LOOKUP_CACHE.clear()
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setattr(nas, "_search_text", search)
    monkeypatch.setattr(nas, "_fetch_hospital_from_google", no_hospitals)
    monkeypatch.setattr(nas, "_driving_routes", no_drive)
    monkeypatch.setattr(tpc, "flag_modified", lambda *_a: None)
    for town in ("Haverford", "Gladwyne"):
        area = SimpleNamespace(slug=town.lower(), name=town, state="PA", centroid_lat=40.02, centroid_lng=-75.3,
                               facts={"medical": {"entities": [{"name": "Bryn Mawr Hospital", "city": "Bryn Mawr", "category": "HOSPITAL"}]}})
        await tpc.check_town_places(_fake_db(), area)
        assert [e["name"] for e in area.facts["medical"]["entities"]] == ["Bryn Mawr Hospital"]
    assert calls == ["Bryn Mawr Hospital, Bryn Mawr, PA"]
    tpc._LOOKUP_CACHE.clear()


@pytest.mark.asyncio
async def test_finishing_a_paused_check_only_looks_up_what_it_never_reached(monkeypatch):
    calls, routed = [], []

    async def search(_c, _k, _lat, _lng, query, _r, **_kw):
        calls.append(query)
        return [gplace("Brick & Brew Malvern", "restaurant", address="70 E King St, Malvern, PA 19355, USA")]

    async def no_hospitals(*_a, **_k):
        return []

    async def drive(_c, _k, _lat, _lng, points, _s):
        routed.extend(points)
        return [(300, 0.4)] * len(points)

    tpc._LOOKUP_CACHE.clear()
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setattr(nas, "_search_text", search)
    monkeypatch.setattr(nas, "_fetch_hospital_from_google", no_hospitals)
    monkeypatch.setattr(nas, "_driving_routes", drive)
    monkeypatch.setattr(tpc, "flag_modified", lambda *_a: None)
    async def no_schools(*_a, **_k):
        return []

    monkeypatch.setattr(tpc, "_check_private_schools", no_schools)
    done = {"name": "Restaurant Alba", "city": "Malvern", "google_checked": True, "lat": 40.03, "lng": -75.51, "distance_miles": 0.3}
    area = SimpleNamespace(slug="malvern-pa", name="Malvern", state="PA", centroid_lat=40.04, centroid_lng=-75.51,
                           facts={"_places_check_waiting": True,
                                  "restaurants": {"entities": [done, {"name": "Brick & Brew", "city": "Malvern"}]}})
    await tpc.check_town_places(_fake_db(), area)
    assert calls == ["Brick & Brew, Malvern, PA"]          # Alba was checked last time
    assert len(routed) == 1                                  # and keeps its driving distance
    names = {e["name"]: e.get("distance_miles") for e in area.facts["restaurants"]["entities"]}
    assert names == {"Restaurant Alba": 0.3, "Brick & Brew Malvern": 0.4}
    assert "_places_check_waiting" not in area.facts
    tpc._LOOKUP_CACHE.clear()


def test_fix_district_names_restores_shortened_names():
    real = ["Great Valley School District", "Tredyffrin-Easttown School District", "West Chester Area School District"]
    text = "Residents enjoy schools through the Valley School District. Some homes are in Tredyffrin School District."
    assert tpc.fix_district_names(text, real) == (
        "Residents enjoy schools through the Great Valley School District. Some homes are in Tredyffrin-Easttown School District.")
    # real names, a capitalised word in front, and repeated runs stay as they are
    ok = "In Great Valley School District and West Chester Area School District."
    assert tpc.fix_district_names(ok, real) == ok
    fixed = tpc.fix_district_names(text, real)
    assert tpc.fix_district_names(fixed, real) == fixed
    # a part that fits two districts is not guessed
    two = ["Upper Merion Area School District", "Lower Merion School District"]
    assert tpc.fix_district_names("the Merion School District", two) == "the Merion School District"


def test_clean_area_payload_fixes_district_in_summary():
    facts = {"boundaries": {"school_districts": [{"name": "Great Valley School District", "share": 89}]}}
    _, nar = clean_area_payload(facts, {"summary": "Schools are part of the Valley School District."})
    assert nar["summary"] == "Schools are part of the Great Valley School District."


def test_tidy_drops_counts_and_addresses():
    t = tpc.tidy_research_text
    assert t("Family amusement park on the boardwalk with 20 rides, arcade games, and midway attractions.") == \
        "Family amusement park on the boardwalk with rides, arcade games, and midway attractions."
    assert t('Known as the "Mile of Style," Tanger Outlets is home to over 140 brand-name outlets on Route 1.') == \
        'Known as the "Mile of Style," Tanger Outlets is home to brand-name outlets on Route 1.'
    assert t("Tanger Outlets, a major 140+ store outlet mall complex.") == "Tanger Outlets, a major outlet mall complex."
    assert t("Urgent care walk-in clinic at 18941 John J Williams Hwy offering non-emergency outpatient care.") == \
        "Urgent care walk-in clinic offering non-emergency outpatient care."
    assert t("Located at 17616 Coastal Highway, hub for year-round DART bus routes.") == "Hub for year-round DART bus routes."
    assert t("Open-air shopping alley at 42 Rehoboth Avenue in the heart of downtown.") == "Open-air shopping alley in the heart of downtown."


def test_notes_drop_sentences_about_removed_places():
    facts = {"grocery": {"notes": "Rehoboth Beach has robust grocery access. Major chains include Safeway and Stop & Shop.",
                         "entities": [{"name": "Safeway"}], "_removed": [{"name": "Stop & Shop"}]}}
    out, _ = clean_area_payload(facts, None)
    assert out["grocery"]["notes"] == "Rehoboth Beach has robust grocery access."


def test_short_distances_are_straight_line_from_the_center():
    facts = {"_places_check_version": 1, "shopping": {"entities": [
        {"name": "Penny Lane Mall", "lat": 38.716229, "lng": -75.0774569, "distance_miles": 1.1, "google_checked": True},
        {"name": "Far", "lat": 38.7377903, "lng": -75.1418237, "distance_miles": 4.6, "google_checked": True}]}}
    out, _ = clean_area_payload(facts, None, (38.7209454, -75.0760137))
    assert [e["distance_miles"] for e in out["shopping"]["entities"]] == [0.3, 4.6]


def test_office_listing_is_not_a_shop_and_other_place_descriptions_drop():
    office = {"displayName": {"text": "Rehoboth Beach Main Street Inc"}, "primaryType": "non_profit_organization", "types": ["non_profit_organization"],
              "businessStatus": "OPERATIONAL"}
    assert tpc._rule_failure("shopping", {}, office)
    assert not tpc._description_is_about("Rehoboth Mall is a shopping center on Coastal Highway.", "Rehoboth Beach Main Street Inc", "Rehoboth Beach")
    assert tpc._description_is_about("Village By The Sea is another quirky shopping area.", "Village by the Sea", "Rehoboth Beach")
    assert tpc._description_is_about("Discount grocery store with produce.", "ALDI", "Rehoboth Beach")


def test_tidy_drops_total_counts():
    assert tpc.tidy_research_text("A vibrant dining scene with 223 total restaurants reflecting its character.") == \
        "A vibrant dining scene with restaurants reflecting its character."


def test_tidy_edge_cases_from_the_all_towns_scan():
    t = tpc.tidy_research_text
    assert t("Open-air mall with 255,000 sq ft, around 40-50 stores and restaurants.") == "Open-air mall with 255,000 sq ft, stores and restaurants."
    assert t("Downtown featuring over 250 independent retailers and cafes.") == "Downtown featuring independent retailers and cafes."
    assert t("Independent urgent care facility located at 14302 Barton Boulevard SW, providing walk-in care.") == \
        "Independent urgent care facility providing walk-in care."
    assert t("Amtrak station providing passenger rail connections, located at 201 East Harrison Street.") == \
        "Amtrak station providing passenger rail connections."
    assert t("Located at 20055 Shuttle Road with free parking.") == "With free parking."
    assert t("Open until 6 p.m. on weekdays, e.g. on Mondays.") == "Open until 6 p.m. on weekdays, e.g. on Mondays."


def test_a_sentence_leaning_on_a_dropped_one_goes_too():
    text = "Ardmore has two supermarkets (Trader Joe's and Spring Grocery Store). These are the primary options. Parking is easy."
    assert tpc.drop_removed_mentions(text, ["Spring Grocery Store"]) == ""  # never a third sentence pulled up
    text = "Good grocery access. Chains include Safeway and Stop & Shop. A third, weaker sentence."
    assert tpc.drop_removed_mentions(text, ["Stop & Shop"]) == "Good grocery access."
    assert tpc.drop_removed_mentions(text, ["Nothing Here"]) == text
    assert tpc.tidy_research_text("ACME has locations at 500 E State St and 1095 W Baltimore Pike with pharmacy.") == "ACME has locations with pharmacy."


def test_tidy_drops_a_sentence_leaning_on_process_talk():
    assert tpc.tidy_research_text("I searched for private schools near Gettysburg. All are privately operated. Delone Catholic is nearby.") == \
        "Delone Catholic is nearby."


def test_tidy_drops_stale_claims_sizes_hours_and_bus_routes():
    t = tpc.tidy_research_text
    assert t("Served by SEPTA Regional Rail. Local bus service via Routes 92 and regional service via Route 106 provides options.") == \
        "Served by SEPTA Regional Rail."
    assert t("The Rabbit Transit Blue Line includes a stop at the hospital. Parking is free.") == "Parking is free."
    assert t("Shopping center on Route 30 with dining.") == "Shopping center on Route 30 with dining."
    assert t("Modern supermarket open 24 hours with online ordering.") == "Modern supermarket with online ordering."
    assert t("Walk-in care for all ages, open 8 AM to 8 PM daily with no appointment necessary.") == \
        "Walk-in care for all ages with no appointment necessary."
    assert t("3.2-mile trail at the northern edge of the park.") == "Trail at the northern edge of the park."
    assert "264" not in t("A 264-bed acute care hospital.")
    assert t("Parks include Community Park North (71 acres) and Walnut Creek.") == "Parks include Community Park North and Walnut Creek."
    assert t("Arboretum featuring more than three miles of color-coded hiking trails.") == "Arboretum featuring color-coded hiking trails."
    assert t("The Borough maintains over 80 acres across 11 parks. It has a pool.") == ""
    assert t("Lankenau ranks among the top five hospitals in the region. It has an ER.") == ""
    assert t("AFC Havertown is the closest urgent care. Walk-ins welcome.") == "Walk-ins welcome."
    assert t("Media is a dense borough with 5,350 residents. Primary parks include Rose Tree.") == "Primary parks include Rose Tree."


def test_page_rules_for_bus_entities_wrong_town_and_park_labels():
    facts = {"transportation": {"entities": [{"name": "SEPTA Route 106"}, {"name": "Ardmore Station", "category": "Regional rail station"}]},
             "restaurants": {"entities": [{"name": "Green Engine Coffee Co.", "city": "Haverford", "description": "Coffee roaster in Gladwyne, beloved locally."}]},
             "parks": {"entities": [{"name": "Ashbridge Memorial Park", "category": "Regional park"},
                                    {"name": "Cape Henlopen State Park", "category": "State park"}]}}
    out, _ = clean_area_payload(facts, None, None, "Gladwyne")
    assert [e["name"] for e in out["transportation"]["entities"]] == ["Ardmore Station"]
    assert out["restaurants"]["entities"][0]["description"] == "Coffee roaster, beloved locally."
    assert [e["category"] for e in out["parks"]["entities"]] == ["Park", "State park"]


def test_more_address_shapes_and_streets_are_not_shops():
    t = tpc.tidy_research_text
    assert t("Urgent care facility at 1149 Seminole Trail.") == "Urgent care facility."
    assert t("Urgent care center located at 920 High Street, Suite 102.") == "Urgent care center."
    street = {"displayName": {"text": "Louella Drive"}, "primaryType": "route", "types": ["route"], "businessStatus": "OPERATIONAL"}
    assert tpc._rule_failure("shopping", {}, street)


def test_tidy_drops_24_hour_claims_but_not_emergency_rooms():
    t = tpc.tidy_research_text
    assert t("Good grocery access. Wegmans is 24-hour with a cafe.") == "Good grocery access."
    assert t("Hospital with a 24-hour emergency department.") == "Hospital with a 24-hour emergency department."
    assert t("Includes CVS, Wegmans and T.J. Maxx; and 20+ more stores.").endswith("T.J. Maxx; and more stores.")
    assert t("Includes CVS and Wegmans;.") == "Includes CVS and Wegmans."
