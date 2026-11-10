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
    assert "It already has 4 homes that might interest you, and it updates" in text  # only 2 have the must-have
    assert "2 of them have a first-floor primary bedroom." in text
    # the newest home (1 day) has no first-floor primary - the named one is a must-have home
    # "2 of them have X. The one I'd look at first is Y." - no ", one of those" (owner's wording, 11/09)
    assert "The one I'd look at first is 2 Oak St in Malvern (4 bd / 3 ba, $650,000). " in text and "one of those" not in text
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
    assert "It already has 2 homes that might interest you, and it updates the moment something new hits the market. One of them has" in tailored
    plain = ts.pitch_for("open_house_signin", None, {"parts": parts})
    # must-haves known: "homes that might interest you", never "similar to the one you toured" (owner, 11/09)
    assert "similar" not in plain and "It already has 2 homes that might interest you. It updates the moment something new "         "hits the market. One of them has a first-floor primary bedroom. The one I'd look at first is 2 Oak St" in plain


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
    ([_smart_home("1", 2, ["Cul-de-sac"])], PH, "That home is on a cul-de-sac."),
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
    assert "one of those" not in msg and "hit the market" not in msg and "came on the market" not in msg, msg
    assert "2 of them are on a cul-de-sac. The one I'd look at first is 3 Oak St" in msg, msg
    assert "1 Oak St" not in msg      # the newest home overall has no cul-de-sac
    # no must-haves at all: the old rule stands - nothing older than 3 weeks is named
    plain = _sc([_smart_home("1", 60, None), _smart_home("2", 45, None)], PH)
    assert "look at first" not in ts.full_message(_task("buyer_form"), plain, None, lead_name="Nicole Williams")


def test_fresh_must_have_home_keeps_its_day_count():
    homes = [_smart_home("1", 1, None), _smart_home("2", 5, ["Cul-de-sac"])]
    msg = ts.full_message(_task("open_house_signin"), _sc(homes, PH), None, lead_name="Nicole Williams")
    assert "The one I'd look at first is 2 Oak St in Malvern (4 bd / 3 ba, $650,000), the one that has it. It just hit the market 5 days ago." in msg, msg


# ---- Nicole's text, end to end (owner, 11/09): when, must-haves wording, the home named first ----
from datetime import date


def _bright(key, street, city, dom, beds, baths, price, smart=False):
    return {"id": f"p{key}", "ListingKey": key, "FullStreetAddress": street, "City": city, "MlsStatus": "ACTIVE-BRIGHT",
            "DaysOnMarket": dom, "BedroomsTotal": beds, "BathroomsTotal": baths, "ListPrice": price,
            "smartMatch": {"wants": ["First-floor primary bedroom"]} if smart else None}


NICOLE_HOMES = [
    _bright("1", "2439 HORSESHOE TRL", "CHESTER SPRINGS", 53, 4, 3.5, 1249000),
    _bright("2", "1430 GROVE AVE #1RD", "WEST CHESTER", 54, 3, 2.5, 1437900),
    _bright("3", "103 LEXINGTON MANOR", "GLENMOORE", 75, 3, 3.5, 1149990, smart=True),
    _bright("4", "3996 POWELL RD", "CHESTER SPRINGS", 25, 5, 5, 1150000),
    _bright("5", "25 SPRING VALLEY RD", "MALVERN", 5, 4, 4.5, 1190000, smart=True),
    _bright("6", "29 MCKNIGHT FARM LN", "GLENMOORE", 159, 5, 5.5, 1299000),
]
NICOLE_PAYLOAD = {"matchedProperties": NICOLE_HOMES, "preferences": {"show_match_score": True},
                  "originalProperty": {"listingKey": "805444215838", "address": "239 WINDHAM DR"}}
NICOLE_TARGET = (
    "Hi Nicole, thanks for stopping by my open house at 239 Windham Dr, Exton last weekend! If you have any questions "
    "about the home or would like to see it a second time, I'd be happy to set up a tour for you.\n\n"
    "I also set up a personal home search for you, called a Showcase. It already has 6 homes that might interest you. "
    "It updates the moment something new hits the market. 2 of them have a first-floor primary bedroom. The one I'd "
    "look at first is 25 Spring Valley Rd in Malvern (4 bd / 4.5 ba, $1.19M). It just hit the market 5 days ago. "
    "Your link is at the bottom of this text.\n\n"
    "Inside it, you can like, comment on or request a tour of any home - or just text me here.\n\n"
    "Thanks,\nPeter Stefanatos | COMPASS\n\n"
    "https://www.mainlinerealty.co/showcase/nicole-jbz4")


def _nicole_showcase(url="https://www.mainlinerealty.co/showcase/nicole-jbz4"):
    import app.main  # noqa: F401 - loaded as it runs: the match-score code's imports only resolve inside the app
    count, best, _m, days = ts._pick_standout(NICOLE_PAYLOAD)
    sm = ts.smart_summary(NICOLE_PAYLOAD)
    sm["phrases"] = FFP
    return {"url": url, "count": count, "parts": ts._pitch_parts(count, best, days, url, sm), "first_name": "Nicole",
            "candidates": ts._standout_candidates(NICOLE_PAYLOAD), "smart": sm, "tailored": False}


def test_nicole_text_reads_like_the_owners():
    from unittest.mock import patch
    owner = SimpleNamespace(first_name="Peter", last_name="Stefanatos", brokerage="COMPASS", followup_rules={})
    made = datetime(2026, 11, 7, 18, 7, tzinfo=timezone.utc)          # her Saturday sign-in
    msg = ts.with_when_slot(ts.render_rule_message(owner, "open_house_signin", name="Nicole Smith",
                                                   address="239 Windham Dr, Exton"), date(2026, 11, 7))
    task = SimpleNamespace(suggested_message=msg, auto_source="open_house_signin", kind="call", completed_at=None,
                           created_at=made, notes=None)
    monday = datetime(2026, 11, 9, 15, tzinfo=timezone.utc)
    real_fill, real_pick = ts.fill_when, ts.pick_for_followup
    with patch.object(ts, "fill_when", lambda t, n=None: real_fill(t, monday)), \
            patch.object(ts, "pick_for_followup", lambda c, cr, n=None: real_pick(c, cr, monday)):
        out = ts.full_message(task, _nicole_showcase(), owner, lead_name="Nicole Smith")
    assert out == NICOLE_TARGET, out


def test_preview_leads_with_the_homes_the_text_names():
    import app.main  # noqa: F401
    # the named home (25 Spring Valley Rd), then the other must-have home - not the newest-added three
    assert ts.preview_lead_ids(NICOLE_PAYLOAD) == ["p5", "p3"]
    # a home they passed on, or one off the market, never leads
    gone = {**NICOLE_PAYLOAD, "matchedProperties": [{**h, "disliked": h["id"] == "p5"} for h in NICOLE_HOMES]}
    assert ts.preview_lead_ids(gone) == ["p3"]
    # no must-haves: just the home the text names (newest fresh best match), if any
    plain = {**NICOLE_PAYLOAD, "matchedProperties": [{**h, "smartMatch": None} for h in NICOLE_HOMES]}
    lead = ts.preview_lead_ids(plain)
    count, best, _m, _d = ts._pick_standout(plain)
    assert lead == [best["id"]]


def test_agents_own_pitch_loses_similar_when_must_haves_known():
    owner = SimpleNamespace(first_name="Peter", last_name="S", followup_rules={"open_house_signin": {
        "pitch": "Here are {home_count} similar to the one you toured! {showcase_link}"}})
    out = ts.pitch_for("open_house_signin", owner, _nicole_showcase())
    assert out.startswith("Here are 6 homes that might interest you! 2 of them have a first-floor primary bedroom."), out
    # no must-haves: their words stay
    sc = _sc([_home("1", 2), _home("2", 3)], FFP)
    assert "similar to the one you toured" in ts.pitch_for("open_house_signin", owner, sc)


# ---- 11/09 review of every must-have text ----
def test_must_haves_known_but_no_home_has_them_yet():
    """Every listing read, none has the must-have: still "might interest you", and it says so plainly."""
    homes = [_home("1", 2), _home("2", 3)]
    payload = {"matchedProperties": homes, "preferences": {}}
    count, best, _m, days = ts._pick_standout(payload)
    parts = ts._pitch_parts(count, best, days, "https://x.test/s", {"n": 0, "wants": ["First-floor primary bedroom"], "phrases": FFP})
    for rule in ("open_house_signin", "buyer_form", "showcase_setup"):
        out = ts.pitch_for(rule, None, {"parts": parts})
        _clean(out)
        assert "similar to the one you toured" not in out and "matching what" not in out, out
        assert "None of them has a first-floor primary bedroom yet, but you'll hear the moment one does." in out, out


def test_all_have_it_keeps_matching():
    text = _pitch([_home("1", 2, smart=True), _home("2", 3, smart=True)])
    assert "It already has 2 homes matching what you're looking for" in text


def test_only_one_home():
    text = _pitch([_home("1", 4, smart=True)], rule="open_house_signin")
    assert "1 home that might interest you" in text and "That home has a first-floor primary bedroom. It's 1 Oak St" in text, text
    assert "The one I'd look at first" not in text
    plain = _pitch([_home("1", 4)])
    assert "That home is 1 Oak St" in plain and "The one I'd look at first" not in plain, plain
    payload = {"matchedProperties": [_home("1", 4)], "preferences": {}}
    count, best, _m, days = ts._pick_standout(payload)
    tailored = ts.pitch_for("open_house_signin", None, {"parts": ts._pitch_parts(count, best, days, "https://x.test/s"), "tailored": True})
    assert "1 home that fits," in tailored, tailored


def test_showcase_request_must_line_before_email():
    text = _pitch([_home("1", 2), _home("2", 3, smart=True), _home("3", 4, smart=True)], rule="showcase_setup")
    assert ("Your Showcase is ready - it already has 3 homes that might interest you. 2 of them have a first-floor primary "
            "bedroom. I also sent it to your email.") in text, text
