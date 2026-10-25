import pytest

from app.services import nearby_amenities_service as svc


def place(name, types=None, lat=40.0, lng=-75.0):
    google_types = types or ["school"]
    return {
        "displayName": {"text": name},
        "primaryType": google_types[0] if google_types else None,
        "types": google_types,
        "formattedAddress": "1 School Ln, Test City, PA",
        "location": {"latitude": lat, "longitude": lng},
    }


def test_place_dedupe_key_prefers_google_id_and_address():
    same_one = place("The Independence School", ["school"])
    same_two = place("The Independence School Campus", ["school"])
    same_one["id"] = same_two["id"] = "abc123"
    same_two["formattedAddress"] = same_one["formattedAddress"]
    assert svc._place_dedupe_key(same_one)[0] == svc._place_dedupe_key(same_two)[0]


def test_park_sub_features_are_not_parent_park_candidates():
    parent = place("Mayo Beach Park", ["park"])
    pavilion = place("Mayo Beach Park Pavilion", ["park"])
    field = place("Mayo Beach Park Soccer Field", ["park"])
    assert svc._is_parent_park_candidate(parent)
    assert not svc._is_parent_park_candidate(pavilion)
    assert not svc._is_parent_park_candidate(field)


def test_park_parking_areas_and_private_community_amenities_are_excluded():
    """A real Google result, "Ceres Park Back Lot" (primaryType=park), is an overflow parking area
    for the real Ceres Park, not a public recreational destination itself. HOA/community-association
    amenities are real parks a typical home buyer (unless living in that specific community) cannot
    actually use."""
    assert not svc._is_parent_park_candidate(place("Ceres Park Back Lot", ["park"]))
    assert not svc._is_parent_park_candidate(place("Fox Run HOA Park", ["park"]))
    assert not svc._is_parent_park_candidate(place("Willowbrook Homeowners Association Park", ["park"]))
    assert not svc._is_parent_park_candidate(place("Meadowview Community Association Park", ["park"]))
    assert not svc._is_parent_park_candidate(place("Lakeside Clubhouse", ["park"]))
    # a real public park with "community" in its name (not "community association") must still pass
    assert svc._is_parent_park_candidate(place("Gloucester Township Community Park", ["park"]))


def test_park_gateway_entrance_trailhead_names_are_excluded():
    """Switching parks to rankPreference=DISTANCE (see _fetch_parks) surfaces sub-location entries for
    linear/greenway parks that popularity ranking used to hide - real results "Tacony Creek Park - Rorer
    St. Gateway", "..., Bingham St. Gateway" etc. are entrances to the same park, not separate places."""
    assert not svc._is_parent_park_candidate(place("Tacony Creek Park - Rorer St. Gateway", ["park"]))
    assert not svc._is_parent_park_candidate(place("Tacony Creek Park, Bingham St. Gateway", ["park"]))
    assert not svc._is_parent_park_candidate(place("Wissahickon Valley Park Entrance", ["park"]))
    assert not svc._is_parent_park_candidate(place("Pine Barrens Trailhead", ["park"]))
    assert not svc._is_parent_park_candidate(place("Riverwalk Access Point", ["park"]))
    assert svc._is_parent_park_candidate(place("Tacony Creek Park", ["park"]))


def test_park_administrative_buildings_are_excluded():
    """A real result, "Elk Neck SF HQ Building" (Elk Neck State Forest headquarters, primaryType=park),
    won a parks slot under DISTANCE ranking - a state park/forest's own admin building is not a public
    recreational destination."""
    assert not svc._is_parent_park_candidate(place("Elk Neck SF HQ Building", ["park"]))
    assert not svc._is_parent_park_candidate(place("Ridley Creek State Park Headquarters", ["park"]))
    assert not svc._is_parent_park_candidate(place("State Forest Administrative Office", ["park"]))
    assert not svc._is_parent_park_candidate(place("County Parks Maintenance Facility", ["park"]))
    assert not svc._is_parent_park_candidate(place("Fox Run Ranger Station", ["park"]))
    # "HQ" is word-boundary matched so it can't false-positive inside an ordinary word like "hqzone".
    assert svc._HQ_ABBREVIATION_RE.search("elk neck sf hq building")
    assert not svc._HQ_ABBREVIATION_RE.search("hqzone park")


def test_park_subdivision_named_entities_are_excluded():
    """Real results at a single Delaware test location under DISTANCE ranking: "Mount Vernon Estates",
    "Stonegate Sub", "Burnwood Sub.", several with no street-level address at all - bare residential
    subdivision entities Google geocodes with primaryType=park, not an actual recreational destination.
    Narrow and structural: only triggers on a bare development-suffix name with none of Google's richer
    park-descriptor types, so a legitimately named park with the same trailing word is unaffected."""
    assert not svc._is_parent_park_candidate(place("Mount Vernon Estates", ["park"]))
    assert not svc._is_parent_park_candidate(place("Stonegate Sub", ["park"]))
    assert not svc._is_parent_park_candidate(place("Burnwood Sub.", ["park"]))
    assert not svc._is_parent_park_candidate(place("Hillside Acres", ["park"]))
    # A real park carrying one of Google's richer descriptor types is never excluded by this rule,
    # even if it happens to end in the same generic word.
    assert svc._is_parent_park_candidate(place("Whispering Gardens", ["park", "tourist_attraction"]))


def test_dedupe_park_sub_locations_prefers_canonical_entry():
    candidates = [
        place("Tacony Creek Park, Bingham St. Gateway", ["park"], lat=40.001, lng=-75.0),
        place("Tacony Creek Park", ["park"], lat=40.002, lng=-75.0),
        place("Tacony Creek Park, Adams Ave Gateway", ["park"], lat=40.0005, lng=-75.0),
        place("Hunting Park", ["park"], lat=40.003, lng=-75.0),
    ]
    # Simulate rankPreference=DISTANCE surfacing gateway entries that _is_parent_park_candidate would
    # already reject via PARK_SUB_FEATURE_KEYWORDS - dedupe must still collapse correctly on its own
    # for any sub-location wording that check doesn't happen to catch by literal keyword.
    result = svc._dedupe_park_sub_locations(candidates, 40.0, -75.0)
    names = sorted(svc._place_name(p) for p in result)
    assert names == ["Hunting Park", "Tacony Creek Park"]


def test_dedupe_park_sub_locations_keeps_nearest_when_no_canonical_entry():
    candidates = [
        place("Tacony Creek Park, Bingham St. Gateway", ["park"], lat=40.001, lng=-75.0),
        place("Tacony Creek Park, Adams Ave Gateway", ["park"], lat=40.0005, lng=-75.0),
    ]
    result = svc._dedupe_park_sub_locations(candidates, 40.0, -75.0)
    assert len(result) == 1
    assert svc._place_name(result[0]) == "Tacony Creek Park, Adams Ave Gateway"


def park(name, reviews, lat=40.0, lng=-75.0, types=None):
    p = place(name, types or ["park"], lat=lat, lng=lng)
    p["userRatingCount"] = reviews
    p["businessStatus"] = "OPERATIONAL"
    return p


@pytest.mark.asyncio
async def test_fetch_parks_pools_nearest_and_popular_and_drops_unvisited_entries(monkeypatch):
    """10/25 grader round: a mapped tree ("Chinese chestnut 2"), 0-review pocket parks and youth-
    league fields filled real sheets while Ridley Creek State Park 2 miles away never showed."""
    captured = []

    async def fake_search_nearby(*args, **kwargs):
        captured.append(kwargs.get("rank_preference"))
        if kwargs.get("rank_preference") == "DISTANCE":
            return [
                park("Chinese chestnut 2", 0, lat=40.002),
                park("Longview Park", 13, lat=40.008),
                park("Camden-Wyoming Little League", 40, lat=40.009),
                park("Louis W Scott Memorial Park", 60, lat=40.012),
                park("Okehocking Preserve", 366, lat=40.015, types=["nature_preserve", "park"]),
            ]
        return [park("Ridley Creek State Park", 6000, lat=40.03), park("Ridley Creek State Park - Area 3", 40, lat=40.031)]

    monkeypatch.setattr(svc, "_search_nearby", fake_search_nearby)
    results = await svc._fetch_parks(None, "fake-key", 40.0, -75.0)

    assert captured == ["DISTANCE", None]
    assert [r.name for r in results] == ["Longview Park", "Okehocking Preserve", "Ridley Creek State Park"]


@pytest.mark.asyncio
async def test_fetch_parks_relaxes_review_bar_in_rural_areas(monkeypatch):
    async def fake_search_nearby(*args, **kwargs):
        if kwargs.get("rank_preference") == "DISTANCE":
            return [park("Hunn Nature Park", 4, lat=40.01), park("Lebanon Landing", 2, lat=40.02),
                    park("Town Point", 1, lat=40.03), park("Wooden Bridge", 0, lat=40.005)]
        return []

    monkeypatch.setattr(svc, "_search_nearby", fake_search_nearby)
    results = await svc._fetch_parks(None, "fake-key", 40.0, -75.0)

    assert [r.name for r in results] == ["Hunn Nature Park", "Lebanon Landing", "Town Point"]


SURVEY = [
    # name, city, state, lat, lng, lowest grade code, highest grade code, typology, students
    ["JOYFUL NOISE CHRISTIAN SCHOOL", "Marlton", "NJ", 40.001, -75.0, 2, 3, 4, 11],   # PK-K
    ["LITTLE LIGHTS ACADEMY", "Marlton", "NJ", 40.002, -75.0, 2, 7, 4, 30],           # stops at 2nd grade
    ["AIM ACADEMY", "Conshohocken", "PA", 40.003, -75.0, 6, 17, 9, 406],               # special education
    ["SS SIMON AND JUDE SCHOOL", "West Chester", "PA", 40.02, -75.0, 2, 13, 2, 90],     # small enough to get the Google check
    ["TINY CHRISTIAN SCHOOL", "West Chester", "PA", 40.004, -75.0, 3, 13, 6, 12],
    ["THE EPISCOPAL ACADEMY", "Newtown Square", "PA", 40.05, -75.0, 2, 17, 5, 1252],
    ["FAR AWAY ACADEMY", "Nowhere", "PA", 41.0, -75.0, 2, 17, 5, 900],
]


def test_private_school_label_shows_real_grades():
    assert svc._private_school_label(2, 13, 2) == "GRADES PK-8"
    assert svc._private_school_label(14, 17, 3) == "GRADES 9-12"
    assert svc._private_school_label(3, 17, 7) == "GRADES K-12"
    assert svc._private_school_label(6, 17, 9) == "SPECIAL EDUCATION · GRADES 1-12"


def test_private_school_candidates_skip_preschools_and_rank_real_schools_first(monkeypatch):
    monkeypatch.setattr(svc, "_load_private_schools", lambda: SURVEY)
    names = [s["name"] for s in svc._private_school_candidates(40.0, -75.0)]
    assert names == ["SS SIMON AND JUDE SCHOOL", "THE EPISCOPAL ACADEMY", "TINY CHRISTIAN SCHOOL", "AIM ACADEMY"]


def test_private_school_display_name():
    assert svc._private_school_display_name("SS SIMON AND JUDE SCHOOL") == "Saints Simon and Jude School"
    assert svc._private_school_display_name("ST MARY MAGDALEN PARISH") == "St. Mary Magdalen Parish School"


@pytest.mark.asyncio
async def test_fetch_private_schools_uses_google_name_and_skips_closed(monkeypatch):
    monkeypatch.setattr(svc, "_load_private_schools", lambda: SURVEY)

    async def fake_search_text(client, api_key, lat, lng, query, radius, **kwargs):
        if "Simon" in query:
            p = place("Saints Simon and Jude School", ["school"], lat=40.02, lng=-75.0)
            p["businessStatus"] = "CLOSED_PERMANENTLY"
            return [p]
        if "Episcopal" in query:
            p = place("The Episcopal Academy", ["school"], lat=40.0501, lng=-75.0)
            p["businessStatus"] = "OPERATIONAL"
            return [p]
        return []

    monkeypatch.setattr(svc, "_search_text", fake_search_text)
    results = await svc._fetch_private_schools(None, "fake-key", 40.0, -75.0)

    assert [(r.name, r.place_type) for r in results] == [
        ("The Episcopal Academy", "GRADES PK-12"),
        ("Tiny Christian School", "GRADES K-8"),
    ]
