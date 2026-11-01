"""AI helper Forms & Invites follow-through (11/01, ai_actions_forms / ai_lookups_forms) - the card
logic with no database or network: the pure helpers directly, and the prepare / adjust / run steps
with their reads and route calls swapped for fakes."""
import asyncio
from types import SimpleNamespace

import pytest

import app.main  # noqa: F401  (loads every router first - avoids a circular import)
from app.services.ai import ai_actions, ai_actions_forms as af, ai_lookups, ai_lookups_forms as lf
from app.services.ai.ai_actions import ActionError

USER = SimpleNamespace(id="u1", plan_tier="NO_MLS", is_admin=False, first_name="Pat", last_name="Lee")


def run(coro):
    return asyncio.run(coro)


NEW = ("mark_form_response_reviewed", "duplicate_form", "create_form_tracking_link", "add_unfinished_form_response_to_leads",
       "send_invite_to_leads", "remind_invite_non_responders", "set_invite_rsvps", "duplicate_invite")


def test_registered_for_every_plan_like_the_page():
    ai_actions._load()
    for n in NEW:
        assert n in ai_actions.ACTIONS and ai_actions.ACTIONS[n].plans is None
    for n in ("get_form_responses", "get_unfinished_form_responses", "get_invite_guests"):
        desc, schema, plans, _fn = ai_lookups.LOOKUPS[n]
        assert plans is None and schema["type"] == "object" and "Use for" in desc
    names = {d["name"] for d in ai_actions.action_tools_for(USER)}
    assert set(NEW) <= names
    # nothing here deletes
    assert not any("delete" in n for n in NEW)


# ---------------------------------------------------------------- matching helpers

def test_person_matching_handles_families_and_emails():
    assert lf.person_words("the Smiths") == ["smiths"]
    assert lf.person_matches(["smiths"], "John Smith", "js@example.com")
    assert lf.person_matches(["dana", "whitfield"], "Dana Whitfield", None)
    assert not lf.person_matches(["dana", "smith"], "Dana Whitfield", None)
    assert lf.person_matches(["dana@example.com"], None, "dana@example.com")
    assert lf.person_matches([], "anyone", None)


def test_form_phrases_find_the_right_form():
    forms = [{"id": "1", "title": "I'm Buying", "form_type": "buying"}, {"id": "2", "title": "I'm Selling", "form_type": "selling"},
             {"id": "3", "title": "What's My Home Worth?", "form_type": "home_value"}, {"id": "4", "title": "Buyer Onboarding", "form_type": "buyer_onboarding"},
             {"id": "5", "title": "Open House Survey", "form_type": "custom"}]
    assert [f["id"] for f in lf.match_forms(forms, "the buyer form")] == ["1"]
    assert [f["id"] for f in lf.match_forms(forms, "home value")] == ["3"]
    assert [f["id"] for f in lf.match_forms(forms, "buyer onboarding")] == ["4"]
    assert [f["id"] for f in lf.match_forms(forms, "open house survey")] == ["5"]
    assert lf.match_forms(forms, "the form") == []


def test_answers_read_plainly_without_contact_fields():
    rows = lf.answer_rows([
        {"questionId": "full_name", "label": "Full name", "type": "short_text", "value": "Dana"},
        {"questionId": "email", "label": "Email", "type": "email", "value": "d@example.com"},
        {"questionId": "q1", "label": "Price range", "type": "select", "value": "$400-500k"},
        {"questionId": "q2", "label": "Must haves", "type": "multi_select", "value": ["Garage", "Yard"]},
        {"questionId": "q3", "label": "Pre-approved?", "type": "select", "value": True},
        {"questionId": "s", "label": "Thanks!", "type": "statement", "value": "x"},
        {"questionId": "q4", "label": "Notes", "type": "long_text", "value": ""},
    ])
    assert rows == [{"question": "Price range", "answer": "$400-500k"}, {"question": "Must haves", "answer": "Garage, Yard"},
                    {"question": "Pre-approved?", "answer": "Yes"}]


# ---------------------------------------------------------------- send to leads: the picker's rows and taps

ROWS = [{"id": "L1", "name": "Ann", "email": "ann@example.com", "tag": "Open house guest"},
        {"id": "L2", "name": "Bob", "email": "bob@example.com", "tag": "Showcase", "invited": "Oct 30"},
        {"id": "L3", "name": "Cy", "email": None, "tag": "Form", "why": "No email"},
        {"id": "L4", "name": "Di", "email": "di@example.com", "tag": "Form", "why": "Unsubscribed"}]


def test_adjust_keeps_only_people_who_can_get_it():
    p = {"invite_id": "i1", "rows": ROWS, "selected": []}
    out = af._adjust_send(USER, p, {"lead_ids": ["L1", "L3", "L4", "L2", "nope"]})
    assert out["selected"] == ["L1", "L2"]
    with pytest.raises(ActionError):
        af._adjust_send(USER, p, {"lead_ids": ["L3", "L4"]})
    # no picks sent (an older card) -> what was pre-checked stands
    assert af._adjust_send(USER, {**p, "selected": ["L1"]}, {})["selected"] == ["L1"]


def test_send_run_emails_in_chunks_of_100_and_reports(monkeypatch):
    rows = [{"id": f"L{i}", "name": f"P{i}", "email": f"p{i}@example.com", "tag": "Form"} for i in range(150)]
    calls = []

    async def fake_route(user, method, path, body=None):
        calls.append((method, path, len(body["recipients"])))
        return {"sent": len(body["recipients"]), "skipped": [{"email": "x@example.com", "reason": "unsubscribed"}] if len(calls) == 1 else []}
    monkeypatch.setattr(af, "_run_route", fake_route)
    out = run(af._r_send_invite(None, USER, {"invite_id": "i1", "invite_title": "Party", "rows": rows, "selected": [r["id"] for r in rows]}))
    assert calls == [("POST", "/invites/i1/send", 100), ("POST", "/invites/i1/send", 50)]
    assert "150 people" in out["message"] and "Skipped 1" in out["message"] and out["link"].endswith("invite=i1")


def _send_setup(monkeypatch, leads, status="live"):
    async def fake_invite(user, a):
        return {"id": "i1", "title": "Holiday Party", "when_label": "Sat, Dec 6", "status": status, "is_past": False, "audience": "clients"}

    async def fake_call(user, method, path, json=None, params=None):
        assert path == "/leads"
        return leads

    async def fake_rows(db, user, inv_id, leads, extra):
        rows = [{"id": f"e:{e}", "name": e.split("@")[0], "email": e, "tag": "Added by you"} for e in extra]
        for l in leads:
            r = {"id": l["id"], "name": l["name"], "email": l.get("email"), "tag": af._SOURCE_TAG.get(l.get("primary_source"), "Lead"),
                 "oh": [o["id"] for o in l.get("open_houses") or []]}
            if not l.get("email"):
                r["why"] = "No email"
            if l.get("invited"):
                r["invited"] = "Oct 30"
            rows.append(r)
        return rows

    async def fake_from(db, user):
        return "from you (as Pat Lee) - replies come to you"
    monkeypatch.setattr(af, "resolve_invite", lambda u, a: _ok(fake_invite(u, a)))
    monkeypatch.setattr(af, "call_as_agent", fake_call)
    monkeypatch.setattr(af, "_send_rows", fake_rows)
    monkeypatch.setattr(af, "_send_from", fake_from)


async def _ok(coro):
    return await coro, None


LEADS = [{"id": "L1", "name": "Ann", "email": "ann@example.com", "primary_source": "open_house", "open_houses": [{"id": "oh1"}]},
         {"id": "L2", "name": "Bob", "email": "bob@example.com", "primary_source": "showcase"},
         {"id": "L3", "name": "Cy", "email": None, "primary_source": "form"},
         {"id": "L4", "name": "Eve", "email": "eve@example.com", "primary_source": "open_house", "open_houses": [{"id": "oh2"}], "invited": True}]


def test_send_card_prechecks_the_group_and_says_how_many(monkeypatch):
    _send_setup(monkeypatch, LEADS)
    p = run(af._p_send_invite(None, USER, {"invite": "party", "group": "open_house_guests"}))
    assert p.picker == "invite_send"
    assert p.payload["selected"] == ["L1"]            # Eve was already invited, so not pre-checked
    assert p.confirm_label == "Email 1 person"
    assert "emails everyone you check" in p.warning
    assert any("1 person checked" in l for l in p.details)
    assert all("oh" not in r for r in p.payload["rows"])   # internal bits never reach the browser
    # named people + typed addresses
    p = run(af._p_send_invite(None, USER, {"invite_id": "i1", "lead_ids": ["L2", "L3", "L4"], "emails": ["New@Example.com"]}))
    assert p.payload["selected"] == ["e:new@example.com", "L2", "L4"]   # Cy has no email
    assert p.confirm_label == "Email 3 people"
    # one open house's guests
    p = run(af._p_send_invite(None, USER, {"invite_id": "i1", "open_house_id": "oh1"}))
    assert p.payload["selected"] == ["L1"]
    # nothing named -> nobody pre-checked, the card asks
    p = run(af._p_send_invite(None, USER, {"invite_id": "i1"}))
    assert p.payload["selected"] == [] and p.confirm_label == "Email people"


def test_send_refuses_a_closed_invite(monkeypatch):
    _send_setup(monkeypatch, LEADS, status="closed")
    with pytest.raises(ActionError, match="closed"):
        run(af._p_send_invite(None, USER, {"invite_id": "i1"}))


# ---------------------------------------------------------------- RSVPs open/close, copy an invite

def test_close_and_reopen_rsvps(monkeypatch):
    inv = {"id": "i1", "title": "Party", "status": "live"}
    monkeypatch.setattr(af, "resolve_invite", lambda u, a: _ok(_val(inv)))
    p = run(af._p_rsvps(None, USER, {"invite_id": "i1"}))
    assert p.payload == {"invite_id": "i1", "status": "closed"} and p.confirm_label == "Close RSVPs"
    with pytest.raises(ActionError, match="already open"):
        run(af._p_rsvps(None, USER, {"invite_id": "i1", "open": True}))
    inv["status"] = "closed"
    p = run(af._p_rsvps(None, USER, {"invite_id": "i1", "open": True}))
    assert p.payload["status"] == "live"


async def _val(v):
    return v


def test_invite_errors_name_the_choices(monkeypatch):
    async def which(u, a):
        return None, {"error": "which_invite", "message": "Which invite?", "invites": [{"invite_id": "i1", "title": "Party", "when": "Sat"}]}
    monkeypatch.setattr(af, "resolve_invite", which)
    with pytest.raises(ActionError, match="Party .*id=i1"):
        run(af._p_dup_invite(None, USER, {}))


# ---------------------------------------------------------------- forms: copy, tracking link

FORMS = [{"id": "f1", "title": "I'm Buying", "form_type": "buying", "slug": "buying"},
         {"id": "f2", "title": "Open House Survey", "form_type": "custom", "slug": "survey"}]


def _forms(monkeypatch):
    async def fake(user):
        return FORMS, None
    monkeypatch.setattr(af, "my_forms", fake)


def test_duplicate_form_card(monkeypatch):
    _forms(monkeypatch)
    p = run(af._p_dup_form(None, USER, {"form": "buyer"}))
    assert p.payload["form_id"] == "f1" and "I'm Buying - Copy" in p.details[0]
    with pytest.raises(ActionError, match="Which form|couldn't tell"):
        run(af._p_dup_form(None, USER, {}))


def test_tracking_link_only_on_premade_forms(monkeypatch):
    _forms(monkeypatch)
    p = run(af._p_campaign(None, USER, {"form_id": "f1", "name": "Sept Postcard", "after_submit": "stay"}))
    assert p.payload["body"] == {"name": "Sept Postcard", "redirect_mode": "none"}
    with pytest.raises(ActionError, match="premade"):
        run(af._p_campaign(None, USER, {"form_id": "f2", "name": "X"}))
    with pytest.raises(ActionError, match="called"):
        run(af._p_campaign(None, USER, {"form_id": "f1"}))
    with pytest.raises(ActionError, match="web address"):
        run(af._p_campaign(None, USER, {"form_id": "f1", "name": "X", "after_submit": "own_link"}))


def test_unfinished_to_leads_card(monkeypatch):
    _forms(monkeypatch)
    items = [{"id": "s1", "_form_id": "f1", "_form": "I'm Buying", "contactName": "Gus Moe", "contactEmail": "gus@example.com",
              "answeredCount": 2, "totalQuestions": 6, "lastQuestionLabel": "Budget", "inProgress": False, "finishedLater": False, "leadId": None},
             {"id": "s2", "_form_id": "f1", "_form": "I'm Buying", "contactName": "Hal", "contactEmail": None, "contactPhone": None, "leadId": None},
             {"id": "s3", "_form_id": "f1", "_form": "I'm Buying", "contactName": "Ivy", "contactEmail": "ivy@example.com", "leadId": "lead9"}]

    async def fake_unfinished(user, db, forms):
        return items
    monkeypatch.setattr(af, "unfinished_for", fake_unfinished)
    p = run(af._p_unfinished(None, USER, {"person": "gus"}))
    assert p.payload == {"form_id": "f1", "session_id": "s1", "who": "Gus Moe"}
    assert any("2 of 6" in l and "Budget" in l for l in p.details) and "Doesn't email them" in p.details
    with pytest.raises(ActionError, match="no one to add"):
        run(af._p_unfinished(None, USER, {"session_id": "s2"}))
    with pytest.raises(ActionError, match="already in your Leads"):
        run(af._p_unfinished(None, USER, {"person": "ivy"}))
