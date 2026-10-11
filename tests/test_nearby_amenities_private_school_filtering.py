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


def test_private_school_candidate_excludes_public_and_school_like_businesses():
    excluded = [
        place("Wyomissing Area High School", ["school", "secondary_school"]),
        place("Wyomissing Public Charter School", ["school"]),
        place("Bright Horizons Daycare", ["school"]),
        place("Kumon Math and Reading Center", ["school"]),
        place("Main Street Driving School", ["school"]),
        place("Berks Music School", ["school"]),
        place("Reading Area Community College", ["university"]),
    ]
    for candidate in excluded:
        assert svc._private_school_candidate(candidate) is None


def test_private_school_candidate_accepts_traditional_private_schools():
    assert svc._private_school_candidate(place("St. Ignatius Loyola Regional School", ["school", "primary_school"])) == (0, "ELEMENTARY")
    assert svc._private_school_candidate(place("The Independence School", ["school"])) == (0, "PRIVATE SCHOOL")
    assert svc._private_school_candidate(place("Malvern Preparatory School", ["school", "secondary_school"])) == (0, "HIGH SCHOOL")
    assert svc._private_school_candidate(place("Holy Child Academy K-8", ["school"])) == (0, "K-8")
    assert svc._private_school_candidate(place("Wilmington Christian School K-12", ["school"])) == (0, "K-12")


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


@pytest.mark.asyncio
async def test_fetch_parks_uses_distance_rank_preference(monkeypatch):
    captured = {}

    async def fake_search_nearby(*args, **kwargs):
        captured["rank_preference"] = kwargs.get("rank_preference")
        return [place("Hunting Park", ["park"], lat=40.001, lng=-75.0)]

    monkeypatch.setattr(svc, "_search_nearby", fake_search_nearby)
    results = await svc._fetch_parks(None, "fake-key", 40.0, -75.0)

    assert captured["rank_preference"] == "DISTANCE"
    assert [r.name for r in results] == ["Hunting Park"]


def test_specialized_school_is_labeled_and_deprioritized():
    assert svc._private_school_candidate(place("New Story Schools - Wyomissing", ["school"])) == (2, "SPECIALIZED SCHOOL")


@pytest.mark.asyncio
async def test_fetch_private_schools_ranks_useful_schools_before_closer_specialized(monkeypatch):
    async def fake_search_text(*args, **kwargs):
        return [
            place("New Story Schools - Wyomissing", ["school"], lat=40.0, lng=-75.01),
            place("The Independence School", ["school"], lat=40.0, lng=-75.04),
            place("St. Ignatius Loyola Regional School", ["school", "primary_school"], lat=40.0, lng=-75.05),
            place("Wyomissing Area High School", ["school", "secondary_school"], lat=40.0, lng=-75.002),
            place("Kumon Math and Reading Center", ["school"], lat=40.0, lng=-75.003),
        ]

    monkeypatch.setattr(svc, "_search_text", fake_search_text)

    results = await svc._fetch_private_schools(None, "fake-key", 40.0, -75.0)

    assert [(r.place_type, r.name) for r in results] == [
        ("PRIVATE SCHOOL", "The Independence School"),
        ("ELEMENTARY", "St. Ignatius Loyola Regional School"),
    ]


@pytest.mark.asyncio
async def test_fetch_private_schools_uses_specialized_only_when_no_typical_options(monkeypatch):
    async def fake_search_text(*args, **kwargs):
        return [
            place("New Story Schools - Wyomissing", ["school"], lat=40.0, lng=-75.01),
            place("Public Charter School", ["school"], lat=40.0, lng=-75.02),
        ]

    monkeypatch.setattr(svc, "_search_text", fake_search_text)

    results = await svc._fetch_private_schools(None, "fake-key", 40.0, -75.0)

    assert len(results) == 1
    assert results[0].place_type == "SPECIALIZED SCHOOL"
    assert results[0].name == "New Story Schools - Wyomissing"


@pytest.mark.asyncio
async def test_fetch_private_schools_returns_empty_when_no_reliable_private_school(monkeypatch):
    async def fake_search_text(*args, **kwargs):
        return [
            place("Local Public School", ["school"]),
            place("Little Learners Preschool", ["school", "preschool"]),
        ]

    monkeypatch.setattr(svc, "_search_text", fake_search_text)

    assert await svc._fetch_private_schools(None, "fake-key", 40.0, -75.0) == []
