"""A form question saved as JSON text opens back up (11/01 live: '[{"label": "Do you have a kitchen ...')."""
from app.services.form_question_heal import heal_questions, parse_blob

LIVE = ('[{"label": "Do you have a kitchen you\'d like to show off on our YouTube show?", "type": "select", '
        '"options": ["Yes, I\'d love to be featured", "Maybe, tell me more", "No, not right now"], "required": t')


def test_cut_off_text_keeps_the_whole_first_question():
    qs = parse_blob(LIVE)
    assert qs == [{"label": "Do you have a kitchen you'd like to show off on our YouTube show?", "type": "select",
                   "options": ["Yes, I'd love to be featured", "Maybe, tell me more", "No, not right now"], "required": True}]


def test_saved_form_heals_and_leaves_good_questions_alone():
    good = {"id": "a", "type": "short_text", "label": "Your street?", "required": False}
    bad = {"id": "ai_q1", "type": "short_text", "label": LIVE[:200], "required": False}
    out = heal_questions([good, bad])
    assert out[0] == good
    assert out[1]["id"] == "ai_q1" and out[1]["label"].startswith("Do you have a kitchen")
    assert heal_questions([good]) is None


def test_whole_json_and_a_cut_mid_options_question():
    full = '[{"label": "Budget?", "type": "currency"}, {"label": "Beds?", "type": "select", "options": ["1", "2"]}]'
    assert [q["label"] for q in parse_blob(full)] == ["Budget?", "Beds?"]
    cut = '[{"label": "Style?", "type": "select", "options": ["Colonial", "Ran'
    assert parse_blob(cut) == [{"label": "Style?", "type": "short_text"}]


def test_chat_card_reads_the_text_form():
    from app.services.ai.ai_actions_catalog import _form_questions
    qs, _ = _form_questions(LIVE)
    assert qs[0]["label"].startswith("Do you have a kitchen") and qs[0]["type"] == "select"
    qs, _ = _form_questions([LIVE, "Anything else?"])
    assert [q["label"][:10] for q in qs] == ["Do you hav", "Anything e"]
