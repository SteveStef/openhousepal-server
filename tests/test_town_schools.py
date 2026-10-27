"""Town-page schools (outside grader, 10/27) - the private-school survey matcher in
town_places_check, the page-time school pass (school_display) and the charter rule in
content_adapters. Every case is a real school from the 19-town fact check."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app.api.site_routes  # noqa: F401  (loads content_adapters in the app's import order)
from app.services.areas import school_display as sd
from app.services.areas import town_places_check as tpc
from app.services.website import content_adapters as ca

# Survey grade codes: 2 = PK, 3/4 = K, grade n = n + 5.
PK, K, G = 2, 4, (lambda n: n + 5)


def row(name, city, lat, lng, lo, hi, typology=2, students=300, miles=1.0):
    return {"name": name, "city": city, "lat": lat, "lng": lng, "lowest": lo, "highest": hi,
            "typology": typology, "students": students, "miles": miles}


SACRED_HEART_HAVERTOWN = row("SACRED HEART CATHOLIC SCHOOL", "Havertown", 39.97222, -75.31188, PK, G(8), miles=2.6)
SACRED_HEART_BRYN_MAWR = row("SACRED HEART ACADEMY BRYN MAWR", "Bryn Mawr", 40.01267, -75.34506, K, G(12), typology=3, students=190, miles=3.2)
FRIENDS_HAVERFORD = row("FRIENDS SCHOOL HAVERFORD", "Haverford", 40.01314, -75.30898, PK, G(5), typology=5, students=33, miles=1.0)
CLASSICAL = row("MAIN LINE CLASSICAL ACADEMY INC", "Bryn Mawr", 40.01441, -75.34587, K, G(11), typology=8, students=85, miles=1.5)
CASKEY = row("CASKEY TORAH ACADEMY OF GREATER PHILADELPHIA", "Wynnewood", 39.99999, -75.28629, PK, G(8), typology=5, students=307, miles=0.5)
FRIENDS_CENTRAL = row("FRIENDS CENTRAL SCHOOL", "Wynnewood", 39.98489, -75.26288, PK, G(12), typology=5, students=791, miles=3.0)
CARROLL_SHORT = row("ARCHBISHOP CARROLL HIGH SCHOOL", "Malvern", 40.04638, -75.35381, G(9), G(12), students=686, miles=0.8)
CARROLL_FULL = row("ARCHBISHOP JOHN CARROLL HIGH SCHOOL", "Radnor", 40.04638, -75.35381, G(9), G(12), students=686, miles=0.8)
SPP_ELEMENTARY = row("SAINTS PETER & PAUL ELEMENTARY SCHOOL", "Easton", 38.76865, -76.06192, PK, G(8), typology=1, students=363, miles=0.5)
ST_PETERS = row("ST PETERS SCHOOL", "Philadelphia", 39.94269, -75.14811, PK, G(8), typology=7, students=121, miles=1.7)
STUART = row("STUART COUNTRY DAY SCHOOL OF THE SACRED HEART", "Princeton", 40.36749, -74.6858, PK, G(12), typology=3, miles=2.0)
MT_CARMEL = row("OUR LADY OF MT CARMEL SCHOOL", "Doylestown", 40.31133, -75.12343, PK, G(8), miles=0.4)


def match(name, rows, grades=None):
    return tpc._best_survey_match({"name": name, "grades_served": grades}, rows)


# ---------------------------------------------------------------- the survey matcher

def test_best_name_wins_over_the_nearest_close_name():
    # Ardmore: "Sacred Heart Academy" is in Bryn Mawr (K-12), not Sacred Heart Catholic School, Havertown (PK-8).
    assert match("Sacred Heart Academy", [SACRED_HEART_HAVERTOWN, SACRED_HEART_BRYN_MAWR]) is SACRED_HEART_BRYN_MAWR


def test_a_big_grade_clash_is_not_the_same_school():
    # The research's own grades (6-12) can't be a PK-8 school - with no better row, no match at all.
    assert match("Sacred Heart Academy", [SACRED_HEART_HAVERTOWN], grades="6 - 12") is None
    # ...but a research span that only overstates (Torah Academy "Pre-K - 12", really PK-8) still matches.
    assert match("Torah Academy of Greater Philadelphia", [CASKEY], grades="Pre-K - 12") is CASKEY


@pytest.mark.parametrize("research, survey", [
    ("The Haverford School", FRIENDS_HAVERFORD),          # Haverford: was shown as PK-5
    ("Main Line Montessori School", CLASSICAL),           # Villanova: was shown as K-11
    ("Princeton Academy of the Sacred Heart", STUART),    # two schools sharing "Sacred Heart"
])
def test_a_different_school_sharing_words_is_not_matched(research, survey):
    assert match(research, [survey]) is None


@pytest.mark.parametrize("research, survey", [
    ("Torah Academy of Greater Philadelphia", CASKEY),
    ("Friends' Central School Lower School", FRIENDS_CENTRAL),
    ("Our Lady of Mount Carmel School", MT_CARMEL),       # "MT" in the survey
    ("St Peter's School", ST_PETERS),                     # the apostrophe used to split "peter" / "s"
    ("Fusion Academy Malvern", row("FUSION MALVERN", "Malvern", 40.04923, -75.51779, G(6), G(12), typology=8, students=68)),
])
def test_the_same_school_is_matched(research, survey):
    assert match(research, [survey]) is survey


def test_level_words_still_keep_a_high_school_off_the_elementary():
    # Easton: Saints Peter and Paul HIGH School serves 9-12; the PK-8 row is the elementary school.
    assert match("Saints Peter and Paul High School", [SPP_ELEMENTARY]) is None


def test_a_school_listed_twice_in_the_survey_is_one_row_with_the_fuller_name():
    rows = tpc._one_row_per_school([CARROLL_SHORT, CARROLL_FULL])
    assert rows == [CARROLL_FULL]


def test_survey_name_that_says_more_is_shown():
    e = tpc._survey_entity(CASKEY, {"name": "Torah Academy of Greater Philadelphia"})
    assert e["name"] == "Caskey Torah Academy of Greater Philadelphia" and e["category"] == "Grades PK-8"
    assert e["research_name"] == "Torah Academy of Greater Philadelphia"
    e = tpc._survey_entity(CARROLL_FULL, {"name": "Archbishop Carroll High School"})
    assert e["name"] == "Archbishop John Carroll High School" and e["city"] == "Radnor"
    # A town in the survey's name isn't "more" - the research's name stays.
    assert tpc._survey_entity(SACRED_HEART_BRYN_MAWR, {"name": "Sacred Heart Academy"})["name"] == "Sacred Heart Academy"


def test_one_campus_matched_to_the_whole_school_shows_the_whole_school():
    e = tpc._survey_entity(FRIENDS_CENTRAL, {"name": "Friends' Central School Lower School"})
    assert e["name"] == "Friends' Central School" and e["category"] == "Grades PK-12"


def test_a_description_the_survey_contradicts_is_dropped():
    # Rittenhouse: St. Peter's School is nonsectarian (survey typology 7) - not "Catholic".
    e = tpc._survey_entity(ST_PETERS, {"name": "St Peter's School", "description": "Catholic elementary school near Rittenhouse Square"})
    assert "description" not in e
    e = tpc._survey_entity(SACRED_HEART_BRYN_MAWR, {"name": "Sacred Heart Academy", "description": "Catholic girls' school"})
    assert e["description"] == "Catholic girls' school"


@pytest.mark.parametrize("text, span", [
    ("Pre-K - 12", (-1, 12)), ("PK, K-8", (-1, 8)), ("NS-K", (-1, 0)), ("6 - 12", (6, 12)), ("PreK-8", (-1, 8)),
    ("K-12", (0, 12)), ("Ages 2-5", None), (None, None),
])
def test_research_grades_parse(text, span):
    assert tpc._research_grades(text) == span


def _run_check(entities, survey, monkeypatch):
    async def no_google(*a, **k):
        raise httpx.HTTPError("no Google in tests")
    monkeypatch.setattr(tpc, "_lookup", no_google)
    monkeypatch.setattr(tpc, "_survey_schools_near", lambda lat, lng, miles: survey)
    area = SimpleNamespace(centroid_lat=40.0, centroid_lng=-75.3, name="Villanova", state="PA", slug="villanova-pa")
    removed = []
    keep = asyncio.run(tpc._check_private_schools(None, None, area, {"entities": entities}, removed, "now"))
    return keep, removed


def test_recheck_drops_a_stale_survey_label_and_a_preschool_by_its_own_grades(monkeypatch):
    entities = [
        # Easton: an earlier wrong match left "Grades PK-8" on the high school.
        {"name": "Saints Peter and Paul High School", "category": "Grades PK-8", "survey_matched": True, "google_checked": True,
         "lat": 38.746, "lng": -76.069, "grades_served": "9-12"},
        # Villanova: "Main Line Montessori School" - its own grades are NS-K.
        {"name": "Main Line Montessori School", "category": "Grades K-11", "survey_matched": True, "lat": 40.01441, "lng": -75.34587,
         "grades_served": "NS-K"},
    ]
    keep, removed = _run_check(entities, [SPP_ELEMENTARY, CLASSICAL], monkeypatch)
    hs = next(e for e in keep if e["name"] == "Saints Peter and Paul High School")
    assert hs.get("category") is None and not hs.get("survey_matched") and hs["lat"] == 38.746
    assert [r["name"] for r in removed] == ["Main Line Montessori School"]


def test_recheck_keeps_one_entry_per_survey_school(monkeypatch):
    keep, removed = _run_check([{"name": "Friends' Central School"}, {"name": "Friends' Central School Lower School"}], [FRIENDS_CENTRAL], monkeypatch)
    assert [e["name"] for e in keep] == ["Friends' Central School"]
    assert removed and "listed twice" in removed[0]["removed_reason"]


# ---------------------------------------------------------------- the map dots at page time

def test_private_dots_follow_the_checked_list():
    block = {
        "entities": [{"name": "Caskey Torah Academy of Greater Philadelphia", "research_name": "Torah Academy of Greater Philadelphia",
                      "category": "Grades PK-8", "lat": 39.99999, "lng": -75.28629},
                     {"name": "Fusion Academy", "category": "Grades 6-12", "lat": 40.00813, "lng": -75.28905}],
        "_removed": [{"name": "St. Denis School", "removed_reason": "closed (Google)"},
                     {"name": "Miller School of Albemarle", "removed_reason": "too far to call nearby (16.4 mi by car)"}],
    }
    dots = [
        {"id": "A0701979", "name": "Phebe Anna Thorne Kindergarten", "lat": 40.0084, "lng": -75.3048},
        {"id": "g:torah", "name": "Torah Academy of Greater Philadelphia", "lat": 39.99999, "lng": -75.28629, "grades": "Pre-K - 12"},
        {"id": "g:stdenisschool", "name": "St. Denis School", "lat": 39.99, "lng": -75.30, "grades": "Pre-K - 8"},
        {"id": "A1903731", "name": "Fusion Academy - Ardmore", "lat": 40.00813, "lng": -75.28905},
        {"id": "pss:x", "name": "Main Line Montessori School", "lat": 40.0, "lng": -75.3, "grades": "NS-K"},
        {"id": "pss:y", "name": "Miller School of Albemarle", "lat": 38.0, "lng": -78.6, "grades": "5-12"},
        {"id": "01907061", "name": "Friends School Haverford", "lat": 40.01314, "lng": -75.30898},
    ]
    out = {d["name"]: d.get("grades") for d in sd.private_dots(dots, block)}
    assert out == {"Caskey Torah Academy of Greater Philadelphia": "PK-8", "Fusion Academy": "6-12",
                   "Miller School of Albemarle": "5-12", "Friends School Haverford": None}


def test_a_listed_school_the_map_missed_gets_its_dot():
    # Haverford: the map build took "The Haverford School" for "Friends School Haverford" (one shared word).
    block = {"entities": [{"name": "The Haverford School", "category": "Grades PK-12", "lat": 40.01512, "lng": -75.30549}]}
    dots = [{"id": "pss:f", "name": "Friends School Haverford", "lat": 40.01314, "lng": -75.30898, "grades": "PK-5"}]
    out = sd.private_dots(dots, block, center=(40.0090, -75.3048))
    assert [(d["name"], d["grades"]) for d in out] == [("Friends School Haverford", "PK-5"), ("The Haverford School", "PK-12")]


def test_map_keeps_the_fuller_name_of_a_school_listed_twice_on_one_spot():
    from app.services.areas import boundary_service as bs
    out = bs.dedupe_schools([{"name": "Archbishop Carroll High School", "lat": 40.04638, "lng": -75.35381},
                             {"name": "Archbishop John Carroll High School", "lat": 40.04638, "lng": -75.35381}])
    assert [s["name"] for s in out] == ["Archbishop John Carroll High School"]


def test_public_dots_show_no_grade_span_and_take_new_names():
    facts = {
        "public_schools": {"elementary_schools": [{"name": "Sunrise Elementary (formerly Burnley-Moran Elementary)"}],
                           "middle_schools": [{"name": "Charlottesville Middle School (formerly Buford Middle)"}]},
        "_school_map_fixes": {"drop": ["Walker Upper Elementary"]},
        "boundaries": {"schools": {"public": [
            {"id": "1", "name": "Burnley-Moran Elementary", "level": "Elementary", "grades": "Pre-K-4"},
            {"id": "2", "name": "Buford Middle", "level": "Middle", "grades": "7-8"},
            {"id": "3", "name": "Walker Upper Elementary", "level": "Middle", "grades": "5-6"},
            {"id": "4", "name": "Central Bucks HS-West", "level": "High", "grades": "10-12"},
        ], "private": []}},
    }
    pub = sd.tidy_school_facts(facts)["boundaries"]["schools"]["public"]
    assert [(d["name"], d.get("grades")) for d in pub] == [
        ("Sunrise Elementary", None), ("Charlottesville Middle School", None), ("Central Bucks HS-West", None)]
    assert facts["boundaries"]["schools"]["public"][0]["grades"] == "Pre-K-4"  # the stored record is untouched


# ---------------------------------------------------------------- charters are never "assigned" schools

def test_a_charter_named_without_the_word_is_found():
    # Rittenhouse: "Freire Middle School" is Freire CS (5-12), an application-based charter.
    assert ca.is_charter_school("Freire Middle School", "PA", 39.9495, -75.1719, "middle_schools")
    assert ca.is_charter_school("Something Charter School", "PA", None, None)


@pytest.mark.parametrize("name, state, lat, lng, level, local", [
    ("Princeton Middle School", "NJ", 40.3504, -74.6571, "middle_schools", {"princeton"}),   # Princeton Charter School is K-8
    ("Benjamin Franklin High School", "PA", 39.9495, -75.1719, "high_schools", set()),
    ("Freire Middle School", "DE", 39.9495, -75.1719, "middle_schools", set()),               # another state's Freire
])
def test_a_district_school_is_not_called_a_charter(name, state, lat, lng, level, local):
    assert not ca.is_charter_school(name, state, lat, lng, level, frozenset(local))


def test_charters_come_off_the_lists_and_the_note():
    area = SimpleNamespace(name="Rittenhouse Square", state="PA", county="Philadelphia", centroid_lat=39.9495, centroid_lng=-75.1719, facts={})
    ps = {"districts": [{"name": "The School District of Philadelphia"}],
          "middle_schools": [{"name": "Amy At Martin Middle School"}, {"name": "Freire Middle School"}],
          "boundary_caveat": "Within Rittenhouse Square, assignment to middle school may vary by exact address. "
                             "Some addresses are assigned to Amy At Martin Middle School while others feed into Freire Middle School."}
    out = ca._without_charters(ps, area)
    assert [x["name"] for x in out["middle_schools"]] == ["Amy At Martin Middle School"]
    assert "Freire" not in out["boundary_caveat"]
