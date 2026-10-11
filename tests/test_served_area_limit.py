import pytest
from fastapi import HTTPException, status

from app.api.site_routes import (
    MAX_SERVED_AREAS_PER_AGENT,
    SERVED_AREA_LIMIT_MESSAGE,
    _validate_served_area_slot,
)


def test_served_area_limit_allows_fourteen_to_fifteen():
    _validate_served_area_slot(MAX_SERVED_AREAS_PER_AGENT - 1, None)


def test_served_area_limit_rejects_sixteenth_selection():
    with pytest.raises(HTTPException) as exc:
        _validate_served_area_slot(MAX_SERVED_AREAS_PER_AGENT, None)

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
    assert exc.value.detail == SERVED_AREA_LIMIT_MESSAGE


def test_served_area_limit_allows_add_after_removal_at_limit():
    existing_count_after_removal = MAX_SERVED_AREAS_PER_AGENT - 1

    _validate_served_area_slot(existing_count_after_removal, None)


def test_duplicate_canonical_area_does_not_consume_another_slot():
    with pytest.raises(HTTPException) as exc:
        _validate_served_area_slot(MAX_SERVED_AREAS_PER_AGENT, object())

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert exc.value.detail == "You already serve this area."


def test_mixed_city_and_neighborhood_selections_share_one_cap():
    mixed_city_and_neighborhood_count = MAX_SERVED_AREAS_PER_AGENT

    with pytest.raises(HTTPException) as exc:
        _validate_served_area_slot(mixed_city_and_neighborhood_count, None)

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST
