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


def test_agent_paragraph_stays_the_agents_even_when_the_ai_copy_matches():
    # live 11/19: after one Update the AI's copy equalled the agent's text - the marker keeps it theirs
    comps = [{"listing_key": "A", "commentary": "Same words.", "ai_commentary": "Same words.", "commentary_by_agent": True}]
    assert ai._own_comp_notes(comps) == {"A": "Same words."}
    rep = _rep([{"listing_key": "A", "commentary": "Changed by a check.", "ai_commentary": "Changed by a check."}])
    ai._keep_own_comp_notes(rep, {"A": "Same words."})
    assert rep.comparables[0]["commentary"] == "Same words." and rep.comparables[0]["commentary_by_agent"] is True
    assert ai._own_comp_notes(rep.comparables) == {"A": "Same words."}      # still the agent's on the next run


def test_a_home_not_on_the_market_has_no_asking_price_in_the_comp_search():
    from app.services import cma_comp_pick as cp
    assert cp.asking_price_now({"list_price": 575000, "mls_status": "Closed"}) is None          # sold in 2024 (live 11/19)
    for st in ("Expired", "Withdrawn", "Canceled", "Off Market"):
        assert cp.asking_price_now({"list_price": 500000, "mls_status": st}) is None
    # for sale (every buyer report, a listed seller home): exactly as before
    for st in ("Active", "Coming Soon", "Active Under Contract", "Pending", "Under Contract", "", None):
        assert cp.asking_price_now({"list_price": 500000, "mls_status": st}) == 500000
    assert cp.asking_price_now({"mls_status": "Active"}) is None


def test_engine_label_never_ends_a_client_comp_note():
    f = ai._strip_internal_data_refs
    assert f("Its higher price reflects size and finish. Supporting weight.") == "Its higher price reflects size and finish."
    assert f("It is much more updated than the subject. Supporting weight, tempered by the finish gap.") == "It is much more updated than the subject."
    assert f("Smaller lot. Supporting weight after the below-grade split is accounted for.") == "Smaller lot."
    assert "weighted toward" in f("The kitchen is weighted toward storage.")      # ordinary English stays


def test_final_read_note_is_numbered_and_drops_self_cancelling_remarks():
    from app.services import cma_final_audit as audit
    assert audit._WEAK_NOTE_RE.search("Methodology says only one sale; fine, though Comp #2 is rated above")
    assert audit._WEAK_NOTE_RE.search("Fine against facts; but it omits the HOA fee")
    assert not audit._WEAK_NOTE_RE.search("Comp 3 sold in 2025, not 2024")
    msg = audit.agent_message([{"why": "comp 3 sold in 2025, not 2024.", "sentence": "It sold last year."},
                               {"why": "the lot is 1.6 acres", "sentence": "It sits on 2 acres."}])
    assert msg is None   # 11/21: the final read's own remarks stay off the agent's screen


def test_update_preview_says_where_the_value_would_go():
    from app.services import cma_seller_prefill as pf
    anchor = {"comps": [{"listing_key": k, "normalized_weight": 0.2, "size_adjusted_price": p, "status": "sold"}
                        for k, p in (("A", 600000), ("B", 620000), ("C", 640000), ("D", 660000), ("E", 680000))]}
    rep = SimpleNamespace(mode="listing", subject={"seller_answers": {"comp_marks": {k: "worse" for k in "ABCDE"}, "ai_filled": list("ABCDE")}},
                          pricing={"most_likely_value": 550000, "ai_estimate": {"valuation_trace": {"anchor": anchor}}})
    out = pf.preview_update(rep, {"B": "same"})
    # one cheaper sale is still "not as nice", so the rule holds the value at that sale ($600,000)
    assert out["ok"] and out["now"] == 550000 and out["about"] == 600000 and out["change_pct"] > 8
    assert pf.preview_update(rep, {"B": "nonsense"})["about"] < 600000            # a bad answer is ignored
    assert pf.preview_update(SimpleNamespace(mode="buyer", subject={}, pricing={}), {"B": "same"}) == {"ok": False}
