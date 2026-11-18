"""Check-ins (owner, 11/13): only leads with a real reason, the reasons strongest first in plain words, and the
best 3 as "Recommended" - then "Worth a check-in", then "Less likely to respond"."""
from datetime import datetime, timedelta, timezone

from app.services import checkins_service as cs

NOW = datetime(2026, 11, 13, 17, 0, tzinfo=timezone.utc)   # a Friday afternoon (Eastern)


def days_ago(n, hour=15):
    return (NOW - timedelta(days=n)).replace(hour=hour)


def sig(**kw):
    base = dict(lead_id="L1", name="Jane Smith", first="Jane", represented="no", has_phone=True, has_email=True,
                created_at=days_ago(19))
    base.update(kw)
    return cs.Signals(**base)


def note(text="Wants a first-floor primary, moving in spring", at=19, street="417 Dogwood Ln", own=False, oh=True):
    return {"text": text, "at": days_ago(at), "open_house": oh, "street": street if oh else "", "own_listing": own}


def test_no_reason_no_checkin():
    # a guest who just signed in, never opened anything, no note: nothing to say - no blanket check-in
    assert cs.evaluate(sig(visits=[{"street": "417 Dogwood Ln", "at": days_ago(19), "own_listing": False}]), NOW) is None


def test_too_new_or_contacted_this_week():
    assert cs.evaluate(sig(created_at=days_ago(5), notes=[note(at=5)]), NOW) is None
    assert cs.evaluate(sig(notes=[note()], last_touch=days_ago(3), ever_contacted=True), NOW) is None
    assert cs.evaluate(sig(notes=[note()], has_phone=False, has_email=False), NOW) is None


def test_has_agent_only_with_note_or_own_listing():
    opened = dict(emails_opened=2, open_total=3, last_open=days_ago(2))
    assert cs.evaluate(sig(represented="yes", **opened), NOW) is None
    r = cs.evaluate(sig(represented="yes", notes=[note(own=True)],
                        visits=[{"street": "417 Dogwood Ln", "at": days_ago(19), "own_listing": True}]), NOW)
    assert r is not None
    # the owner's case: says the note, at the listing - not "has an agent - your listing" as the reason
    assert r["headline"].startswith("You left a note on Jane at your listing, 417 Dogwood Ln: “Wants a first-floor")
    assert "Has an agent - your listing" in r["tags"]
    assert r["gap"] == "You haven't reached out yet (2 weeks since they came in)"
    assert r["origin"].startswith("Added to your leads on Oct 25")   # no origin event given here


def test_reasons_lead_with_the_strongest():
    r = cs.evaluate(sig(notes=[note(at=10)], emails_opened=3, open_total=5, last_open=days_ago(1),
                        liked=["795 Mancill Rd"], last_like=days_ago(2), viewed=["22 Birch Ln", "9 Oak St"],
                        last_view=days_ago(2), last_touch=days_ago(15), ever_contacted=True), NOW)
    assert r["headline"] == "Opened your emails 5 times since you last reached out - last yesterday"
    assert r["reasons"][0].startswith("You left a note on Jane at your 417 Dogwood Ln open house")
    assert any(x.startswith("Liked 795 Mancill Rd in their Showcase on Wednesday") for x in r["reasons"])
    assert r["tags"][0] == "No agent"
    assert r["gap"] == "Last heard from you on Oct 29 (2 weeks ago)"
    assert r["simple"] is False


def test_click_is_not_said_twice():
    r = cs.evaluate(sig(emails_opened=1, open_total=1, last_open=days_ago(3), clicks=1, last_click=days_ago(3)), NOW)
    assert r["headline"] == "Clicked a link in your email on Tuesday"
    assert not any("Opened" in x for x in r["reasons"])
    assert r["simple"] is True   # nothing personal to work in - a one-tap check-in is fine


def test_long_note_is_trimmed_on_a_word():
    s = cs.note_snippet("Relocating from Ohio for a new job at the hospital, needs good schools and a big yard for the dog")
    assert s.endswith("…") and len(s) <= 71 and " " in s and not s[:-1].endswith(" ")


def test_old_note_alone_is_not_enough_but_with_activity_it_is():
    assert cs.evaluate(sig(created_at=days_ago(300), notes=[note(at=250)]), NOW) is None
    r = cs.evaluate(sig(created_at=days_ago(300), notes=[note(at=250)], emails_opened=1, open_total=1, last_open=days_ago(4)), NOW)
    assert r is not None and r["headline"].startswith("Opened your email")


def test_groups_top_three_then_worth_then_less():
    hot = lambda i: {**cs.evaluate(sig(lead_id=f"H{i}", notes=[note(at=12)], emails_opened=3, open_total=4, last_open=days_ago(1)), NOW), "lead_id": f"H{i}"}
    warm = {**cs.evaluate(sig(lead_id="W", emails_opened=1, open_total=1, last_open=days_ago(20)), NOW), "lead_id": "W"}
    cold = {**cs.evaluate(sig(lead_id="C", represented="yes", notes=[note(own=True, at=100)],
                              visits=[{"street": "417 Dogwood Ln", "at": days_ago(100), "own_listing": True}],
                              created_at=days_ago(100)), NOW), "lead_id": "C"}
    out = cs.group_items([cold, warm, hot(1), hot(2), hot(3), hot(4)])
    assert [x["group"] for x in out[:3]] == ["recommended"] * 3
    assert out[3]["group"] == "worth"            # the 4th hot one - only 3 recommended
    assert out[-1]["lead_id"] == "C" and out[-1]["group"] == "less"
    assert out[0]["score"] >= out[-1]["score"]


def test_weak_list_has_no_recommended():
    only = {**cs.evaluate(sig(represented="unknown", viewed=["9 Oak St"], last_view=days_ago(30), has_phone=False,
                              last_touch=days_ago(200), ever_contacted=True, created_at=days_ago(250)), NOW), "lead_id": "X"}
    assert cs.group_items([only])[0]["group"] != "recommended"


def test_skip_and_unskip():
    rules = cs.with_skip({"open_house_signin": {"enabled": True}, cs.SKIP_KEY: {"old": (NOW - timedelta(days=1)).isoformat()}}, "L1", NOW)
    assert rules["open_house_signin"] == {"enabled": True}
    assert set(rules[cs.SKIP_KEY]) == {"L1"}          # the expired one is dropped
    owner = type("O", (), {"followup_rules": rules})()
    assert cs.skipped_until(owner, "L1", NOW) is not None
    assert cs.skipped_until(owner, "L1", NOW + timedelta(days=31)) is None
    assert "L1" not in cs.with_skip(rules, "L1", NOW, days=0)[cs.SKIP_KEY]


def test_quick_message():
    owner = type("O", (), {"first_name": "Sam"})()
    assert cs.quick_message("Jane", owner, True).endswith("{showcase}")
    assert cs.quick_message("Jane", owner, False).startswith("Hi Jane, it's Sam!")


def test_never_contacted_doesnt_say_since_you_last_reached_out():
    r = cs.evaluate(sig(emails_opened=2, open_total=3, last_open=days_ago(2)), NOW)
    assert r["headline"] == "Opened your emails 3 times - last on Wednesday"


def test_note_buying_signals_count():
    # 11/16: the agent's own note saying they're pre-approved / moving soon used to count for nothing
    pre = cs.evaluate(sig(notes=[note("4-5 beds TE schools, 3-5k sqft, Pre-approved with Chase")]), NOW)
    plain = cs.evaluate(sig(notes=[note("4-5 beds TE schools, 3-5k sqft")]), NOW)
    assert "Pre-approved" in pre["tags"] and pre["score"] > plain["score"]
    soon = cs.evaluate(sig(notes=[note("Relocating from Ohio, lease is up in March")]), NOW)
    assert "Ready soon (your note)" in soon["tags"]
    assert "Pre-approved" not in plain["tags"] and not any("Ready" in t for t in plain["tags"])


def test_price_drop_email_is_named():
    r = cs.evaluate(sig(emails_opened=2, open_total=3, last_open=days_ago(1), ever_contacted=True, last_touch=days_ago(9),
                        hot_email="2439 Horseshoe Trl dropped to $1,195,000", hot_email_at=days_ago(1)), NOW)
    everything = [r["headline"], *r["reasons"]]
    assert any("“2439 Horseshoe Trl dropped to $1,195,000”" in x for x in everything)
    assert cs.HOT_SUBJECT.search("25 Spring Valley Rd dropped to $1,090,000")
    assert not cs.HOT_SUBJECT.search("Peter updated your Showcase search")


def test_display_name_and_street_tidy():
    assert cs.nice_name("peter judd") == "Peter Judd" and cs.nice_name("PETER JUDD") == "Peter Judd"
    assert cs.nice_name("Céline Guemnie Tafo") == "Céline Guemnie Tafo" and cs.nice_name("McKenzie O'Neil") == "McKenzie O'Neil"
    assert cs._street("1330 N Avignon Dr #, Gladwyne, PA") == "1330 N Avignon Dr"
    from app.services.tasks_service import _guest_first
    assert _guest_first("peter judd") == "Peter" and _guest_first("JOHN DOE") == "John" and _guest_first("DeShawn Lee") == "DeShawn"


def test_every_checkin_says_how_they_came_in():
    # owner 11/18: always from the lead's own first event - never the AI's words
    at = days_ago(33)
    o = lambda t, **kw: cs.origin_line({"type": t, "at": at, **kw}, days_ago(40), NOW)
    assert o("open_house_visitor", street="1330 N Avignon Dr") == "Signed in at your 1330 N Avignon Dr open house on Oct 11"
    assert o("form_submission", form="I'm Buying") == "Filled out your I'm Buying form on Oct 11"
    assert o("form_submission", form="What's My Home Worth?") == "Filled out your “What's My Home Worth?” form on Oct 11"
    assert o("property_website_inquiry", street="847 Lesley Rd") == "Asked about 847 Lesley Rd on your Spotlight on Oct 11"
    assert o("property_page_tour_request", street="26 Old Gulph Rd", site=True) == "Asked for a tour of 26 Old Gulph Rd on your website on Oct 11"
    assert o("showcase_view") == "Came in through their Showcase on Oct 11"
    assert o("portal_lead", title="Zillow", street="26 Old Gulph Rd") == "Came in as a Zillow lead about 26 Old Gulph Rd on Oct 11"
    assert cs.origin_line(None, days_ago(40), NOW) == "Added to your leads on Oct 4"
    assert cs.date_phrase(datetime(2025, 9, 2, 16, tzinfo=timezone.utc), NOW) == "on Sep 2, 2025"
    r = cs.evaluate(sig(notes=[note()], origin={"type": "form_submission", "at": days_ago(19), "form": "I'm Buying"}), NOW)
    assert r["origin"] == "Filled out your I'm Buying form on Oct 25"
