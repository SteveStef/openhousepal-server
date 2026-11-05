"""Forks (a question that only shows after a certain answer): the visibility rule the server shares
with FormRunner, and the safety net that runs when a form is saved."""
import copy

import pytest

from app.api.forms_routes import DEFAULT_FORMS
from app.services.forms_service import clean_question_forks, question_is_visible


def q(id, type="select", options=None, depends_on=None, **extra):
    out = {"id": id, "type": type, "label": id, **extra}
    if options is not None:
        out["options"] = options
    if depends_on is not None:
        out["depends_on"] = {"question_id": depends_on[0], "values": depends_on[1]}
    return out


CHAIN = [
    q("a", options=["buy", "sell"]),
    q("b", options=["yes", "no"], depends_on=("a", ["buy"])),
    q("c", "short_text", depends_on=("b", ["no"])),
]


@pytest.mark.parametrize("form_type", sorted(DEFAULT_FORMS))
def test_premade_forms_come_through_untouched(form_type):
    questions = copy.deepcopy(DEFAULT_FORMS[form_type]["questions"])
    assert clean_question_forks(copy.deepcopy(questions)) == questions


def test_a_hidden_parent_hides_its_follow_ups():
    answers = {"a": "sell", "b": "no"}  # went buy -> no, then came back and picked sell
    assert not question_is_visible(CHAIN[1], CHAIN, answers)
    assert not question_is_visible(CHAIN[2], CHAIN, answers)
    answers["a"] = "buy"
    assert question_is_visible(CHAIN[2], CHAIN, answers)


def test_multi_select_answer_opens_the_follow_up():
    qs = [q("a", "multi_select", ["x", "y"]), q("b", "short_text", depends_on=("a", ["y"]))]
    assert question_is_visible(qs[1], qs, {"a": ["x", "y"]})
    assert not question_is_visible(qs[1], qs, {"a": ["x"]})
    assert not question_is_visible(qs[1], qs, {"a": []})


def test_parent_can_be_a_field_on_an_earlier_screen():
    qs = [
        q("gate", options=["on", "off"]),
        q("group", "contact_group", depends_on=("gate", ["on"]), fields=[{"id": "kind", "type": "select", "label": "Kind"}]),
        q("follow", "short_text", depends_on=("kind", ["house"])),
    ]
    assert question_is_visible(qs[2], qs, {"gate": "on", "kind": "house"})
    assert not question_is_visible(qs[2], qs, {"gate": "off", "kind": "house"})
    assert clean_question_forks(copy.deepcopy(qs)) == qs


def test_a_loop_never_shows_and_never_hangs():
    qs = [q("a", depends_on=("b", ["1"])), q("b", depends_on=("a", ["1"]))]
    assert not question_is_visible(qs[0], qs, {"a": "1", "b": "1"})


def test_parent_outside_the_question_list_reads_the_answer():
    qs = [q("a", "short_text", depends_on=("hidden_id", ["1"]))]
    assert question_is_visible(qs[0], qs, {"hidden_id": "1"})
    assert not question_is_visible(qs[0], qs, {})


def test_drops_answers_the_parent_no_longer_offers():
    qs = [q("a", options=["x", "y"]), q("b", "short_text", depends_on=("a", ["x", "gone"]))]
    assert clean_question_forks(qs)[1]["depends_on"] == {"question_id": "a", "values": ["x"]}


def test_clears_a_fork_with_no_answers_left():
    qs = [q("a", options=["x"]), q("b", "short_text", depends_on=("a", ["gone"]))]
    assert clean_question_forks(qs)[1]["depends_on"] is None


def test_clears_a_fork_whose_parent_is_gone():
    qs = [q("b", "short_text", depends_on=("deleted", ["x"]))]
    assert clean_question_forks(qs)[0]["depends_on"] is None


def test_clears_a_fork_whose_parent_comes_later():
    qs = [q("b", "short_text", depends_on=("a", ["x"])), q("a", options=["x"])]
    assert clean_question_forks(qs)[0]["depends_on"] is None


def test_leaves_questions_without_a_fork_alone():
    qs = [q("a", options=["x"]), q("b", "short_text")]
    assert clean_question_forks(copy.deepcopy(qs)) == qs
