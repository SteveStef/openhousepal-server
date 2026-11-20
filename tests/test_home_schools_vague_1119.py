"""A listing whose "high school" is only the district's name ("DOWNINGTOWN" in a district with an East and a West
school - 10 of 34 live Downingtown listings in Lisa's range, 11/19) says nothing about the school: those homes are
placed like blank ones, never a third "Downingtown" school. A district-wide academy (STEM) never wins either."""
from types import SimpleNamespace as NS

from app.services import home_school_service as hs
from app.services.nces_school_assignment_service import wgs84_to_web_mercator


def _square(sid, name, lat0, lng0, lat1, lng1):
    (x0, y0), (x1, y1) = wgs84_to_web_mercator(lat0, lng0), wgs84_to_web_mercator(lat1, lng1)
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]
    return (sid, name, min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), [ring])


# West half: lng -75.80 .. -75.70, East half: -75.70 .. -75.60; the STEM academy covers both
MAP = hs._MapSchools([
    _square("W", "Downingtown HS West Campus", 40.0, -75.80, 40.10, -75.70),
    _square("E", "Downingtown HS East Campus", 40.0, -75.70, 40.10, -75.60),
    _square("S", "Downingtown STEM Academy", 39.9, -75.90, 40.20, -75.50),
])


TWP = {"EAST": "UWCHLAN", "WEST": "WEST BRADFORD"}


def _homes():
    ev, n = [], 0
    for side, lng0 in (("EAST", -75.69), ("WEST", -75.79)):
        for i in range(30):
            n += 1
            lat, lng = 40.01 + (i % 10) * 0.002, lng0 + (i // 10) * 0.005
            spell = f"DOWNINGTOWN HIGH SCHOOL {side} CAMPUS" if i % 2 else f"DHS {side}"
            ev.append(NS(id=f"e{n}", latitude=lat, longitude=lng, high_school=spell, township=TWP[side]))
            if i % 3 == 0:
                n += 1
                ev.append(NS(id=f"e{n}", latitude=lat + 0.001, longitude=lng + 0.001, high_school="DOWNINGTOWN", township=TWP[side]))
    return ev


def test_district_name_is_not_a_school():
    ev = _homes()
    targets = [NS(id="t-east", latitude=40.02, longitude=-75.685, high_school="DOWNINGTOWN", township="UWCHLAN"),
               NS(id="t-west", latitude=40.02, longitude=-75.785, high_school="Downingtown", township="West Bradford"),
               NS(id="t-named", latitude=40.02, longitude=-75.685, high_school="DHS East", township="UWCHLAN"),
               NS(id="t-far", latitude=40.05, longitude=-75.75, high_school=None, township="WEST BRADFORD"),
               NS(id="t-no-twp", latitude=40.02, longitude=-75.685, high_school=None)]
    stats, rows = hs._compute("PA", "DOWNINGTOWN AREA", ev, targets, MAP, False)
    got = {r["p"]: r for r in rows}
    assert "DOWNINGTOWN" in stats["vague"]
    # township + map + every nearby listing agree -> placed
    assert got["t-east"]["k"] == "nces:E" and got["t-east"]["src"] == "agreed", (got, stats)
    assert got["t-west"]["k"] == "nces:W" and got["t-west"]["src"] == "agreed"
    assert got["t-named"]["k"] == "nces:E" and got["t-named"]["src"] == "bright"     # both spellings -> one school
    # map alone (no nearby listings), or no township to check against -> unknown, never a guess
    assert got["t-far"]["k"] is None and got["t-no-twp"]["k"] is None
    assert {r["k"] for r in rows} - {None} == {"nces:E", "nces:W"}                   # never a "Downingtown" school, never STEM
    assert got["t-east"]["n"].startswith("Downingtown High School East")


def test_single_school_district_keeps_its_name():
    """A district with ONE high school named like the district ("RADNOR") is a real answer, not vague."""
    ev = [NS(id=f"r{i}", latitude=40.03 + i * 0.002, longitude=-75.35, high_school="RADNOR") for i in range(12)]
    stats, rows = hs._compute("PA", "RADNOR TOWNSHIP", ev, [NS(id="t", latitude=40.035, longitude=-75.35, high_school="Radnor")],
                              hs._MapSchools([]), False)
    assert not stats["vague"] and rows[0]["src"] == "bright" and rows[0]["k"] == "txt:PA:RADNOR"


def test_school_search_needs_a_district():
    assert hs.search_filter(NS(high_schools=["nces:E"], school_districts=[])) is None
    assert hs.search_filter(NS(high_schools=[], school_districts=["Downingtown Area, PA"])) is None
    assert hs.search_filter(NS(high_schools=["nces:E"], school_districts=["Downingtown Area, PA"])) is not None


def test_live_cases_spellings():
    """From live data 11/19: a single-school district spelled short merges ("READING" = "READING SENIOR"); a typo merges
    ("PENNSBURYE" -> "PENNSBURY"); a real school name stays the listing's answer even when the old map disagrees."""
    ev = [NS(id=f"r{i}", latitude=40.30 + i * 0.001, longitude=-75.90, high_school="READING SENIOR") for i in range(20)] + \
         [NS(id=f"s{i}", latitude=40.30 + i * 0.001, longitude=-75.901, high_school="Reading") for i in range(9)]
    stats, rows = hs._compute("PA", "READING", ev, [NS(id="t", latitude=40.305, longitude=-75.90, high_school="READING")], hs._MapSchools([]), False)
    assert rows[0]["k"] == "txt:PA:READING SENIOR" and rows[0]["src"] == "bright", (rows, stats)
    ev = [NS(id=f"p{i}", latitude=40.20 + i * 0.001, longitude=-74.80, high_school="PENNSBURY") for i in range(30)] + \
         [NS(id=f"q{i}", latitude=40.20 + i * 0.001, longitude=-74.801, high_school="PENNSBURYE") for i in range(10)]
    stats, rows = hs._compute("PA", "PENNSBURY", ev, [NS(id="t", latitude=40.21, longitude=-74.80, high_school="Pennsburye")], hs._MapSchools([]), False)
    assert rows[0]["k"] == "txt:PA:PENNSBURY", (rows, stats)
    # a real school ("RIVER HILL") with homes split 73/20 on an old map stays what the listing says
    ev = _homes()
    for i in range(12):
        ev.append(NS(id=f"rh{i}", latitude=40.01 + i * 0.006, longitude=-75.69 - (0.05 if i < 3 else 0), high_school="EASTERN VIEW"))
    stats, rows = hs._compute("PA", "DOWNINGTOWN AREA", ev, [NS(id="t", latitude=40.03, longitude=-75.75, high_school="Eastern View")], MAP, False)
    assert "EASTERN VIEW" not in stats["vague"] and rows[0]["src"] == "bright", (rows, stats)


def test_signals_that_disagree_leave_the_school_unknown():
    """826 Williamsburg Blvd (live, 11/19): in Uwchlan (almost all East) and inside the 2015 map's East lines, yet it goes
    to WEST - the listings just across the line say West. Owner: a wrong school is worse than "unknown"."""
    ev = [NS(id=f"u{i}", latitude=40.02 + i * 0.004, longitude=-75.65, high_school="DOWNINGTOWN HS EAST CAMPUS", township="UWCHLAN") for i in range(14)] + \
         [NS(id=f"w{i}", latitude=40.050 + i * 0.0004, longitude=-75.7005, high_school="DOWNINGTOWN HS WEST CAMPUS", township="DOWNINGTOWN") for i in range(10)]
    target = NS(id="t", latitude=40.050, longitude=-75.6998, high_school="DOWNINGTOWN", township="UWCHLAN")
    for m in (hs._MapSchools([]), MAP):          # no map, or a map that sides with the township
        stats, rows = hs._compute("PA", "DOWNINGTOWN AREA", ev, [target], m, False)
        assert rows[0]["k"] is None, (rows, stats)
    # every signal agrees -> placed
    inside = NS(id="t2", latitude=40.04, longitude=-75.65, high_school=None, township="UWCHLAN")
    stats, rows = hs._compute("PA", "DOWNINGTOWN AREA", ev, [inside], MAP, False)
    assert rows[0]["k"] == "nces:E" and rows[0]["src"] == "agreed", (rows, stats)
    # a township short of 98% agreement -> unknown, even with the map and the neighbours agreeing
    ev2 = ev + [NS(id="x", latitude=40.20, longitude=-75.66, high_school="DOWNINGTOWN HS WEST CAMPUS", township="UWCHLAN")]
    stats, rows = hs._compute("PA", "DOWNINGTOWN AREA", ev2, [inside], MAP, False)
    assert rows[0]["k"] is None, (rows, stats)
