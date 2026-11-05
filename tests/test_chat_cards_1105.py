"""The chat cards' new choices (11/05): what a card sends is checked on the server, never trusted."""
from types import SimpleNamespace

import pytest

from app.services.ai import ai_actions_catalog as cat
from app.services.ai import ai_actions_media as media
from app.services.ai import ai_spotlight_setup as setup
from app.services.ai.ai_actions import ActionError

USER = SimpleNamespace(plan_tier="IDX", is_admin=False)


def test_spotlight_keeps_only_real_photos_and_sections():
    payload = {"listing_key": "k", "template_id": "cinematic_luxury", "preview": {"brand_color": "#C9A24D", "photos": ["a", "b"]}}
    out = cat._adjust_spotlight(USER, payload, {"hero_image": "http://elsewhere/x.jpg", "header_mode": "cycle", "header_motion": "sideways", "hidden_sections": ["payment", "nope"], "story_mode": "original", "publish": True})
    assert "hero_image" not in out["draft"]
    assert out["draft"]["header"] == {"mode": "cycle"}
    assert out["draft"]["section_visibility"] == {"payment": False}
    assert out["story_mode"] == "original" and out["publish"] is True


def test_spotlight_hiding_open_houses_sticks():
    payload = {"preview": {"photos": []}}
    out = cat._adjust_spotlight(USER, payload, {"hidden_sections": ["open_houses"]})
    assert out["draft"]["section_visibility"]["open_houses_by_agent"] is True


def test_spotlight_rejects_a_bad_tour_link():
    with pytest.raises(ActionError):
        cat._adjust_spotlight(USER, {"preview": {"photos": []}}, {"virtual_tour_url": "javascript:alert(1)"})


def _manual(**extra):
    return {"street_address": "1 Oak St", "city": "Wayne", "state": "PA", "latitude": 40.0, "longitude": -75.4, "property_type": "SINGLE_FAMILY", "beds": 3, "full_baths": 2, "half_baths": 1,
            "above_grade_finished_area": 2000, "year_built": 1990, **extra}


def test_cma_manual_needs_the_map_point():
    with pytest.raises(ActionError):
        cat._adjust_cma(USER, {"manual": True, "ai": False}, {"ai": False, "subject": {"street_address": "1 Oak St"}})


def test_ai_cma_on_a_manual_home_needs_its_facts():
    with pytest.raises(ActionError) as e:
        cat._adjust_cma(USER, {"manual": True, "ai": True}, {"ai": True, "subject": _manual(beds=None)})
    assert "bedrooms" in str(e.value)


def test_cma_manual_adds_up_baths_and_size():
    out = cat._adjust_cma(USER, {"manual": True, "ai": True}, {"ai": True, "subject": _manual(), "condition": "Updated", "theme": "dark", "hidden_sections": ["charts", "nope"]})
    assert out["subject"]["baths"] == 2.5 and out["subject"]["sqft"] == 2000 and out["subject"]["source"] == "manual"
    assert out["extra"] == {"condition": "Updated"} and out["theme"] == "dark" and out["hidden_sections"] == ["charts"]


def _invite(**body):
    return {"body": {"invite_type": "client_event", "event_date": "2026-11-13", "title": "Dinner", "address": "1 Oak St", **body},
            "options": {"homes": [], "presets": {"client_event": {"audience": "clients", "message": "Hi", "questions": [{"id": "party"}]}}}}


def test_a_chat_invite_always_has_its_rsvp_questions():
    out = cat._adjust_invite(USER, _invite(), {})
    assert out["body"]["questions"] == [{"id": "party"}] and out["body"]["message"] == "Hi" and out["body"]["audience"] == "clients"


def test_a_chat_invite_needs_a_place():
    with pytest.raises(ActionError):
        cat._adjust_invite(USER, _invite(address=""), {"address": "", "place_name": ""})


def test_a_brokers_open_is_for_agents():
    p = _invite(invite_type="brokers_open")
    p["options"]["presets"]["brokers_open"] = {"audience": "agents", "message": "", "questions": []}
    assert cat._adjust_invite(USER, p, {"audience": "clients"})["body"]["audience"] == "agents"


def test_discovery_takes_a_typed_address_as_the_center():
    p = {"centers": [{"label": "Wayne, PA", "lat": 40, "lng": -75, "kind": "town"}], "center": "Wayne, PA"}
    out = cat._adjust_discovery(USER, p, {"custom_center": {"label": "200 Eagle Rd", "lat": 40.04, "lng": -75.39}, "center": "200 Eagle Rd"})
    assert out["center"] == "200 Eagle Rd" and out["centers"][0]["lat"] == 40.04


def test_discovery_ignores_a_made_up_map_point():
    p = {"centers": [{"label": "Wayne, PA", "lat": 40, "lng": -75, "kind": "town"}], "center": "Wayne, PA"}
    out = cat._adjust_discovery(USER, p, {"custom_center": {"label": "Mars", "lat": 400, "lng": 0}, "center": "Mars"})
    assert out["center"] == "Wayne, PA"


def _media(**over):
    return {"tool": None, "listing": {"photos": ["L1"]}, "uploads": ["U1"], **over}


def test_media_only_uses_their_own_photos():
    with pytest.raises(ActionError):
        media._adjust_media(USER, _media(), {"tool": "twilight", "effects": ["twilight"], "photo": "http://someone-else/x.jpg"})


def test_staging_needs_a_room_and_a_style():
    with pytest.raises(ActionError):
        media._adjust_media(USER, _media(), {"tool": "virtual_staging", "effects": ["virtual_staging"], "photo": "U1", "style": "modern"})


def test_photo_source_is_told_apart():
    assert media._adjust_media(USER, _media(), {"tool": "twilight", "effects": ["twilight"], "photo": "L1"})["source_kind"] == "listing"
    assert media._adjust_media(USER, _media(), {"tool": "twilight", "effects": ["twilight"], "photo": "U1"})["source_kind"] == "upload"


def test_day_to_twilight_is_one_photo():
    with pytest.raises(ActionError):
        media._adjust_media(USER, _media(), {"tool": "video", "photos": ["L1", "U1"], "video_style": "day_to_twilight"})


def test_town_facts_are_kept_to_size():
    facts = setup._town_facts('here you go {"tagline": "' + "x" * 300 + '", "known_for": ["a", 5, ""], "downtown": {"name": "Main"}}')
    assert len(facts["tagline"]) == 140 and facts["known_for"] == ["a"]
    assert setup._town_facts("no json here") is None
