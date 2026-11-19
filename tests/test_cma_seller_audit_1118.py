"""Seller-side CMA audit (11/18): mistakes found reading the finished seller reports and the seller-only code."""
from app.services import cma_ai_service as ai


def test_the_repair_pass_uses_the_seller_wording_on_a_seller_report():
    """The main pass stopped telling a homeowner to 'verify the roof with the seller' after 2631 S Holbrook St (11/15),
    but the final auto-repair pass (_deterministic_text_fixes) still called the softener without the seller flag."""
    subject = {"street_address": "1 A ST", "public_remarks": "Brick rowhome with an updated kitchen."}
    text = "The home has a newer roof and an updated kitchen. It has no garage."
    buyer = ai._deterministic_text_fixes(text, "Listing Prep", subject)
    seller = ai._deterministic_text_fixes(text, "Listing Prep", subject, seller=True)
    if "with the seller" in buyer:       # the claim was caught - each reader gets their own wording
        assert "with the seller" not in seller
        assert "have any records ready for buyers" in seller
    else:
        assert seller == buyer


def _position(ask, days=58, lo=303000, hi=365000, value=313500, comps=None):
    return ai._seller_pricing_position(value=value, range_min=lo, range_max=hi, subject_asking=ask,
                                       status_timeline={"is_active": True, "active_market_days": days}, comps=comps or [])


def test_no_price_cut_while_the_asking_price_is_inside_the_range():
    """10918 Valley Forge Cir #918: asking $319,900, value $313,500, range to $365,000 -> was told to cut to $315,000."""
    t = _position(319900)
    assert "reposition" not in t.lower() and "defensible" in t and "inside the range" in t
    assert "look at presentation" in t                           # 58 days: the stale note stays
    t = _position(380000)                                        # above the whole range: the cut is still advised
    assert "repositioning to about $315,000" in t


def test_over_asking_comps_are_demand_not_a_promise():
    """104 Linda Ln: 'closed about 10% above their asking prices ... gives this home the best chance of the same'."""
    comps = [{"status": "sold", "sale_price": 660000, "list_price": 600000, "days_on_market": 6},
             {"status": "sold", "sale_price": 550000, "list_price": 500000, "days_on_market": 5},
             {"status": "sold", "sale_price": 640000, "list_price": 590000, "days_on_market": 7}]
    t = ai._seller_pricing_position(value=618000, range_min=580000, range_max=655000, subject_asking=None,
                                    status_timeline={}, comps=comps)
    assert "listing at about $620,000" in t
    assert "not a promise" in t and "best chance of the same" not in t


def _pending_trace(implied):
    return {"pending_competition": {"pendings_used": [{
        "implied_value": implied, "list_price": 624000, "match_score": 90, "distance_miles": 0.41,
        "street_address": "30 MEADOWBROOK LN", "days_on_market": 83}]}}


def test_a_seller_report_gets_no_second_price_from_a_home_under_contract():
    """104 Linda Ln: 'that asking price implies about $694,000, about 12% above this report's value'."""
    final = {"value": 618000, "range_min": 580000, "range_max": 655000}
    buyer, _ = ai._v10_pending_context_sentence("Value notes.", final=final, trace=_pending_trace(694000))
    seller, _ = ai._v10_pending_context_sentence("Value notes.", final=final, trace=_pending_trace(694000), seller=True)
    assert "$694,000" in buyer                                    # buyer report: unchanged
    assert "$694,000" not in seller and "12%" not in seller and "implies" not in seller
    assert "30 Meadowbrook Ln" in seller and "$624,000 asking price" in seller
    assert "points above the supported range" in seller and "caution on the high side" in seller
    inside, _ = ai._v10_pending_context_sentence("Value notes.", final=final, trace=_pending_trace(640000), seller=True)
    assert "falls inside the supported range and backs it" in inside and "$640,000" not in inside


# ---- the seller questions + placement band (11/18) --------------------------------------------------------------

from app.services import cma_service


def _anchor(prices, cond=None):
    cond = cond or [0] * len(prices)
    return {"comps": [{"status": "sold", "normalized_weight": 1 / len(prices), "size_adjusted_price": p,
                       "feature_adjustments": {"condition": c}} for p, c in zip(prices, cond)]}


def _subj(placement=None, **more):
    return {"street_address": "1 A ST", "seller_answers": {"placement": placement, **more}}


def test_the_placement_rule_matches_the_tested_numbers():
    low, med, high = 400000, 450000, 500000
    assert ai._placement_rule_value("above_all", 450000, low, med, high) == high * 1.05
    assert ai._placement_rule_value("below_all", 450000, low, med, high) == low * 0.95
    # above the middle: held in [median, top], then half way to that half's middle (475,000)
    assert ai._placement_rule_value("upper_half", 440000, low, med, high) == 450000 + 0.5 * (475000 - 450000)
    assert ai._placement_rule_value("lower_half", 490000, low, med, high) == 450000 + 0.5 * (425000 - 450000)


def test_the_placement_band_is_seller_only_and_needs_an_answer_and_three_sales():
    a = _anchor([400000, 450000, 500000])
    assert ai._seller_placement_check(_subj("above_all"), a, "buyer") is None
    assert ai._seller_placement_check(_subj(None), a, "listing") is None
    assert ai._seller_placement_check(_subj("not_sure"), a, "listing") is None
    assert ai._seller_placement_check(_subj("above_all"), _anchor([400000, 450000]), "listing") is None
    pc = ai._seller_placement_check(_subj("above_all"), a, "listing")
    assert pc["target"] == 525000 and pc["allowed_low"] < 525000 < pc["allowed_high"]


def test_the_band_leaves_out_the_ai_condition_step():
    """The AI's condition ratings already price the same thing - counted once, not twice."""
    pc = ai._seller_placement_check(_subj("above_all"), _anchor([420000, 470000, 520000], cond=[20000] * 3), "listing")
    assert pc["comp_high"] == 500000 and pc["target"] == 525000


def test_the_value_is_held_to_the_band():
    pc = ai._seller_placement_check(_subj("above_all"), _anchor([400000, 450000, 500000]), "listing")
    r = {"final_fmv": 455000, "range_low": 430000, "range_high": 480000}
    out, flags = ai._apply_placement_check(r, {"placement_check": pc})
    assert out["final_fmv"] == pc["allowed_low"] // 1000 * 1000 + (1000 if pc["allowed_low"] % 1000 else 0)
    assert out["range_low"] < out["final_fmv"] < out["range_high"] and flags
    inside, f2 = ai._apply_placement_check({**r, "final_fmv": 525000}, {"placement_check": pc})
    assert inside["final_fmv"] == 525000 and not f2 and inside["placement_check_applied"]["to"] == 525000
    # an asking-price band already set it: the placement band stays out
    asked = {**r, "asking_price_check_applied": {"from": 1, "to": 2}}
    assert ai._apply_placement_check(asked, {"placement_check": pc})[0] is asked


def test_the_agents_answers_are_facts_for_the_ai_and_the_fact_checks():
    s = _subj("upper_half", kitchen="redone", kitchen_year=2021, baths="updated",
              replaced=[{"item": "roof", "year": 2019}, {"item": "heating_cooling", "year": None}])
    t = ai._agent_walkthrough_text(s, for_prompt=True)
    assert "above the middle" in t and "Kitchen: fully redone in 2021" in t and "Bathrooms: updated" in t
    assert "roof replaced 2019" in t and "per the agent" in t
    assert ai._listing_documents_system_claim(s, "The roof was replaced in 2019.")
    assert not ai._listing_documents_system_claim(s, "The roof was replaced in 2015.")
    assert ai._subject_interior_documented({"source": "manual", "seller_answers": {"kitchen": "original"}})
    assert ai._agent_walkthrough_text({"seller_answers": {}}) is None


def test_the_value_steps_name_the_agents_placement():
    pc = ai._seller_placement_check(_subj("above_all"), _anchor([400000, 450000, 500000]), "listing")
    trace = {"deterministic_anchor": 450000, "placement_check": pc,
             "reasoning_model": {"placement_check_applied": {**pc, "from": 470000, "to": 510000}}}
    b = ai._v10_value_bridge(trace, value=510000, range_min=490000, range_max=535000)
    keys = [x["key"] for x in b["steps"]]
    assert "placement" in keys and "judgment" not in keys
    assert b["adjusted_sales_value"] + sum(x["amount"] for x in b["steps"]) == 510000
    assert b["confidence_cap"] == "medium" and b["range_min"] == 490000      # the range follows the value
    m, _ = ai._v10_placement_sentence("Value notes.", trace=trace)
    assert "has been inside this home" in m and "a little above the highest of them" in m


def test_the_agent_is_told_when_the_photos_disagree():
    pc = {"placement": "above_all", "placement_words": ai._PLACEMENT_WORDS["above_all"]}
    assert ai._placement_review_note({"placement_check": pc, "weighted_condition_signal": {"signal": 0.7}}, 500000)
    assert ai._placement_review_note({"placement_check": pc, "weighted_condition_signal": {"signal": -0.2}}, 500000) is None


def test_answers_lock_with_the_comps():
    a = {"seller_answers": {"placement": "above_all", "kitchen": None, "replaced": [{"item": "roof", "year": 2019}]}}
    b = {"seller_answers": {"placement": "above_all", "replaced": [{"item": "roof", "year": 2019}]}}
    assert cma_service._seller_answers(a) == cma_service._seller_answers(b)        # blank = missing
    assert cma_service._seller_answers(a) != cma_service._seller_answers({"seller_answers": {"placement": "upper_half"}})
    assert cma_service._seller_answers({}) == cma_service._seller_answers({"seller_answers": None}) == {}


# ---- comp by comp (owner 11/18: flip through each comp, better or worse, next) -----------------------------------

def _keyed_anchor(prices):
    return {"comps": [{"listing_key": f"K{i}", "status": "sold", "normalized_weight": 1 / len(prices),
                       "size_adjusted_price": p, "feature_adjustments": {}} for i, p in enumerate(prices)]}


def _marks(**m):
    return {"street_address": "1 A ST", "seller_answers": {"comp_marks": m}}


def test_comp_marks_set_the_value_between_what_it_beats_and_what_it_doesnt():
    a = _keyed_anchor([400000, 450000, 500000, 550000])
    pc = ai._seller_placement_check(_marks(K0="better", K1="better", K2="worse", K3="worse"), a, "listing")
    assert pc["how"] == "comp_marks" and pc["target"] == 475000
    pc = ai._seller_placement_check(_marks(K0="better", K2="same", K3="worse"), a, "listing")
    assert pc["target"] == 500000                                   # the 'about the same' comp
    pc = ai._seller_placement_check(_marks(K0="better", K1="better", K2="better", K3="better"), a, "listing")
    assert pc["target"] == round(550000 * 1.10) and pc["placement"] == "above_all"   # 10% past the top (11/18 re-test)
    pc = ai._seller_placement_check(_marks(K0="worse"), a, "listing")
    assert pc["target"] == round(400000 * 0.90) and pc["placement"] == "below_all"
    # 'not sure' and unknown comps are ignored; nothing usable = no band
    assert ai._seller_placement_check(_marks(K0="not_sure", ZZ="better"), a, "listing") is None
    assert ai._seller_placement_check(_marks(K0="better"), a, "buyer") is None


def test_a_weaker_match_cannot_drag_the_value_off_the_same_homes():
    # 612 Radnor Valley shape: two strong 'same' matches ~1.25M, a weaker renovated comp in a cheaper town at 989k
    a = {"comps": [{"listing_key": k, "status": "sold", "normalized_weight": w, "size_adjusted_price": p, "feature_adjustments": {}}
                   for k, w, p in (("S1", .26, 1277000), ("S2", .12, 1271000), ("W1", .23, 1116000), ("W2", .20, 1146000), ("W3", .18, 989000))]}
    pc = ai._seller_placement_check(_marks(S1="same", S2="same", W1="worse", W2="worse", W3="worse"), a, "listing")
    assert pc["target"] == 1116000                                  # the strong not-as-nice comps still cap it, the weak one doesn't
    assert pc["placement_words"] == "about the same as 2 and not as nice as 3"
    # equal weights (the usual case): the bracket still holds exactly as before
    eq = _keyed_anchor([400000, 450000, 500000, 550000])
    assert ai._seller_placement_check(_marks(K0="worse", K2="same", K3="same"), eq, "listing")["target"] == 400000


def test_contradicting_marks_still_give_a_sensible_value():
    a = _keyed_anchor([400000, 450000, 500000])
    pc = ai._seller_placement_check(_marks(K2="better", K0="worse"), a, "listing")     # beats the dearest, loses to the cheapest
    assert 400000 <= pc["target"] <= 500000


def test_comp_marks_reach_the_ai_and_the_client_sentence():
    s = _marks(K0="better", K1="worse")
    comps = [{"listing_key": "K0", "street_address": "10 OAK LN"}, {"listing_key": "K1", "street_address": "12 ELM ST"}]
    t = ai._agent_walkthrough_text(s, for_prompt=True, comps=comps)
    assert "nicer than 10 Oak Ln" in t and "not as nice as 12 Elm St" in t
    pc = ai._seller_placement_check(s, _keyed_anchor([400000, 450000, 500000]), "listing")
    m, _ = ai._v10_placement_sentence("Notes.", trace={"placement_check": pc})
    assert "compared it with each sale" in m and "nicer than 1 of the comparable sales and not as nice as 1" in m
