"""10/24 grader round (239 Windham Dr, 417 Dogwood Ln, 4913 Chancellor St, 191 Presidential Blvd #R-605,
102 Steeplewood Dr, 8084 Goshen Rd, 612 Radnor Valley Dr). The inputs are the shapes those reports had; the
assertions are the rules, never a hardcoded value."""
from datetime import date

import app.main  # noqa: F401
from app.services import cma_ai_service as ai
from app.services import cma_fact_check as fc
from app.services import cma_service as cs

WINDHAM = {"street_address": "239 WINDHAM DR", "zipcode": "19341", "subdivision_name": "TURNBURY", "township": "UWCHLAN",
           "list_price": 1300000, "above_grade_finished_area": 3524, "sqft": 5024, "below_grade_finished_area": 1500}


# ---- fact gate ---------------------------------------------------------------------------------

def test_same_community_claim_rewritten_when_subdivisions_differ():
    linda = {"street_address": "100 LINDA CIR", "subdivision_name": "UWCHLAN HUNT"}
    text = "A 1994 colonial in the same Uwchlan Hunt community and Downingtown Area School District as the subject."
    out, flags = fc.fix_text(text, subject=WINDHAM, comp=linda)
    assert "same Uwchlan Hunt" not in out and "nearby Uwchlan Hunt community" in out and flags
    # Steeplewood (Lynnbrooke) vs 223 Warren Ct (Windham)
    out, _ = fc.fix_text("Comp #1 is the most recent same-neighborhood sale.", subject={"subdivision_name": "LYNNBROOKE"},
                         comp={"subdivision_name": "WINDHAM"})
    assert "same-neighborhood" not in out
    # a real same-subdivision claim stays
    same, flags = fc.fix_text("It is in the same Turnbury community.", subject=WINDHAM, comp={"subdivision_name": "TURNBURY"})
    assert same == "It is in the same Turnbury community." and not flags


def test_township_in_the_subdivision_field_is_not_a_subdivision():
    assert fc.subdivision_of({"subdivision_name": "UWCHLAN TWP", "township": "UWCHLAN"}) is None
    assert fc.same_subdivision(WINDHAM, {"subdivision_name": "UWCHLAN TWP"}) is None


def test_same_building_condos_count_as_one_community():
    a = {"street_address": "191 PRESIDENTIAL BLVD #R-605", "zipcode": "19004", "subdivision_name": "BALA"}
    b = {"street_address": "191 PRESIDENTIAL BLVD #805", "zipcode": "19004", "subdivision_name": "PRESIDENTIAL ESTS"}
    assert fc.same_subdivision(a, b) is True
    assert "same building" in fc.neighborhood_vs_subject(a, b)


def test_subject_psf_must_be_the_above_grade_list_rate():
    bad = "It sold for $256/sqft, essentially in line with the subject's own $256/sqft list price."
    assert fc.text_problems(bad, subject=WINDHAM, label="Comp #2")
    assert not fc.text_problems("At $284/sqft it is below the subject's $369/sqft list price.", subject=WINDHAM)


def test_finished_basement_of_unknown_size_is_never_called_absent():
    woodland = {"basement": "Partially Finished", "below_grade_finished_area": 0}
    out, flags = fc.fix_lower_level_claims("It sold despite having no finished basement and dated cabinetry.", woodland, "Comp #2")
    assert "no finished basement" not in out and "finished basement of unrecorded size" in out and flags
    measured = {"basement": "Full, Fully Finished", "below_grade_finished_area": 1000}
    assert fc.lower_level_claim_problem("It has no finished lower level.", measured)


def test_field_names_and_repeated_sentences_removed():
    out, _ = fc.fix_field_name_leaks("The listing does not document updates (no documented_system_updates are listed).")
    assert "_" not in out
    twice = ("The range tops out at the $500,000 asking price because the home hasn't yet drawn an accepted offer at that level. "
             "The range stops at the $500,000 asking price because the home hasn't drawn an accepted offer at that price yet.")
    out, flags = fc.drop_repeated_sentences(twice)
    assert len(fc.sentences(out)) == 1 and flags


def test_implausible_lot_is_unknown_not_431_acres():
    assert fc.implausible_lot(18774360 / 43560, 930000)
    assert not fc.implausible_lot(4.0, 1325000)
    assert ai._relevance_lot_sqft({"lot_size": 18774360, "sale_price": 930000}) is None

    class P:  # a Property row's shape
        lot_size, price, home_type = 18774360.0, 930000.0, "SINGLE_FAMILY"
    assert cs._sane_lot_size(P(), 930000) is None


# ---- value bridge ------------------------------------------------------------------------------

def _trace(anchor, *, apc=None, model_fmv=None, applied=None, pend=None, drivers=None, drags=None, grid=None):
    rm = {"major_value_drivers": drivers or [], "major_value_drags": drags or []}
    if applied:
        rm["asking_price_check_applied"] = applied
    return {"authority": "reasoning_model", "deterministic_anchor": anchor, "anchor": {"anchor_value": anchor, "comps": grid or []},
            "asking_price_check": apc or {}, "pending_competition": pend or {}, "reasoning_model": rm,
            "reasoning_model_fmv": model_fmv}


def _adds_up(b):
    return b["adjusted_sales_value"] + sum(s["amount"] for s in b["steps"]) == b["final_value"]


def test_bridge_names_the_asking_price_floor_and_keeps_the_sales_in_the_range():
    apc = {"asking_price": 1300000, "expected_sale_price": 1272700, "allowed_low_price": 1081795,
           "allowed_high_price": 1272700, "ratio_used": 0.979, "passed_over": True}
    b = ai._v10_value_bridge(_trace(1001396, apc=apc), value=1082000, range_min=1020000, range_max=1150000)
    assert _adds_up(b) and [s["key"] for s in b["steps"]] == ["listing"]
    assert "asking price" in b["steps"][0]["basis"] and b["range_min"] <= 1001396 and b["confidence_cap"] == "medium"
    text = ai._bridge_client_text(b)
    assert "can't fully capture" not in text and "$81,000" in text


def test_bridge_math_is_exact_when_pendings_also_move_it():
    # 8084 Goshen Rd: adjusted sales $1,517K, reasoning $1.40M toward the $1.325M asking, pendings -0.7%
    apc = {"asking_price": 1325000, "expected_sale_price": 1325000, "allowed_low_price": 1192500,
           "allowed_high_price": 1457500, "ratio_used": 1.0}
    pend = {"nudged_from": 1400000, "nudge_pct": -0.7, "pendings_used": [{"street_address": "5 STONEHENGE LN"}]}
    b = ai._v10_value_bridge(_trace(1516512, apc=apc, pend=pend), value=1390000, range_min=1265000, range_max=1490000)
    assert _adds_up(b) and {s["key"] for s in b["steps"]} == {"listing", "pending"}
    assert sum(abs(s["amount"]) for s in b["steps"]) == 127000


def test_bridge_flags_judgment_for_features_the_grid_already_priced():
    grid = [{"feature_adjustments": {"garage": 5475, "condition": 14600}, "size_adjustment": 0}]
    drivers = [{"factor": "Updated kitchen and renovated primary bathroom", "direction": "up",
                "evidence_strength": "directionally_supported_amount_uncertain"},
               {"factor": "Two garage spaces", "direction": "up", "evidence_strength": "directionally_supported_amount_uncertain"}]
    b = ai._v10_value_bridge(_trace(352212, drivers=drivers, grid=grid), value=375000, range_min=360000, range_max=395000)
    j = next(s for s in b["steps"] if s["key"] == "judgment")
    assert j["already_in_grid"] and any("Review before sending" in c for c in b["checks"]) and b["range_min"] <= 352212


def test_range_is_not_capped_at_asking_for_a_home_that_sat():
    chk = {"asking_price": 500000, "allowed_low": 416500, "allowed_high": 490000, "passed_over": True,
           "expected_sale_price": 490000, "ratio_used": 0.98}
    out, flags = ai._apply_asking_price_check({"final_fmv": 490000, "range_low": 470000, "range_high": 515000,
                                               "confidence": "medium"}, {"asking_price_check": chk})
    assert out["range_high"] == 515000 and not flags


def test_pending_context_sentence_labels_it_as_not_a_sale():
    trace = {"pending_competition": {"pendings_used": [{"street_address": "102 STEEPLEWOOD DR", "list_price": 1200000,
                                                        "implied_value": 1129292, "match_score": 78, "distance_miles": 0.42,
                                                        "days_on_market": 9}]}}
    out, _ = ai._v10_pending_context_sentence("The value is $1,082,000.", final={"value": 1082000, "range_min": 1000000,
                                                                                    "range_max": 1150000}, trace=trace)
    assert "102 Steeplewood Dr" in out and "not treated as a sale" in out and "$1,129,000" in out


def test_asking_price_is_never_rewritten_as_a_stale_value():
    # 191 Presidential dry run: the anchor's top sat near the ask, and "$399,999 asking price" became the value
    text = "The concluded value of $354,259 sits below the $399,999 asking price because the sales come in beneath it."
    trace = {"subject_asking": 399999, "anchor": {"anchor_value": 352212, "anchor_high": 399600},
             "ai_value_cross_check": 372000}
    out, _vd, _f = ai._reconcile_post_gate_prose({"m": ("Methodology summary", text)}, value=354259, range_pct=6.0,
                                                  anchor=trace["anchor"], trace=trace, value_drivers=[])
    assert "$399,999 asking price" in out["m"][1]


# ---- offers ------------------------------------------------------------------------------------

def test_underpriced_offer_ceiling_is_the_value_not_the_asking_price():
    band = ai._v10_build_buyer_offer_band(1390000, 1325000, trace={"anchor": {"comp_list_to_sale_median": 1.0}},
                                          confidence="medium", status_timeline={"active_market_days": 1})
    assert band["ceiling"] == 1390000 and band["target"] <= 1390000
    text, _ = ai._v10_rebuild_buyer_offer_prose("", band, 8.0)
    assert "never above" not in text


def test_under_contract_offer_is_a_backup_at_the_value():
    band = ai._v10_build_buyer_offer_band(1150000, 1200000, trace={}, confidence="medium",
                                          status_timeline={"is_under_contract": True, "active_market_days": 8})
    assert band["backup"] and band["opening_high"] <= 1150000 and band["opening_low"] >= 1150000 * 0.97
    text, _ = ai._v10_rebuild_buyer_offer_prose("", band, 6.0)
    assert "already accepted an offer after 8 days" in text and "never above" not in text


# ---- record facts --------------------------------------------------------------------------------

def test_condo_fee_from_association_fee_2():
    fees = fc.monthly_fees({"condo_fee": 2405, "condo_fee_frequency": "Monthly", "property_type": "CONDO"})
    assert fees == [{"label": "Condo fee", "amount": 2405, "frequency": "monthly", "monthly": 2405}]
    assert fc.monthly_fees({"association_fee": 495, "association_fee_frequency": "Annually"})[0]["monthly"] == 41


def test_relisted_home_counts_all_its_market_time():
    events = [{"date": "2026-10-09", "event": "Listed for sale", "price": 450000},
              {"date": "2026-10-09", "event": "Taken off market", "price": 450000},
              {"date": "2026-10-06", "event": "Price changed", "price": 450000},
              {"date": "2026-08-13", "event": "Listed for sale", "price": None},
              {"date": "2026-03-13", "event": "Sold", "price": 200000}]
    h = fc.summarize_history(events, current_list_date="2026-10-09", today=date(2026, 10, 24))
    assert h["relisted"] and h["current_listing_days"] == 15 and h["marketing_days_total"] == 72
    assert h["last_sale"] == {"date": "2026-03-13", "price": 200000.0}
    packets = [{"sale_price": 3e5, "sold_list_ratio": r, "dom": 30} for r in (0.97, 0.98, 1.0)]
    chk = ai._asking_price_market_check({"current_list_price": 450000, "dom": 15, "marketing_days_total": 72}, {},
                                        {"state": "normal_exposure", "comp_median_dom": 30}, packets)
    assert chk["passed_over"]


def test_indoor_pool_is_always_addressed():
    goshen = {"pool": ["Yes - Personal"], "public_remarks": "Enjoy the indoor lap pool year round."}
    delchester = {"street_address": "989 DELCHESTER RD", "public_remarks": "Saltwater pool and finished basement."}
    s = fc.feature_coverage_sentence("The value is $1,390,000.", goshen, [delchester])
    assert "indoor pool" in s and "989 Delchester Rd" in s
    assert not fc.feature_coverage_sentence("Its indoor pool is unique here.", goshen, [delchester])
    assert not fc.high_impact_features({"pool": ["Yes - Community"]}, condo=True)


# ---- comp selection ------------------------------------------------------------------------------

def test_same_street_detection():
    a = {"street_address": "612 Radnor Valley Dr", "zipcode": "19085", "latitude": 40.0186, "longitude": -75.3665}
    b = {"street_address": "660 RADNOR VALLEY DRIVE", "zipcode": "19085", "latitude": 40.0148, "longitude": -75.3651}
    far = {"street_address": "660 RADNOR VALLEY DR", "zipcode": "19085", "latitude": 40.10, "longitude": -75.20}
    assert ai._same_street(a, b) and not ai._same_street(a, far)


def test_sale_outside_the_price_band_is_shown_not_picked():
    c = cs.CmaCompCandidate(listing_key="x", status="sold", sale_price=1850000, similarity_details={})
    marked = cs._mark_outside_band(c, 1325000)
    assert not cs._auto_pickable(marked) and marked.similarity_details["outside_price_band"]["pct_vs_asking"] == 40
