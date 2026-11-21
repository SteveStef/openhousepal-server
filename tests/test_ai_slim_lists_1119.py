"""Owner 11/19: the AI chat's lists carry only what it answers from - details come from the item's own lookup."""
from app.services.ai import ai_slim

TASK = {"id": "t1", "title": "Follow up with Nia", "kind": "call", "due_at": "2026-11-20T17:30:00Z", "remind_at": "2026-11-20T17:30:00Z",
        "bucket": "upcoming", "has_message": True, "suggested_message": "Hi Nia, thanks for stopping by...", "on_calendar": False,
        "notes": "Auto-created: Signed in at 25 Manchester Ct\nContact: nia@example.com  |  6105550123\nLoved the kitchen",
        "lead": {"id": "l1", "name": "Nia", "phone": "6105550123", "email": "nia@example.com", "extra": "x"},
        "property": {"short": "25 Manchester Ct, Berwyn", "address": "25 Manchester Ct, Berwyn, PA", "mls": "PA1", "url": "/p/1",
                     "list_agent": {"name": "Someone", "phone": "1"}}, "comments": [], "created_at": "2026-11-19"}


def test_a_list_of_followups_leaves_the_written_message_for_the_one_person_ask():
    out = ai_slim.followups([TASK])
    t = out["followups"][0]
    assert "suggested_message" not in t and t["has_message"] and "word for word" in out["note"]
    assert out["count"] == 1 and t["notes"] == "Signed in at 25 Manchester Ct\nLoved the kitchen"          # the agent's own words stay
    assert t["lead"] == {"id": "l1", "name": "Nia", "phone": "6105550123", "email": "nia@example.com"}
    assert "list_agent" not in t["property"] and "remind_at" not in t and "on_calendar" not in t
    one = ai_slim.followups([TASK], one_person=True)
    assert one[0]["suggested_message"].startswith("Hi Nia")


def test_forms_showcases_and_leads_keep_what_answers_questions():
    f = ai_slim.forms([{"id": "f1", "title": "I'm Buying", "is_enabled": False, "questions": [{}, {}, {}], "luxe_metal": "gold"}])
    assert f["forms"] == [{"id": "f1", "title": "I'm Buying", "is_enabled": False, "question_count": 3}]
    s = ai_slim.showcases([{"id": "c1", "name": "Wayne", "status": "ACTIVE", "matchedProperties": [{}] * 50,
                            "preferences": {"max_price": 700000, "min_beds": None, "cities": ["Exton"], "townships": [], "is_condo": False}}])
    assert s == {"showcases": [{"id": "c1", "name": "Wayne", "status": "ACTIVE", "preferences": {"max_price": 700000, "cities": ["Exton"]}}], "count": 1}
    lead = ai_slim.lead_row({"id": "l1", "name": "Nia", "needs_attention": True, "has_commented": False, "tags": [], "showcase_new_matches": 0, "last_event_at": None})
    assert lead == {"id": "l1", "name": "Nia", "needs_attention": True}
    rules = ai_slim.followup_rules({"rules": [{"key": "a", "template": "Hi", "default_template": "Hi", "custom_template": False},
                                              {"key": "b", "template": "Mine", "default_template": "Hi", "custom_template": True}]})
    assert "default_template" not in rules["rules"][0] and rules["rules"][1]["default_template"] == "Hi"
    assert ai_slim.drop_none({"a": None, "b": [{"c": None, "d": 0}]}) == {"b": [{"d": 0}]}
