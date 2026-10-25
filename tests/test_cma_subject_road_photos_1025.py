"""10/25 - 602 Media Line Rd (manual seller CMA): the subject's road lookup got no answer, so the report
called a comp on the SAME road busier than the subject; and with no description or inside photos the
report called the home original and took a condition deduction."""
import asyncio

from app.services import cma_ai_service as svc
from app.services import road_context_service as rc


def test_road_name_key_matches_address_and_map_name():
    assert rc.road_name_key("602 Media Line Rd") == "media line road"
    assert rc.road_name_key("538 S MEDIA LINE RD") == "media line road"
    assert rc.road_name_key("Media Line Road") == "media line road"
    # direction words drop on both sides ("N Lemon St" / OSM "North Lemon Street") - same key either way
    assert rc.road_name_key("12 West Chester Pike") == rc.road_name_key("West Chester Pike") == "chester pike"
    assert rc.road_name_key("2701 CORNELL CT #2701") == "cornell court"
    assert rc.road_name_key("") is None


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _stmt):
        return _Rows(self.rows)


def test_address_fallback_says_major_road_only():
    major = [{"name": "Media Line Road", "kinds": ["residential", "secondary"], "max_speed_mph": 35}]
    out = asyncio.run(rc.road_context_from_address(_FakeDb(major), 39.9721, -75.3928, "602 Media Line Rd"))
    assert out["verdict"] == "through" and "Media Line Road" in out["summary"]
    quiet = [{"name": "Elliott Road", "kinds": ["residential"], "max_speed_mph": None}]
    assert asyncio.run(rc.road_context_from_address(_FakeDb(quiet), 39.97, -75.38, "7 Elliott Rd")) is None


def test_failed_lookup_falls_back_to_road_name(monkeypatch):
    calls = []

    async def no_cache(db, key):
        calls.append(("read", key))
        return None

    async def fetch(lat, lng, race=False):
        return None if lat == 1 else [{"tags": {"highway": "residential", "name": "Elliott Road"}, "geometry": [{"lat": 2, "lon": 2}]}]

    async def save(db, key, payload):
        calls.append(("write", key))

    async def remember(db, lat, lng, el):
        calls.append(("remember", lat))

    async def by_name(db, lat, lng, address):
        return {"verdict": "through", "source": "openstreetmap-road-name", "address": address}

    monkeypatch.setattr(rc, "_read_cache", no_cache)
    monkeypatch.setattr(rc, "_fetch_overpass", fetch)
    monkeypatch.setattr(rc, "_write_cache", save)
    monkeypatch.setattr(rc, "_remember_road_names", remember)
    monkeypatch.setattr(rc, "road_context_from_address", by_name)
    out = asyncio.run(rc.get_road_contexts(None, [(1, 1), (2, 2)], ["602 Media Line Rd", "7 Elliott Rd"]))
    assert out[0]["source"] == "openstreetmap-road-name" and out[0]["address"] == "602 Media Line Rd"
    assert out[1]["source"] == "openstreetmap"
    # reads first, then saves - one database call at a time
    assert [c[0] for c in calls] == ["read", "read", "write", "remember"]


def test_interior_documented():
    manual = {"source": "manual", "photos": ["street.jpg"]}
    assert svc._subject_interior_documented(manual) is False
    assert svc._subject_interior_documented({**manual, "condition_photos": ["kitchen.jpg"]}) is True
    assert svc._subject_interior_documented({**manual, "public_remarks": "New primary suite addition in 2019, renovated kitchen and baths."}) is True
    assert svc._subject_interior_documented({"source": "mls", "photos": ["a", "b", "c"]}) is True


def test_runtime_facts_keep_road_context():
    s = {"road_context": {"verdict": "through"}, "road_context_note": "fronts Media Line Road", "beds": 5}
    assert svc._subject_runtime_facts(s) == {"road_context": {"verdict": "through"}, "road_context_note": "fronts Media Line Road"}


def test_fingerprint_changes_with_condition_photos():
    base = {"street_address": "602 Media Line Rd"}
    a = svc._compute_fingerprint(base, [], 8, False)
    assert a == svc._compute_fingerprint(dict(base), [], 8, False)
    assert a != svc._compute_fingerprint({**base, "condition_photos": ["k.jpg"]}, [], 8, False)


# ---- grader 10/25 on the 602 Media Line Rd report -----------------------------------------------
from app.services import cma_fact_check as fc

MANUAL = {"source": "manual", "lot_size": 20909}


def test_manual_subject_no_mls_record_and_unknown_not_none():
    t = ("**Systems and Age:** The MLS record shows no documented updates to the heating, cooling, water heater, "
         "or electrical systems. Since the home was built in 1970, buyers will ask.")
    out, flags = fc.fix_manual_subject_record(t, MANUAL)
    assert "MLS record" not in out and "don't say when the heating, cooling, water heater, or electrical systems were last updated" in out
    assert flags
    # an MLS subject, or a comp's own note, is left alone
    assert fc.fix_manual_subject_record(t, {"source": "mls"})[0] == t
    assert fc.fix_manual_subject_record(t, MANUAL, comp={"street_address": "x"})[0] == t


def test_subject_lot_words_checked_against_comps():
    comps = [{"lot_size": v} for v in (20473, 15000, 18000, 12000, 21000)]
    t = "Presenting the home's strengths - smaller lot, ample above-grade square footage - will help."
    out, flags = fc.fix_subject_lot_words(t, MANUAL, comps)
    assert "smaller lot" not in out and "strengths - larger lot, ample" in out and flags
    # about a comp, or true: unchanged
    t2 = "Comp #3's smaller lot holds it back."
    assert fc.fix_subject_lot_words(t2, MANUAL, comps)[0] == t2
    assert fc.fix_subject_lot_words(t, {"lot_size": 9000}, comps)[0] == t


def test_above_grade_psf_not_backed_out():
    t = ("it remains a useful size and age reference point once its ~734 sqft of finished lower level is backed out "
         "of its blended $315/sqft above grade This is a fully renovated home")
    out, _ = fc.fix_above_grade_psf_wording(t)
    assert "backed out" not in out and "blended" not in out and "at its $315 per above-grade sq ft" in out


def test_market_demand_is_not_a_judgment_reason():
    text, _sup, all_gridded = svc._bridge_driver_text(
        [{"factor": "Relevant nearby and same-school-district sales"}, {"factor": "Competitive sale outcomes"}], set())
    assert text == "relevant nearby and same-school-district sales" and not all_gridded
    _t, _s, all_g = svc._bridge_driver_text([{"factor": "Competitive sale outcomes"}], set())
    assert all_g


def test_manual_subject_prompt_notes():
    assert "no MLS record" in svc._MANUAL_RECORD_NOTE and "typical condition" in svc._INTERIOR_UNKNOWN_NOTE
    assert "record_source" in svc._SUBJECT_FIELDS and "interior_condition" in svc._SUBJECT_FIELDS


def test_road_lookup_has_a_time_limit(monkeypatch):
    async def slow(lat, lng, race=False):
        await asyncio.sleep(5)
        return []

    async def no_cache(db, key):
        return None

    async def no_name(db, lat, lng, address):
        return None

    monkeypatch.setattr(rc, "_fetch_overpass", slow)
    monkeypatch.setattr(rc, "_read_cache", no_cache)
    monkeypatch.setattr(rc, "road_context_from_address", no_name)
    monkeypatch.setattr(rc, "_SUBJECT_BUDGET_S", 0.2)
    monkeypatch.setattr(rc, "_COMPS_BUDGET_S", 0.3)
    import time
    t = time.time()
    out = asyncio.run(rc.get_road_contexts(None, [(40.0, -75.0), (40.1, -75.1), (40.2, -75.2), (40.3, -75.3)]))
    assert out == [None, None, None, None] and time.time() - t < 2
