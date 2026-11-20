"""Owner 11/19: (1) a comp paragraph the agent wrote is signed off - no run changes a word of it, the agent is told when
one would have; (2) Fix grammar: spelling / grammar only, a fix that changes a fact is never offered. Free - no model."""
from types import SimpleNamespace

import app.main  # noqa: F401
from app.services import cma_ai_service as ai
from app.services.ai import grammar_fix_service as gf


def _rep(comps, pricing=None):
    return SimpleNamespace(comparables=comps, pricing=pricing or {"ai_estimate": {"value": 1}})


def test_own_notes_are_only_the_agents():
    comps = [{"listing_key": "A", "commentary": "AI words.", "ai_commentary": "AI words."},
             {"listing_key": "B", "commentary": "My own words.", "ai_commentary": "AI words."},
             {"listing_key": "C", "commentary": "", "ai_commentary": "AI words."},
             {"id": "D", "commentary": "Typed before any AI ran."}]
    assert ai._own_comp_notes(comps) == {"B": "My own words.", "D": "Typed before any AI ran."}


def test_a_run_never_changes_the_agents_paragraph_and_says_so():
    own = {"B": "My own words."}
    rep = _rep([{"listing_key": "A", "commentary": "new AI", "ai_commentary": "new AI", "street_address": "1 OAK LN"},
                {"listing_key": "B", "commentary": "Rewritten by a check.", "ai_commentary": "x", "street_address": "2 ELM ST"}])
    ai._keep_own_comp_notes(rep, own)
    assert rep.comparables[1]["commentary"] == "My own words."               # word for word
    assert rep.comparables[0]["commentary"] == "new AI"                      # the AI's own paragraph still updates
    msg = rep.pricing["ai_estimate"]["own_notes_review"]["message"]
    assert "2 Elm St" in msg and "left exactly as you wrote it" in msg
    assert rep.pricing["ai_estimate"]["value"] == 1                          # nothing else in the estimate touched


def test_no_note_when_nothing_tried_to_change_it():
    own = {"B": "My own words."}
    rep = _rep([{"listing_key": "B", "commentary": "My own words.", "ai_commentary": "x"}])
    ai._keep_own_comp_notes(rep, own)
    assert "own_notes_review" not in rep.pricing["ai_estimate"]
    rep2 = _rep([{"listing_key": "Z", "commentary": "a"}])
    ai._keep_own_comp_notes(rep2, {})
    assert "own_notes_review" not in rep2.pricing["ai_estimate"]


def test_grammar_fix_offers_only_real_safe_fixes():
    held = {"1": "this home have a new roof and sold for $650,000 in march.",
            "2": "Great kitchen.",
            "3": "The deck was built in 2019 by the owner.",
            "4": "It sits on 0.5 acres near the park."}
    replies = [{"id": "1", "text": "This home has a new roof and sold for $650,000 in March."},   # grammar: offered
               {"id": "2", "text": "Great kitchen."},                                            # unchanged: not listed
               {"id": "3", "text": "The deck was built in 2021 by the owner."},                  # a number changed: refused
               {"id": "4", "text": "It sits on 0.5 acres near Valley Forge Park, a lovely spot with trails, shops and "
                                   "plenty of room for the whole family to enjoy."},              # grew a lot: refused
               {"id": "9", "text": "Not one of ours."}]
    changes, refused = gf.checked_changes(held, replies)
    assert [c["id"] for c in changes] == ["1"]
    assert changes[0]["after"].startswith("This home has")
    assert len(refused) == 2


def test_grammar_fix_refuses_a_new_name():
    changes, refused = gf.checked_changes({"1": "near the school"}, [{"id": "1", "text": "near Henderson school"}])
    assert changes == [] and refused


def test_grammar_fix_never_flips_or_reverses_meaning():
    mp = gf.meaning_problem
    assert mp("The kitchen is not updated.", "The kitchen is updated.")                       # lost a not
    assert mp("The basement is finished.", "The basement isn't finished.")                     # gained a not
    assert mp("It is nicer than comp 2.", "It is not as nice as comp 2.")                      # flipped
    assert mp("Sold over asking.", "Sold under asking.")
    assert mp("Priced below the comps.", "Priced above the comps.")
    assert mp("Has more space.", "Has less space.")
    assert mp("all three baths updated", "three baths updated")                                 # dropped "all"
    assert mp("the roof is newer", "the roof is older")
    # real spelling fixes still pass
    assert mp("this home is beter than the last one", "This home is better than the last one.") is None
    assert mp("it isnt finished", "It isn't finished.") is None
    assert mp("the owner dont want to sell", "The owner doesn't want to sell.") is None
    assert mp("the house have new floors", "The house has new floors.") is None
    assert mp("Its not updated", "It's not updated.") is None


def test_grammar_fix_keeps_questions_and_symbols():
    mp = gf.meaning_problem
    assert mp("Has an agent already (Lisa from Compass) dont send listings.", "Has an agent already (Lisa from Compass)? Don't send listings.")
    assert mp("Can you host Sunday?", "You can host Sunday.")
    assert mp("searches under 600k", "searches under $600k")
    assert mp("PRICE REDUCED!", "Price reduced.")
    assert mp("rates at 6.25%", "rates at 6.25 percent")
    assert mp("can you host it sunday?", "Can you host it Sunday?") is None
