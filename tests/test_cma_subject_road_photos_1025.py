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
    async def lookup(db, lat, lng, race=False):
        return None if lat == 1 else {"verdict": "through", "summary": "x"}

    async def by_name(db, lat, lng, address):
        return {"verdict": "through", "source": "openstreetmap-road-name", "address": address}

    monkeypatch.setattr(rc, "get_road_context", lookup)
    monkeypatch.setattr(rc, "road_context_from_address", by_name)
    out = asyncio.run(rc.get_road_contexts(None, [(1, 1), (2, 2)], ["602 Media Line Rd", "538 S Media Line Rd"]))
    assert out[0]["source"] == "openstreetmap-road-name" and out[0]["address"] == "602 Media Line Rd"
    assert out[1] == {"verdict": "through", "summary": "x"}


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
