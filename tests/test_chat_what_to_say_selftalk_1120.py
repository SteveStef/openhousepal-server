"""A/B chat test 11/20: "what should I say to the first one?" reads their follow-up's own message; a Showcase check never
tells the agent about its own marking steps."""
import app.main  # noqa: F401
from app.services.ai import ai_showcase_cards as sc
from app.services.ai.ai_orchestrator_service import _ASKS_WHAT_TO_SAY as ASK


def test_what_to_say_is_about_a_person():
    for q in ("What should I say to the first one?", "what do I text Nia", "What should I email them?", "what to say to Wayne"):
        assert ASK.search(q), q
    for q in ("What should I say in my listing description?", "What should I write for my bio?", "what do I say about the kitchen"):
        assert not ASK.search(q), q


def test_showcase_reply_drops_its_own_checking_steps():
    a = ("Looking at the 8 matches, all of them describe a finished lower level or basement, so none need to be greyed out.\n\n"
         "I checked 17 homes and 8 are real matches, including 1587 Russell Rd in Paoli.")
    assert sc.no_self_talk(a) == "I checked 17 homes and 8 are real matches, including 1587 Russell Rd in Paoli."
    b = "Looking at the quotes, all 8 read as real, so there's nothing to mark as not real. I checked all 17 homes."
    assert sc.no_self_talk(b) == "I checked all 17 homes."
    keep = "I checked 17 homes: 6 are real matches and 2 aren't real matches (a community pool), shown greyed on the card."
    assert sc.no_self_talk(keep) == keep
    assert sc.no_self_talk("I'm not marking any as greyed out.") == "I'm not marking any as greyed out."   # never empties a reply
