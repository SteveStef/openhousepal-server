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
    assert "2.5-mile Radnor Trail" in out  # a length, not a distance from town
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
