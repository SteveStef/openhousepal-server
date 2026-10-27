"""Philadelphia neighborhood name -> map piece matching (10/27: "Logan Square" landed on "Logan")."""
import pytest

from app.services.areas import boundary_sources as bs


def _feat(name, listname, mapname, x):
    ring = [[x, 0], [x + 1, 0], [x + 1, 1], [x, 1], [x, 0]]
    return {"properties": {"NAME": name, "LISTNAME": listname, "MAPNAME": mapname},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


@pytest.fixture
def features(monkeypatch):
    feats = [
        _feat("LOGAN", "Logan", "Logan", 0),
        _feat("LOGAN_SQUARE", "Logan Square", "Logan Square", 2),
        _feat("RITTENHOUSE", "Rittenhouse", "Rittenhouse", 4),
        _feat("WASHINGTON_SQUARE", "Washington Square West", "Washington Square West", 6),
    ]
    monkeypatch.setattr(bs, "_PHILLY_FEATURE_CACHE", feats)
    return feats


@pytest.mark.asyncio
@pytest.mark.parametrize("typed,piece", [
    ("Logan Square", "Logan Square"),
    ("Logan", "Logan"),
    ("Rittenhouse Square", "Rittenhouse"),
    ("Rittenhouse", "Rittenhouse"),
    ("Washington Square West", "Washington Square West"),
])
async def test_names_land_on_the_right_piece(features, typed, piece):
    b = await bs.resolve_named_area_boundary(typed, "Philadelphia", "PA")
    assert b and b["matched_name"] == piece


@pytest.mark.asyncio
async def test_outside_philadelphia_never_matches(features):
    assert await bs.resolve_named_area_boundary("Logan Square", "Pittsburgh", "PA") is None


def test_merge_pieces_drops_the_shared_seam():
    left = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    right = {"type": "MultiPolygon", "coordinates": [[[[1, 0], [2, 0], [2, 1], [1, 1], [1, 0]]]]}
    merged = bs.merge_pieces([left, right])
    assert merged["type"] == "Polygon" and len(merged["coordinates"]) == 1
    assert abs(abs(bs._ring_area(merged["coordinates"][0])) - 2) < 1e-9


def test_merge_pieces_keeps_pieces_that_dont_line_up():
    a = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    b = {"type": "Polygon", "coordinates": [[[0.5, 0], [1.5, 0], [1.5, 1], [0.5, 1], [0.5, 0]]]}
    assert bs.merge_pieces([a, b])["type"] == "MultiPolygon"


@pytest.mark.asyncio
async def test_split_neighborhood_combines_its_pieces(monkeypatch):
    feats = [_feat("RICHMOND", "Richmond", "Richmond", 0), _feat("PORT_RICHMOND", "Port Richmond", "Port Richmond", 1)]
    monkeypatch.setattr(bs, "_PHILLY_FEATURE_CACHE", feats)
    b = await bs.resolve_named_area_boundary("Port Richmond", "Philadelphia", "PA")
    assert b["matched_name"] == "Port Richmond" and b["map_pieces"] == ["Port Richmond", "Richmond"]
    assert b["geometry"]["type"] == "Polygon" and b["bounds"]["max_lng"] == 2
    assert b["source"] == "opendataphilly_philadelphia_neighborhoods"
