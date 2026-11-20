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


def _homes():
    ev, n = [], 0
    for side, lng0 in (("EAST", -75.69), ("WEST", -75.79)):
        for i in range(30):
            n += 1
            lat, lng = 40.01 + (i % 10) * 0.008, lng0 + (i // 10) * 0.02
            spell = f"DOWNINGTOWN HIGH SCHOOL {side} CAMPUS" if i % 2 else f"DHS {side}"
            ev.append(NS(id=f"e{n}", latitude=lat, longitude=lng, high_school=spell))
            if i % 3 == 0:
                n += 1
                ev.append(NS(id=f"e{n}", latitude=lat + 0.001, longitude=lng + 0.001, high_school="DOWNINGTOWN"))
    return ev


def test_district_name_is_not_a_school():
    ev = _homes()
    targets = [NS(id="t-east", latitude=40.03, longitude=-75.67, high_school="DOWNINGTOWN"),
               NS(id="t-west", latitude=40.03, longitude=-75.77, high_school="Downingtown"),
               NS(id="t-named", latitude=40.03, longitude=-75.67, high_school="DHS East"),
               NS(id="t-blank", latitude=40.05, longitude=-75.75, high_school=None)]
    stats, rows = hs._compute("PA", "DOWNINGTOWN AREA", ev, targets, MAP, False)
    got = {r["p"]: r for r in rows}
    assert "DOWNINGTOWN" in stats["vague"]
    assert got["t-east"]["k"] == "nces:E" and got["t-east"]["src"] in ("map", "nearby")
    assert got["t-west"]["k"] == "nces:W" and got["t-west"]["src"] != "bright"
    assert got["t-named"]["k"] == "nces:E" and got["t-named"]["src"] == "bright"     # both spellings -> one school
    assert got["t-blank"]["k"] == "nces:W"
    assert {r["k"] for r in rows} == {"nces:E", "nces:W"}                            # never a "Downingtown" school, never STEM
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
