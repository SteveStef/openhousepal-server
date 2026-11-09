"""Follow-up texts mention the Showcase's must-haves (owner, 11/09): "4 of them have a first-floor primary
bedroom." right after the home count (tidied words), and the home named first is one that has them. Without
tidied words it reads "match what you asked for: ..." - right whatever was typed ("col de sack")."""
from datetime import datetime, timedelta, timezone

from app.services import tasks_service as ts


def _home(key, dom, smart=False, street=None):
    return {"ListingKey": key, "DaysOnMarket": dom, "MlsStatus": "Active", "FullStreetAddress": street or f"{key} Oak St",
            "City": "Malvern", "BedroomsTotal": 4, "BathroomsTotal": 3, "ListPrice": 650000,
            "smartMatch": {"wants": ["First-floor primary bedroom"]} if smart else None}


FFP = {"First-floor primary bedroom": {"many": "have a first-floor primary bedroom", "one": "has a first-floor primary bedroom", "it": "have a first-floor primary bedroom"}}


def _pitch(homes, rule="buyer_form", phrases=FFP):
    payload = {"matchedProperties": homes, "preferences": {}}
    count, best, _m, days = ts._pick_standout(payload)
    smart = ts.smart_summary(payload)
    if smart:
        smart["phrases"] = phrases
    parts = ts._pitch_parts(count, best, days, "https://x.test/showcase/abc", smart)
    return ts.pitch_for(rule, None, {"parts": parts, "first_name": "Nicole"})


def test_some_have_it():
    text = _pitch([_home("1", 2), _home("2", 3, smart=True), _home("3", 4, smart=True), _home("4", 1)])
    assert "It already has 4 homes matching what you're looking for, and it updates" in text
    assert "2 of them have a first-floor primary bedroom." in text
    # the newest home (1 day) has no first-floor primary - the named one is a must-have home
    assert "The one I'd look at first is 2 Oak St in Malvern (4 bd / 3 ba, $650,000), one of those." in text
    assert text.index("2 of them have") < text.index("The one I'd look at first")


def test_one_has_it():
    text = _pitch([_home("1", 2), _home("2", 3, smart=True)])
    assert "One of them has a first-floor primary bedroom." in text
    assert "2 Oak St in Malvern (4 bd / 3 ba, $650,000), the one that has it." in text


def test_all_have_it():
    text = _pitch([_home("1", 2, smart=True), _home("2", 3, smart=True)])
    assert "All 2 have a first-floor primary bedroom." in text
    assert "one of those" not in text


def test_no_must_haves_unchanged():
    homes = [_home("1", 2), _home("2", 3)]
    text = _pitch(homes)
    assert "of them" not in text and "All " not in text
    payload = {"matchedProperties": homes, "preferences": {}}
    count, best, _m, days = ts._pick_standout(payload)
    old = ts.render_pitch(ts.DEFAULT_PITCHES["buyer_form"], ts._pitch_parts(count, best, days, "https://x.test/showcase/abc"))
    assert text == old


def test_tailored_open_house_and_signin():
    homes = [_home("1", 2), _home("2", 3, smart=True)]
    payload = {"matchedProperties": homes, "preferences": {}}
    count, best, _m, days = ts._pick_standout(payload)
    sm = ts.smart_summary(payload); sm["phrases"] = FFP
    parts = ts._pitch_parts(count, best, days, "https://x.test/s", sm)
    tailored = ts.pitch_for("open_house_signin", None, {"parts": parts, "tailored": True})
    assert "It already has 2 homes that fit, and it updates the moment something new hits the market. One of them has" in tailored
    plain = ts.pitch_for("open_house_signin", None, {"parts": parts})
    assert "similar to the one you toured" in plain and "One of them has a first-floor primary bedroom." in plain


def test_overdue_followup_names_a_must_have_home():
    cands = ts._standout_candidates({"matchedProperties": [_home("1", 1), _home("2", 5, smart=True)]})
    made = datetime.now(timezone.utc) - timedelta(days=3)
    home, _days = ts.pick_for_followup(cands, made)
    assert home["ListingKey"] == "2"


def test_agent_version_without_count_left_alone():
    parts = ts._pitch_parts(3, None, None, "https://x.test/s", {"n": 2, "wants": ["Pool"]})
    out = ts.render_pitch(ts._with_must("Take a look: {showcase_link}", parts), parts)
    assert out == "Take a look: https://x.test/s"


def test_untidied_words_still_read_right():
    """A must-have with no tidied words (older, or the tidy-up failed) - even a typo reads as a plain list."""
    text = _pitch([_home("1", 2), _home("2", 3, smart=True), _home("3", 4, smart=True)], phrases={})
    assert "2 of them match what you asked for: first-floor primary bedroom." in text


def test_several_must_haves_tidied():
    ph = {"Cul-de-sac": {"many": "are on a cul-de-sac", "one": "is on a cul-de-sac", "it": "be on a cul-de-sac"},
          "No HOA": {"many": "have no HOA", "one": "has no HOA", "it": "have no HOA"}}
    parts = ts._must_parts(6, None, {"n": 2, "wants": ["Cul-de-sac", "No HOA"], "phrases": ph})
    assert parts["must_line"] == "2 of them are on a cul-de-sac and have no HOA."
    ph["Pool"] = {"many": "have a pool", "one": "has a pool", "it": "have a pool"}
    assert ts._must_parts(6, None, {"n": 2, "wants": ["No HOA", "Pool"], "phrases": ph})["must_line"] == "2 of them have no HOA and a pool."
    parts = ts._must_parts(6, None, {"n": 1, "wants": ["Cul-de-sac"], "phrases": ph})
    assert parts["must_line"] == "One of them is on a cul-de-sac."


# ---- every follow-up that carries the Showcase message (owner, 11/09: "all follow ups that have to do with it") ----
import re
from types import SimpleNamespace

PH = {"Cul-de-sac": {"many": "are on a cul-de-sac", "one": "is on a cul-de-sac", "it": "be on a cul-de-sac"},
      "Pool": {"many": "have a pool", "one": "has a pool", "it": "have a pool"}}


def _smart_home(key, dom, wants):
    h = _home(key, dom)
    h["smartMatch"] = {"wants": wants} if wants else None
    return h


def _sc(homes, phrases, tailored=False):
    payload = {"matchedProperties": homes, "preferences": {}}
    count, best, _m, days = ts._pick_standout(payload)
    sm = ts.smart_summary(payload)
    if sm:
        sm["phrases"] = phrases
    return {"url": "https://x.test/showcase/abc", "count": count, "parts": ts._pitch_parts(count, best, days, "https://x.test/showcase/abc", sm),
            "first_name": "Nicole", "candidates": ts._standout_candidates(payload), "smart": sm, "tailored": tailored}


def _task(rule):
    return SimpleNamespace(suggested_message=f"Hi Nicole, thanks for coming!{ts.SHOWCASE_MARKER}", auto_source=rule, kind="text",
                           completed_at=None, created_at=datetime.now(timezone.utc), notes=None)


def _clean(text):
    assert "{" not in text and "}" not in text, text
    assert "  " not in text, text
    assert not re.search(r"\s[.,]", text), text
    assert ".." not in text, text


CASES = [  # (homes, phrases, the must-haves sentence expected - None = none at all)
    ([_smart_home("1", 2, ["Cul-de-sac"]), _smart_home("2", 3, None), _smart_home("3", 4, ["Cul-de-sac"])], PH, "2 of them are on a cul-de-sac."),
    ([_smart_home("1", 2, ["Cul-de-sac"]), _smart_home("2", 3, None)], PH, "One of them is on a cul-de-sac."),
    ([_smart_home("1", 2, ["Cul-de-sac", "Pool"]), _smart_home("2", 3, ["Cul-de-sac", "Pool"])], PH, "All 2 are on a cul-de-sac and have a pool."),
    ([_smart_home("1", 2, ["Cul-de-sac"])], PH, "It is on a cul-de-sac."),
    ([_smart_home("1", 2, ["col de sack"]), _smart_home("2", 3, None), _smart_home("3", 4, None)], {}, "One of them matches what you asked for: col de sack."),
    ([_smart_home("1", 2, None), _smart_home("2", 3, None)], PH, None),
]


def test_every_followup_situation_reads_right():
    for rule in list(ts.PITCH_RULES) + ["open_house_signin_tailored", "second_visit_request"]:
        for homes, phrases, want in CASES:
            tailored = rule == "open_house_signin_tailored"
            sc = _sc(homes, phrases, tailored=tailored)
            r = "open_house_signin" if tailored else rule
            msg = ts.full_message(_task(r), sc, None, lead_name="Nicole Williams")
            _clean(msg)
            if want is None:
                assert "of them" not in msg and "All " not in msg and "It is on" not in msg, (rule, msg)
                continue
            assert msg.count(want) == 1, (rule, msg)
            # right after the sentence with the home count, never inside it
            count_txt = sc["parts"]["home_count"]
            assert msg.index(count_txt) < msg.index(want), (rule, msg)
            assert re.search(r"[.!?] " + re.escape(want), msg), (rule, msg)
            # the email version carries it too
            assert want in ts.email_version(msg, None), rule


def test_agent_edited_pitch_keeps_their_words():
    owner = SimpleNamespace(first_name="Peter", last_name="S", followup_rules={"buyer_form": {
        "pitch": "Here are {home_count} I picked for you! {showcase_link}", "custom_pitch": True}})
    sc = _sc(CASES[0][0], PH)
    out = ts.pitch_for("buyer_form", owner, sc)
    assert out.startswith("Here are 3 homes I picked for you! 2 of them are on a cul-de-sac."), out
    owner.followup_rules["buyer_form"]["pitch"] = "My picks for you: {showcase_link}"   # no count -> no must-haves line
    assert "cul-de-sac" not in ts.pitch_for("buyer_form", owner, sc)


def test_no_homes_no_must_line():
    sc = _sc([], PH)
    msg = ts.full_message(_task("buyer_form"), sc, None, lead_name="Nicole Williams")
    assert "of them" not in msg and "Nothing matches just yet" in msg


def test_must_have_home_named_even_when_not_new():
    """Owner 11/09: the home named first has the must-have, not just the newest - even when every must-have
    home is older than 3 weeks (then no "hit the market" line)."""
    homes = [_smart_home("1", 2, None), _smart_home("2", 60, ["Cul-de-sac"]), _smart_home("3", 45, ["Cul-de-sac"])]
    sc = _sc(homes, PH)
    msg = ts.full_message(_task("buyer_form"), sc, None, lead_name="Nicole Williams")
    _clean(msg)
    assert "The one I'd look at first is 3 Oak St in Malvern" in msg, msg     # the newer of the two must-have homes
    assert "one of those" in msg and "hit the market" not in msg and "came on the market" not in msg, msg
    assert "1 Oak St" not in msg      # the newest home overall has no cul-de-sac
    # no must-haves at all: the old rule stands - nothing older than 3 weeks is named
    plain = _sc([_smart_home("1", 60, None), _smart_home("2", 45, None)], PH)
    assert "look at first" not in ts.full_message(_task("buyer_form"), plain, None, lead_name="Nicole Williams")


def test_fresh_must_have_home_keeps_its_day_count():
    homes = [_smart_home("1", 1, None), _smart_home("2", 5, ["Cul-de-sac"])]
    msg = ts.full_message(_task("open_house_signin"), _sc(homes, PH), None, lead_name="Nicole Williams")
    assert "The one I'd look at first is 2 Oak St in Malvern (4 bd / 3 ba, $650,000), the one that has it. It just hit the market 5 days ago." in msg, msg
