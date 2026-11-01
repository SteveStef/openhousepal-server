"""AI helper additions from the 11/01 capability review (ai_actions_more / ai_lookups_more) - the
card logic and lookup shaping, with no database or network: the pure helpers directly, and the
prepare steps with their reads swapped for fakes."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.main  # noqa: F401  (loads every router first - avoids a circular import)
from app.services.ai import ai_actions, ai_actions_more as more, ai_lookups, ai_lookups_more as lmore
from app.services.ai.ai_actions import ActionError

USER = SimpleNamespace(id="u1", plan_tier="IDX", is_admin=False, mls_id="M1")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- registered + gated like their pages

def test_everything_is_registered_with_the_right_plans():
    ai_actions._load()
    plans = {n: ai_actions.ACTIONS[n].plans for n in (
        "withdraw_hosting_request", "offer_other_host_time", "answer_team_host_ask", "add_guest_note", "share_guest_list_with_listing_agent",
        "publish_blog_post", "reply_to_help_ticket", "update_email_signature", "resync_calendar", "send_crm_test_lead", "set_setup_step")}
    assert plans["withdraw_hosting_request"] == {"BASIC", "PREMIUM", "IDX"}
    assert plans["answer_team_host_ask"] == {"BASIC", "PREMIUM", "IDX"}
    assert plans["publish_blog_post"] == {"PREMIUM", "IDX", "PRO_NO_MLS"}
    assert plans["update_email_signature"] == {"IDX", "PRO_NO_MLS"}
    assert plans["add_guest_note"] is None and plans["reply_to_help_ticket"] is None and plans["set_setup_step"] is None
    for name, (_, schema, p, _fn) in ((n, ai_lookups.LOOKUPS[n]) for n in (
            "find_homes_to_host", "get_blog_stats", "get_agent_hub_stats", "get_help_ticket", "get_email_signature", "get_calendar_status", "get_crm_status")):
        assert schema["type"] == "object"
    assert ai_lookups.LOOKUPS["find_homes_to_host"][2] == {"BASIC", "PREMIUM", "IDX"}
    assert ai_lookups.LOOKUPS["get_email_signature"][2] == {"IDX", "PRO_NO_MLS"}
    # a Basic agent never sees the signature or blog tools
    basic = SimpleNamespace(plan_tier="BASIC", is_admin=False)
    names = {d["name"] for d in ai_actions.action_tools_for(basic)} | {d["name"] for d in ai_lookups.definitions_for(basic)}
    assert "update_email_signature" not in names and "publish_blog_post" not in names and "get_blog_stats" not in names
    assert "withdraw_hosting_request" in names and "find_homes_to_host" in names


# ---------------------------------------------------------------- Discovery lookup

LISTINGS = [
    {"ListingKey": "A", "Street": "12 Oak St", "City": "Malvern", "Price": "$650,000", "Beds": 4, "Baths": 3, "ListAgentFullName": "Dana Lee",
     "ListOfficeName": "Fox", "AgentOnPlatform": True, "HostRequestEligible": True, "DistanceFromLandmark": 2.345, "New": True, "DaysOnMarket": 1},
    {"ListingKey": "B", "Street": "9 Elm Rd", "City": "Paoli", "Price": "$500,000", "AgentOnPlatform": False, "DistanceFromLandmark": 4.0},
    {"ListingKey": "C", "Street": "3 Main St", "City": "Malvern", "Price": "$800,000", "AgentOnPlatform": True, "HostRequestEligible": False,
     "HostingBlocked": "coming_soon"},
]


def test_discovery_keeps_one_town_and_says_if_the_day_is_open():
    statuses = {"A": {"scheduled_opens": [{"scheduled_date": "2026-11-08", "scheduled_start_time": "13:00:00", "scheduled_end_time": "15:00:00"}]}}
    mine = [{"id": "r1", "listing_key": "A", "requested_date": "2026-11-14", "status": "pending"}]
    out = lmore.discovery_rows(LISTINGS, statuses, mine, town="malvern", day="2026-11-08")
    assert [h["listing_key"] for h in out["homes"]] == ["A", "C"]
    a, c = out["homes"]
    assert a["miles_away"] == 2.3 and a["can_ask_in_app"] and a["listing_agent"] == "Dana Lee"
    assert a["open_houses_already_set"] == [{"date": "2026-11-08", "start": "13:00", "end": "15:00"}]
    assert a["on_that_day"] == "already has an open house"
    assert a["your_requests"] == [{"request_id": "r1", "date": "2026-11-14", "status": "pending"}]
    assert c["on_that_day"] == "can't host yet" and not c["can_ask_in_app"] and "Coming soon" in c["hosting_note"]
    assert lmore.discovery_rows(LISTINGS, {}, [], day="2026-11-09")["homes"][1]["on_that_day"] == "open to host"


def test_discovery_leaves_out_the_agents_own_listings():
    own = [{"ListingKey": "Y", "Street": "1 Mine Ct", "City": "Malvern", "OnPlatformAgentId": "u1"}] + LISTINGS
    assert "Y" not in [h["listing_key"] for h in lmore.discovery_rows(own, {}, [], me="u1")["homes"]]
    assert "Y" in [h["listing_key"] for h in lmore.discovery_rows(own, {}, [], me="someone-else")["homes"]]


def test_discovery_warns_when_already_hosting_that_day():
    mine = [{"id": "r9", "listing_key": "Z", "requested_date": "2026-11-08", "status": "approved", "property_address": "5 Pine Ln"}]
    out = lmore.discovery_rows(LISTINGS, {}, mine, day="2026-11-08")
    assert "5 Pine Ln" in out["you_already_host_that_day"]


def test_discovery_lookup_with_no_search_area(monkeypatch):
    async def fake_get(user, path, params=None):
        return True, {"success": True, "data": [], "preferences": None}
    monkeypatch.setattr(lmore, "_get", fake_get)
    out = run(lmore._homes_to_host(None, USER, {}))
    assert out["homes"] == [] and "Discovery" in out["note"]


def test_discovery_lookup_names_the_search_when_the_town_is_outside_it(monkeypatch):
    async def fake_get(user, path, params=None):
        if path == "/api/discovery":
            return True, {"data": LISTINGS, "preferences": {"landmark_address": "Wayne, PA", "miles": 5.0}}
        return True, ([] if path.endswith("my-requests") else {})
    monkeypatch.setattr(lmore, "_get", fake_get)
    out = run(lmore._homes_to_host(None, USER, {"town": "Doylestown"}))
    assert out["homes"] == [] and "5 miles around Wayne, PA" in out["note"]


# ---------------------------------------------------------------- stats / signature / calendar / CRM lookups

def test_hub_counts_by_window():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    ev = [{"type": "visit", "timestamp": (now - timedelta(days=1)).isoformat()}, {"type": "qr", "timestamp": (now - timedelta(days=20)).isoformat()},
          {"type": "form", "timestamp": (now - timedelta(days=50)).isoformat()}, {"type": "visit", "timestamp": "junk"}]
    out = lmore.hub_counts(ev, now)
    assert out["last_7_days"] == {"visits": 1, "qr_scans": 0, "form_responses": 0}
    assert out["last_30_days"] == {"visits": 1, "qr_scans": 1, "form_responses": 0}
    assert out["last_60_days"] == {"visits": 1, "qr_scans": 1, "form_responses": 1}


def test_signature_summary():
    cfg = {"ctaButtons": [{"label": "What's My Home Worth?", "formType": "home_value", "enabled": False}, {"label": "x", "url": "http://a"}],
           "blockVisibility": {"banner": True}, "banner": {"showStats": False}, "fields": {"instagram": {"include": True}, "phone": {"include": True}},
           "luxe": True, "luxeMetal": "silver"}
    out = lmore.signature_summary(cfg, {"installed": True, "installed_client": "gmail", "needs_recopy": True, "recopy_reason": "edits"})
    assert out["buttons"][0]["label"] == "My Agent Hub" and out["buttons"][1] == {"label": "What's My Home Worth?", "form": "home_value", "on": False}
    assert len(out["buttons"]) == 2          # a free-link button with no form isn't editable - left out like the page does
    assert out["listings_and_sales_banner"]["sales_stats"] is False and out["socials_shown"] == ["instagram"]
    assert out["look"]["style"] == "Luxe (silver)" and out["installed_in"] == "Gmail" and "copy it again" in out["why_recopy"]
    assert lmore.signature_summary({}, None)["installed"] == "unknown"


def test_calendar_and_crm_summaries_never_show_the_webhook_address():
    data = {"google_calendar": {"connected": True, "account_email": "a@b.com", "last_sync_at": "2026-11-01T10:00:00", "needs_reconnect": False},
            "google_calendar_available": True, "microsoft_calendar": None, "microsoft_calendar_available": False,
            "crm_fub_connected": True, "crm_webhook_url": "https://hooks.example.com/secret-123", "crm_parse_email": None,
            "crm_push_enabled": False, "crm_last_push_at": None, "crm_last_push_status": None, "inbound_address": "leads-abc@mg.openhousepal.com"}
    cal = lmore.calendar_summary(data)
    assert cal["google"]["connected"] and cal["google"]["account"] == "a@b.com" and not cal["microsoft"]["connected"] and cal["connected_any"]
    crm = lmore.crm_summary(data)
    assert crm["follow_up_boss_connected"] and crm["webhook_set"] and not crm["sending_new_leads_on"]
    assert "secret-123" not in str(crm)


# ---------------------------------------------------------------- blogs

def _blog(**kw):
    base = dict(id="b1", title="Main Line Prices", status="DRAFT", agent_hub_visible=True, ai_review=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_one(obj):
    async def one(db, stmt, missing):
        if obj is None:
            raise ActionError(missing)
        return obj
    return one


def test_blog_flags_lists_open_issues_numbers_and_geography():
    review = {"status": "done", "open_issues": [{"quote": "Median price rose 12%", "problem": "Source says 8%"}] * 6,
              "numbers_to_verify": ["$612,000", "12%"], "geography_flags_open": 1}
    flags = more.blog_flags(review)
    assert flags[0] == "“Median price rose 12%” - Source says 8%"
    assert "...and 1 more flag" in flags and "Numbers to double-check: $612,000, 12%" in flags
    assert any("school-district" in f for f in flags)
    assert more.blog_flags({"status": "done", "checked": False})[-1].startswith("The fact-check didn't finish")
    assert more.blog_flags(None) == []


def test_publish_card_warns_with_the_flags(monkeypatch):
    review = {"status": "done", "finished": True, "open_issues": [{"quote": "Q", "problem": "P"}]}
    monkeypatch.setattr(more, "_one", _fake_one(_blog(ai_review=review)))
    p = run(more._p_blog_publish(None, USER, {"blog_id": "b1", "when": "now"}))
    assert p.title.startswith("Publish") and p.payload["status"] == {"status": "PUBLISHED"} and p.confirm_label == "Publish"
    assert p.warning.startswith("Check these before it goes live") and "“Q” - P" in p.warning
    assert "Shows in your Agent Hub's Blogs section" in p.details


def test_publish_card_without_flags_but_unfinished(monkeypatch):
    monkeypatch.setattr(more, "_one", _fake_one(_blog(ai_review={"status": "done", "finished": False})))
    p = run(more._p_blog_publish(None, USER, {"blog_id": "b1", "when": "now"}))
    assert "Fact-check & finish" in p.warning


def test_schedule_card_and_its_checks(monkeypatch):
    monkeypatch.setattr(more, "_one", _fake_one(_blog()))
    future = (date.today() + timedelta(days=3)).isoformat()
    p = run(more._p_blog_publish(None, USER, {"blog_id": "b1", "when": "schedule", "date": future, "time": "10:30"}))
    assert p.payload["status"]["status"] == "SCHEDULED" and p.payload["status"]["scheduled_at"].startswith(future + "T10:30")
    assert p.warning is None
    with pytest.raises(ActionError, match="still ahead"):
        run(more._p_blog_publish(None, USER, {"blog_id": "b1", "when": "schedule", "date": "2020-01-01", "time": "10:00"}))
    monkeypatch.setattr(more, "_one", _fake_one(_blog(status="PUBLISHED")))
    with pytest.raises(ActionError, match="already live"):
        run(more._p_blog_publish(None, USER, {"blog_id": "b1", "when": "now"}))


def test_blog_hub_switch_and_unwritten_drafts(monkeypatch):
    monkeypatch.setattr(more, "_one", _fake_one(_blog(status="PUBLISHED", agent_hub_visible=True)))
    p = run(more._p_blog_publish(None, USER, {"blog_id": "b1", "show_on_agent_hub": False}))
    assert p.payload == {"blog_id": "b1", "hub": False} and p.title.startswith("Hide") and p.warning is None
    with pytest.raises(ActionError, match="already on"):
        run(more._p_blog_publish(None, USER, {"blog_id": "b1", "show_on_agent_hub": True}))
    monkeypatch.setattr(more, "_one", _fake_one(_blog(ai_review={"status": "running"})))
    with pytest.raises(ActionError, match="still working"):
        run(more._p_blog_publish(None, USER, {"blog_id": "b1", "when": "now"}))
    monkeypatch.setattr(more, "_one", _fake_one(_blog(ai_review={"status": "interview", "new": True})))
    with pytest.raises(ActionError, match="hasn't been written"):
        run(more._p_blog_publish(None, USER, {"blog_id": "b1", "when": "now"}))


# ---------------------------------------------------------------- signature switches

def test_signature_changes():
    cfg = {"ctaButtons": [{"id": "cta-home_value", "label": "What's My Home Worth?", "formType": "home_value", "enabled": True}],
           "blockVisibility": {"banner": True}, "banner": {}, "fields": {"instagram": {"include": False}}}
    new, lines = more.apply_signature_changes(cfg, {"banner": False, "banner_parts": {"recent_sales": False},
                                                    "buttons": {"home_value": False, "showing": True}, "socials": {"instagram": True}},
                                              my_form_types={"showing", "home_value"}, social_urls={"instagram"})
    assert new["blockVisibility"]["banner"] is False and new["banner"]["showRecentSales"] is False
    assert new["ctaButtons"][0]["enabled"] is False and new["ctaButtons"][1]["formType"] == "showing"
    assert new["fields"]["instagram"]["include"] is True
    assert cfg["blockVisibility"]["banner"] is True          # the original isn't touched
    assert "Add a “Schedule a Showing” button" in lines and "Listings & sales banner: Off" in lines


@pytest.mark.parametrize("changes,forms,socials,msg", [
    ({"buttons": {"My Agent Hub": False}}, None, None, "always on"),
    ({"buttons": {"selling": True}}, {"home_value"}, None, "don't have the I'm Selling form"),
    ({"buttons": {"selling": False}}, None, None, "no “selling” button"),
    ({"socials": {"tiktok": True}}, None, set(), "Add your TikTok link first"),
    ({"socials": {"linkedin": True}}, None, None, "Instagram, Facebook"),
    ({}, None, None, "What should change"),
])
def test_signature_refusals(changes, forms, socials, msg):
    with pytest.raises(ActionError, match=msg):
        more.apply_signature_changes({"ctaButtons": []}, changes, forms, socials)


def test_signature_button_limit():
    cfg = {"ctaButtons": [{"formType": "home_value", "label": "a", "enabled": True}, {"formType": "showing", "label": "b", "enabled": True}]}
    with pytest.raises(ActionError, match="already has 2 buttons"):
        more.apply_signature_changes(cfg, {"buttons": {"buying": True}}, {"buying"}, None)


def test_signature_card_says_to_recopy_when_installed(monkeypatch):
    async def ctx(db, user, need_forms):
        return {"blockVisibility": {"banner": True}}, None, set()

    async def route(user, method, path, body=None):
        assert method == "GET" and path == "/email-signature/me/html"
        return {"installed": True}
    monkeypatch.setattr(more, "_sig_context", ctx)
    monkeypatch.setattr(more, "_run_route", route)
    p = run(more._p_signature(None, USER, {"banner": False}))
    assert p.payload == {"changes": {"banner": False}} and "copy it again" in p.warning


# ---------------------------------------------------------------- setup, calendar, CRM, tickets

def test_setup_change_rules():
    assert more.setup_change("crm", "not_using", "todo")[0] == {"not_using": "crm"}
    assert more.setup_change("photo", "skip", "todo")[0] == {"skip": "photo"}
    assert more.setup_change("calendar", "bring_back", "not_using")[0] == {"use_again": "calendar"}
    assert more.setup_change("photo", "bring_back", "skipped")[0] == {"unskip": "photo"}
    for args, msg in ((("license", "skip", "todo"), "required"), (("photo", "not_using", "todo"), "only for Calendar"),
                      (("domain", "skip", None), "isn't part of setup"), (("photo", "skip", "done"), "already done"),
                      (("photo", "bring_back", "todo"), "isn't skipped"), (("nope", "skip", "todo"), "Which setup step")):
        with pytest.raises(ActionError, match=msg):
            more.setup_change(*args)


def test_resync_and_crm_test_cards(monkeypatch):
    state = {}

    async def route(user, method, path, body=None):
        return state["integrations"]
    monkeypatch.setattr(more, "_run_route", route)
    state["integrations"] = {"google_calendar": None, "microsoft_calendar": None}
    with pytest.raises(ActionError, match="isn't connected"):
        run(more._p_resync(None, USER, {}))
    state["integrations"] = {"google_calendar": {"connected": True, "needs_reconnect": False}}
    p = run(more._p_resync(None, USER, {}))
    assert p.payload == {"providers": ["google"]} and "Google Calendar" in p.title
    state["integrations"] = {"google_calendar": {"connected": True, "needs_reconnect": True}}
    with pytest.raises(ActionError, match="Reconnect"):
        run(more._p_resync(None, USER, {}))

    state["integrations"] = {}
    with pytest.raises(ActionError, match="isn't set up"):
        run(more._p_crm_test(None, USER, {}))
    state["integrations"] = {"crm_fub_connected": True, "crm_push_enabled": False}
    p = run(more._p_crm_test(None, USER, {}))
    assert "Follow Up Boss" in p.details[0] and any("switched off" in l for l in p.details) and "test contact" in p.warning


def test_ticket_reply_card(monkeypatch):
    monkeypatch.setattr(more, "_one", _fake_one(SimpleNamespace(id="t1", subject="Flyer won't print", status="closed")))
    p = run(more._p_ticket_reply(None, USER, {"ticket_id": "t1", "text": "Still broken on Safari"}))
    assert "Reopens the ticket" in p.details and "real person" in p.warning and p.payload == {"ticket_id": "t1", "text": "Still broken on Safari"}
    with pytest.raises(ActionError, match="What should the reply say"):
        run(more._p_ticket_reply(None, USER, {"ticket_id": "t1", "text": " "}))


def test_hosting_times_are_checked():
    with pytest.raises(ActionError, match="after the start"):
        more._times({"start_time": "14:00", "end_time": "13:00"})
    assert more._times({}, required=False) == (None, None)
    with pytest.raises(ActionError, match="already passed"):
        more._future_day("2020-01-01")


# ---------------------------------------------------------------- hosting cards (reads faked)

class _Res:
    def __init__(self, first=None, many=()):
        self._first, self._many = first, list(many)

    def first(self):
        return self._first

    def scalars(self):
        return SimpleNamespace(all=lambda: self._many)


class _DB:
    """Answers each db.execute in order."""
    def __init__(self, *results):
        self.results = list(results)

    async def execute(self, stmt):
        return self.results.pop(0)


def _names(monkeypatch):
    async def agent_name(db, uid):
        return "Dana Lee"

    async def address(db, key):
        return "12 Oak St"
    monkeypatch.setattr(more, "_agent_name", agent_name)
    monkeypatch.setattr(more, "_address", address)


def _ask(**kw):
    from datetime import time
    base = dict(id="b1", owner_id="o1", target_agent_id=None, status="open", listing_key="A", is_anytime=False,
                requested_date=date.today() + timedelta(days=5), requested_start_time=time(13), requested_end_time=time(15))
    base.update(kw)
    return SimpleNamespace(**base)


def test_team_ask_yes_as_asked_books_right_away(monkeypatch):
    _names(monkeypatch)
    monkeypatch.setattr(more, "_one", _fake_one(_ask()))
    db = _DB(_Res(first=("member",)), _Res(first=None))      # is a teammate; hasn't answered yet
    p = run(more._p_team_ask(db, USER, {"ask_id": "b1", "answer": "yes"}))
    assert p.payload == {"ask_id": "b1", "answer": "yes", "body": {}} and "first teammate" in p.details[1]
    assert "tells Dana Lee right away" in p.warning and p.confirm_label == "I'll host"


def test_team_ask_other_time_goes_for_approval_and_no_rules(monkeypatch):
    _names(monkeypatch)
    later = (date.today() + timedelta(days=6)).isoformat()
    monkeypatch.setattr(more, "_one", _fake_one(_ask()))
    p = run(more._p_team_ask(_DB(_Res(first=("m",)), _Res()), USER, {"ask_id": "b1", "answer": "yes", "date": later, "start_time": "11:00", "end_time": "13:00"}))
    assert p.payload["body"] == {"proposed_date": later, "proposed_start_time": "11:00:00", "proposed_end_time": "13:00:00"}
    assert "Goes to Dana Lee to approve" in p.details
    with pytest.raises(ActionError, match="stays open for the others"):
        run(more._p_team_ask(_DB(_Res(first=("m",)), _Res()), USER, {"ask_id": "b1", "answer": "no"}))
    monkeypatch.setattr(more, "_one", _fake_one(_ask(target_agent_id="u1")))
    p = run(more._p_team_ask(_DB(_Res()), USER, {"ask_id": "b1", "answer": "no"}))
    assert p.payload == {"ask_id": "b1", "answer": "no"} and "emails Dana Lee" in p.warning
    monkeypatch.setattr(more, "_one", _fake_one(_ask(is_anytime=True, requested_start_time=None, requested_end_time=None)))
    with pytest.raises(ActionError, match="any-time ask"):
        run(more._p_team_ask(_DB(_Res(first=("m",)), _Res()), USER, {"ask_id": "b1", "answer": "yes"}))
    monkeypatch.setattr(more, "_one", _fake_one(_ask(owner_id="o2")))
    with pytest.raises(ActionError, match="couldn't find"):
        run(more._p_team_ask(_DB(_Res(first=None)), USER, {"ask_id": "b1", "answer": "yes"}))   # not on their team


def test_other_time_card(monkeypatch):
    from datetime import time
    _names(monkeypatch)
    req = SimpleNamespace(id="r1", status="countered", listing_key="A", requesting_agent_id="u1", listing_owner_agent_id="o1", broadcast_id=None,
                          counter_date=date.today() + timedelta(days=3), counter_start_time=time(10), counter_end_time=time(12))
    monkeypatch.setattr(more, "_one", _fake_one(req))
    day = (date.today() + timedelta(days=4)).isoformat()
    p = run(more._p_other_time(_DB(_Res(many=["o1"])), USER, {"request_id": "r1", "date": day, "start_time": "13:00", "end_time": "15:00"}))
    assert p.payload["body"] == {"proposed_date": day, "proposed_start_time": "13:00:00", "proposed_end_time": "15:00:00", "note": None}
    assert "emails Dana Lee" in p.warning
    with pytest.raises(ActionError, match="already suggested another time"):
        run(more._p_other_time(_DB(_Res(many=["u1"])), USER, {"request_id": "r1", "date": day, "start_time": "13:00", "end_time": "15:00"}))
