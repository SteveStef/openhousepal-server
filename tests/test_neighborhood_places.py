"""City-neighborhood place rules + Philadelphia school zones (outside grader, 10/27, 30 Philly neighborhoods)."""
from app.services.areas import boundary_sources, neighborhood_places as np_, philly_schools

# A 1-mile-ish square around (39.95, -75.16)
SQUARE = {"type": "Polygon", "coordinates": [[[-75.17, 39.94], [-75.15, 39.94], [-75.15, 39.96], [-75.17, 39.96], [-75.17, 39.94]]]}
NOW = "2026-10-28T00:00:00+00:00"


def _apply(cat, ents):
    facts = {cat: {"entities": ents}}
    return np_.apply("Testville", SQUARE, facts, NOW, (cat,))[cat]


def test_interior_point_is_inside_even_for_a_c_shape():
    c_shape = {"type": "Polygon", "coordinates": [[[0, 0], [3, 0], [3, 1], [1, 1], [1, 2], [3, 2], [3, 3], [0, 3], [0, 0]]]}
    lat, lng = boundary_sources.interior_point(c_shape)
    assert boundary_sources._ring_contains(c_shape["coordinates"][0], lng, lat)


def test_far_places_drop_and_close_ones_are_labelled():
    out = _apply("restaurants", [
        {"name": "Inside Diner", "lat": 39.95, "lng": -75.16, "distance_miles": 0.2},
        {"name": "Corner Cafe", "lat": 39.962, "lng": -75.16, "distance_miles": 0.4},       # ~0.14 mi past
        {"name": "Center City Deli", "lat": 40.05, "lng": -75.16, "distance_miles": 7.3},   # ~6 mi past
    ])
    names = {e["name"]: e for e in out["entities"]}
    assert names["Inside Diner"]["where"] == "In Testville"
    assert names["Corner Cafe"]["where"] == "Just outside Testville"
    assert "Center City Deli" not in names
    assert out["_removed"][0]["name"] == "Center City Deli"


def test_hospitals_are_never_dropped_for_distance():
    out = _apply("medical", [{"name": "Far General", "category": "HOSPITAL", "lat": 40.05, "lng": -75.16}])
    assert out["entities"][0]["name"] == "Far General"


def test_big_park_hides_its_distance():
    out = _apply("parks", [{"name": "Wissahickon Valley Park", "category": "Regional park", "lat": 39.97, "lng": -75.16, "distance_miles": 4.9}])
    e = out["entities"][0]
    assert "distance_miles" not in e and e["access_note"] == "Access varies by entrance"


def test_transit_lines_and_unlocated_stations_go():
    out = _apply("transportation", [
        {"name": "PATCO Rail - Trenton Line (TRE)"},
        {"name": "SEPTA Regional Rail Station (2nd & South Streets)"},
        {"name": "Pier 70 Shopping Center Transit Stop", "category": "Transit station", "lat": 39.95, "lng": -75.16},
        {"name": "Ellsworth-Federal", "category": "Subway station", "lat": 39.95, "lng": -75.166},
    ])
    assert [e["name"] for e in out["entities"]] == ["Ellsworth-Federal"]
    assert out["entities"][0]["category"] == "SEPTA Broad Street Line subway station"


def test_transit_kinds():
    k = np_.transit_kind
    assert k({"name": "PATCO 15th-16th & Locust Street Station"}) == "PATCO rapid-transit station"
    assert k({"name": "9th-10th & Locust", "category": "Regional rail station"}) == "PATCO rapid-transit station"
    assert k({"name": "Berks Station", "category": "Subway station"}) == "SEPTA Market-Frankford Line subway station"
    assert k({"name": "Girard", "category": "Subway station", "lng": -75.159}) == "SEPTA Broad Street Line subway station"
    assert k({"name": "Girard", "category": "Subway station", "lng": -75.136}) == "SEPTA Market-Frankford Line subway station"
    assert k({"name": "Jefferson Station", "category": "Transit station"}) == "SEPTA Regional Rail station"
    assert k({"name": "William H. Gray III 30th Street Station", "category": "Train station"}) == "Amtrak and SEPTA Regional Rail station"
    assert k({"name": "Philadelphia International Airport", "category": "Airport"}) is None


def test_clinics_are_not_hospitals_and_specialty_says_what_it_is():
    out = _apply("medical", [
        {"name": "Temple University Student Health Services", "category": "Hospital", "lat": 39.95, "lng": -75.16},
        {"name": "Kensington Hospital", "category": "HOSPITAL", "lat": 39.95, "lng": -75.16},
    ])
    assert [e["name"] for e in out["entities"]] == ["Kensington Hospital"]
    assert out["entities"][0]["category"].startswith("Specialty hospital")


def test_districts_named_for_somewhere_else_go(monkeypatch):
    monkeypatch.setattr(boundary_sources, "philly_neighborhood_names", lambda: {"Chestnut Hill", "Fishtown", "Germantown"})
    out = _apply("downtown_centers", [
        {"name": "Center City Philadelphia"},
        {"name": "Chestnut Hill Business District (Germantown Avenue)"},
        {"name": "Germantown Avenue Commercial Corridor"},
    ])
    assert [e["name"] for e in out["entities"]] == ["Germantown Avenue Commercial Corridor"]


def test_school_names_from_the_district_file():
    assert philly_schools.display_name("Furness, Horace", "9-12") == "Horace Furness High School"
    assert philly_schools.display_name("McCall, General George A", "K-8") == "General George A. McCall School"
    assert philly_schools.display_name("Fanny Jackson Coppin", "K-8") == "Fanny Jackson Coppin School"
    assert philly_schools.display_name("Marian Anderson Neighborhood Academy", "K-8") == "Marian Anderson Neighborhood Academy"


def test_zoned_schools_from_real_zones():
    # Homes around 9th & Christian (Bella Vista) - zoned by the district's 2025-26 file.
    pts = [(-75.1575 + i * 0.0004, 39.9390 + j * 0.0004) for i in range(6) for j in range(6)]
    z = philly_schools.zoned_schools(pts)
    assert z and z["school_year"] == "2025-26"
    assert all(s["assignment"] == "zoned" for lvl in ("elementary_schools", "high_schools") for s in z[lvl])
    names = {s["name"] for lvl in ("elementary_schools", "middle_schools", "high_schools") for s in z[lvl]}
    # Application-based schools are never "zoned".
    assert not names & {"Central High School", "Julia R. Masterman School", "AMY at James Martin"}


def test_unchecked_walk_and_transit_scores_are_dropped():
    from app.services.areas.town_places_check import tidy_research_text
    out = tidy_research_text("Bella Vista is exceptionally well-served by public transit. The neighborhood has high walk scores and transit scores in the 90s.")
    assert "score" not in out.lower() and out.startswith("Bella Vista is exceptionally")


def test_wikipedia_and_parking_counts_are_dropped():
    from app.services.areas.town_places_check import tidy_research_text
    assert tidy_research_text("A SEPTA station on the Chestnut Hill West Line, with about 35 parking spaces; Wikipedia lists it as not accessible.") == ""
