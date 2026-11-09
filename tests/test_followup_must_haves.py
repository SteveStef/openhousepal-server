"""Follow-up texts mention the Showcase's must-haves (owner, 11/09): "4 of them have the first-floor primary
bedroom you wanted." right after the home count, and the home named first is one that has them."""
from datetime import datetime, timedelta, timezone

from app.services import tasks_service as ts


def _home(key, dom, smart=False, street=None):
    return {"ListingKey": key, "DaysOnMarket": dom, "MlsStatus": "Active", "FullStreetAddress": street or f"{key} Oak St",
            "City": "Malvern", "BedroomsTotal": 4, "BathroomsTotal": 3, "ListPrice": 650000,
            "smartMatch": {"wants": ["First-floor primary bedroom"]} if smart else None}


def _pitch(homes, rule="buyer_form"):
    payload = {"matchedProperties": homes, "preferences": {}}
    count, best, _m, days = ts._pick_standout(payload)
    smart = ts.smart_summary(payload)
    parts = ts._pitch_parts(count, best, days, "https://x.test/showcase/abc", smart)
    return ts.pitch_for(rule, None, {"parts": parts, "first_name": "Nicole"})


def test_some_have_it():
    text = _pitch([_home("1", 2), _home("2", 3, smart=True), _home("3", 4, smart=True), _home("4", 1)])
    assert "It already has 4 homes matching what you're looking for, and it updates" in text
    assert "2 of them have the first-floor primary bedroom you wanted." in text
    # the newest home (1 day) has no first-floor primary - the named one is a must-have home
    assert "The one I'd look at first is 2 Oak St in Malvern (4 bd / 3 ba, $650,000), one of those." in text
    assert text.index("2 of them have") < text.index("The one I'd look at first")


def test_one_has_it():
    text = _pitch([_home("1", 2), _home("2", 3, smart=True)])
    assert "One of them has the first-floor primary bedroom you wanted." in text
    assert "2 Oak St in Malvern (4 bd / 3 ba, $650,000), the one that has it." in text


def test_all_have_it():
    text = _pitch([_home("1", 2, smart=True), _home("2", 3, smart=True)])
    assert "All 2 have the first-floor primary bedroom you wanted." in text
    assert "one of those" not in text


def test_no_must_haves_unchanged():
    homes = [_home("1", 2), _home("2", 3)]
    text = _pitch(homes)
    assert "you wanted" not in text
    payload = {"matchedProperties": homes, "preferences": {}}
    count, best, _m, days = ts._pick_standout(payload)
    old = ts.render_pitch(ts.DEFAULT_PITCHES["buyer_form"], ts._pitch_parts(count, best, days, "https://x.test/showcase/abc"))
    assert text == old


def test_tailored_open_house_and_signin():
    homes = [_home("1", 2), _home("2", 3, smart=True)]
    payload = {"matchedProperties": homes, "preferences": {}}
    count, best, _m, days = ts._pick_standout(payload)
    parts = ts._pitch_parts(count, best, days, "https://x.test/s", ts.smart_summary(payload))
    tailored = ts.pitch_for("open_house_signin", None, {"parts": parts, "tailored": True})
    assert "It already has 2 homes that fit, and it updates the moment something new hits the market. One of them has" in tailored
    plain = ts.pitch_for("open_house_signin", None, {"parts": parts})
    assert "similar to the one you toured" in plain and "One of them has the first-floor primary bedroom you wanted." in plain


def test_overdue_followup_names_a_must_have_home():
    cands = ts._standout_candidates({"matchedProperties": [_home("1", 1), _home("2", 5, smart=True)]})
    made = datetime.now(timezone.utc) - timedelta(days=3)
    home, _days = ts.pick_for_followup(cands, made)
    assert home["ListingKey"] == "2"


def test_agent_version_without_count_left_alone():
    parts = ts._pitch_parts(3, None, None, "https://x.test/s", {"n": 2, "wants": ["Pool"]})
    out = ts.render_pitch(ts._with_must("Take a look: {showcase_link}", parts), parts)
    assert out == "Take a look: https://x.test/s"
