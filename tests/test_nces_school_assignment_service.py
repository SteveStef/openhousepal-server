from types import SimpleNamespace

import pytest

from app.services import nces_school_assignment_service as svc


def boundary(
    name: str,
    *,
    level: str,
    low: str,
    high: str,
    school_id: str = "1",
    geometry=None,
):
    return SimpleNamespace(
        school_id=school_id,
        school_name=name,
        lea_id="lea",
        state="PA",
        grade_low=low,
        grade_high=high,
        level=level,
        source_name=None,
        open_enroll="0",
        multi_boundary="0",
        geometry=geometry or [],
    )


def prop(**overrides):
    values = {
        "elementary_school": None,
        "middle_or_junior_school": None,
        "high_school": None,
        "latitude": 40.0,
        "longitude": -75.0,
        "state": "PA",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_bright_mls_full_data_wins_over_nces(monkeypatch):
    async def fake_matches(_db, _property):
        return {
            "elementary": [boundary("NCES Elem", level="1", low="KG", high="05")],
            "middle": [boundary("NCES Middle", level="2", low="06", high="08")],
            "high": [boundary("NCES High", level="3", low="09", high="12")],
        }

    monkeypatch.setattr(svc, "_nces_matches_for_property", fake_matches)
    result = await svc.resolve_school_assignment(
        None,
        prop(elementary_school="Ithan Elementary", middle_or_junior_school="Welsh Valley", high_school="Harriton"),
    )

    assert result.final == {"elementary": "Ithan Elementary", "middle": "Welsh Valley", "high": "Harriton"}
    assert result.sources == {"elementary": "bright_mls", "middle": "bright_mls", "high": "bright_mls"}


@pytest.mark.asyncio
async def test_partial_bright_mls_data_fills_missing_levels_from_nces(monkeypatch):
    async def fake_matches(_db, _property):
        return {
            "elementary": [boundary("NCES Elem", level="1", low="KG", high="05")],
            "middle": [boundary("NCES Middle", level="2", low="06", high="08")],
            "high": [boundary("NCES High", level="3", low="09", high="12")],
        }

    monkeypatch.setattr(svc, "_nces_matches_for_property", fake_matches)
    result = await svc.resolve_school_assignment(None, prop(elementary_school="Ithan Elementary"))

    assert result.final["elementary"] == "Ithan Elementary"
    assert result.sources["elementary"] == "bright_mls"
    assert result.final["middle"] == "NCES Middle"
    assert result.sources["middle"] == "nces"
    assert result.final["high"] == "NCES High"
    assert result.sources["high"] == "nces"


@pytest.mark.asyncio
async def test_no_bright_mls_data_uses_nces_matches(monkeypatch):
    async def fake_matches(_db, _property):
        return {
            "elementary": [boundary("K-8 School", level="1", low="KG", high="08")],
            "middle": [boundary("K-8 School", level="1", low="KG", high="08")],
            "high": [boundary("High School", level="3", low="09", high="12")],
        }

    monkeypatch.setattr(svc, "_nces_matches_for_property", fake_matches)
    result = await svc.resolve_school_assignment(None, prop())

    assert result.final["elementary"] == "K-8 School"
    assert result.final["middle"] == "K-8 School"
    assert result.final["high"] == "High School"


@pytest.mark.asyncio
async def test_no_reliable_match_leaves_level_unavailable(monkeypatch):
    async def fake_matches(_db, _property):
        return {"elementary": [], "middle": [], "high": []}

    monkeypatch.setattr(svc, "_nces_matches_for_property", fake_matches)
    result = await svc.resolve_school_assignment(None, prop())

    assert result.final == {"elementary": None, "middle": None, "high": None}
    assert result.sources == {"elementary": None, "middle": None, "high": None}


@pytest.mark.asyncio
async def test_ambiguous_equal_nces_matches_are_not_used(monkeypatch):
    async def fake_matches(_db, _property):
        return {
            "elementary": [
                boundary("North Elem", level="1", low="KG", high="05", school_id="1"),
                boundary("South Elem", level="1", low="KG", high="05", school_id="2"),
            ],
            "middle": [],
            "high": [],
        }

    monkeypatch.setattr(svc, "_nces_matches_for_property", fake_matches)
    result = await svc.resolve_school_assignment(None, prop())

    assert result.final["elementary"] is None
    assert result.sources["elementary"] == "ambiguous_nces"


def test_grade_ranges_cover_unusual_schools():
    assert svc.grade_range_targets("KG", "08") == {"elementary", "middle"}
    assert svc.grade_range_targets("06", "12") == {"middle", "high"}
    assert svc.grade_range_targets("KG", "12") == {"elementary", "middle", "high"}
    assert svc.grade_range_targets("05", "06") == {"elementary", "middle"}


def test_point_in_polygon_includes_boundary_edge():
    square = [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]]
    assert svc.point_in_polygon_geometry(5, 5, square)
    assert svc.point_in_polygon_geometry(10, 5, square)
    assert not svc.point_in_polygon_geometry(11, 5, square)


@pytest.mark.asyncio
async def test_missing_coordinates_returns_no_nces_matches():
    matches = await svc._nces_matches_for_property(None, prop(latitude=None, longitude=None))
    assert matches == {"elementary": [], "middle": [], "high": []}
