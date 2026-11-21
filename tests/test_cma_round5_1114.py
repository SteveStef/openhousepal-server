"""Round 5 of the CMA test (11/14 evening): six homes of kinds the engine had not been tested on - a to-be-built
townhouse, a 55+ ranch, a luxury home on 3.45 acres, a rural home sold at auction, a condo flat and a 5-unit
building. Each test below is a mistake found in one of those six real reports."""
from app.services import cma_ai_service as ai


# ---- "$415 monthly" was read as "$415 m" (million) and scrubbed mid-word ------------------------------------------

def test_a_monthly_fee_is_never_scrubbed_as_an_invented_adjustment():
    text = ("Gather the HOA documents, the $415 monthly fee breakdown and the 55+ age restrictions for buyers. "
            "Budget for the $220/month association fee and the $1,250 annual dues, about $104 a month.")
    out, flags = ai._scrub_invented_adjustments(text, {"value": 460000})
    assert out == text and flags == []
    assert "valueonthly" not in out


def test_money_suffixes_are_whole_words_only():
    rx = ai._MONEY_TOKEN_RE
    assert rx.search("the $415 monthly fee").group(0) == "$415"
    assert rx.search("about $1.2M above").group(0) == "$1.2M"
    assert rx.search("roughly $300K premium").group(0) == "$300K"
    assert rx.search("$2 million estate").group(0) == "$2 million"
    assert rx.search("$950 more per year").group(0) == "$950"
    assert rx.search("$25,000 kitchen").group(0) == "$25,000"


def test_an_invented_adjustment_is_still_scrubbed():
    out, flags = ai._scrub_invented_adjustments("The new kitchen adds $75,000 to the value.", {"value": 460000})
    assert "$75,000" not in out and flags


# ---- the Pricing Position quoted a different range from the rest of the report --------------------------------------

def _position(**kw):
    base = dict(value=460000, range_min=444000, range_max=478000, subject_asking=469900,
                status_timeline={"active_market_days": 61}, comps=[])
    base.update(kw)
    return ai._seller_pricing_position(**base)


def test_pricing_position_quotes_the_reports_own_range():
    text = _position()
    assert "(supported range $444,000 to $478,000)" in text
    assert "$445,000" not in text and "$480,000" not in text


def test_pricing_position_low_confidence_also_quotes_the_exact_range():
    text = _position(subject_asking=None, status_timeline={"not_listed": True}, confidence="low")
    assert "$444,000 to $478,000" in text


# ---- an auction's list price is an opening bid ---------------------------------------------------------------------

def test_auction_listing_is_not_told_its_asking_price_is_below_value():
    comps = [{"sale_price": 593000, "list_price": 589900, "days_on_market": 3, "is_included": True},
             {"sale_price": 552800, "list_price": 535000, "days_on_market": 27, "is_included": True}]
    text = _position(value=545000, range_min=500000, range_max=590000, subject_asking=500000,
                     status_timeline={"active_market_days": 26, "auction": True}, comps=comps)
    assert "opening bid, not an asking price" in text and "$545,000" in text
    for wrong in ("no reason to reduce", "strong early interest", "below the supported value", "repositioning",
                  "without an accepted offer", "priced to its value"):
        assert wrong not in text, wrong
    # the same home without the auction flag still gets the ordinary wording
    plain = _position(value=545000, range_min=500000, range_max=590000, subject_asking=500000,
                      status_timeline={"active_market_days": 26}, comps=comps)
    assert "below the supported value" in plain


def test_the_timeline_knows_an_auction_from_the_mls_sale_type():
    tl = ai._subject_status_timeline({"list_price": 500000, "mls_status": "Active", "days_on_market": 26,
                                      "sale_type": ["Auction"]}, None)
    assert tl.get("auction") is True
    tl = ai._subject_status_timeline({"list_price": 500000, "mls_status": "Active", "days_on_market": 26,
                                      "sale_type": ["Standard"]}, None)
    assert tl.get("auction") is False


# ---- one day count for the current listing ---------------------------------------------------------------------------

def test_the_current_listing_uses_the_mls_day_count_when_it_is_close_to_the_dates():
    from datetime import date
    from app.services import cma_fact_check as fc
    events = [{"date": "2026-09-10", "event": "Listed for sale", "price": 469900},
              {"date": "2026-08-21", "event": "Taken off market", "price": 485000},
              {"date": "2026-07-31", "event": "Listed for sale", "price": 485000}]
    # counted from the dates: 65 days on this listing, 86 in all
    h = fc.summarize_history(events, current_list_date="2026-09-10", today=date(2026, 11, 14))
    assert h["current_listing_days"] == 65 and h["marketing_days_total"] == 86
    # the MLS says 61 (it does not count the coming-soon days): the report uses 61 everywhere
    h = fc.summarize_history(events, current_list_date="2026-09-10", today=date(2026, 11, 14), current_days=61)
    assert h["current_listing_days"] == 61 and h["marketing_days_total"] == 82
    assert "61 days on this listing, about 82 days on the market in total" in h["lines"][-1]
    # an MLS count far from the dates is some other number - the dates stand
    h = fc.summarize_history(events, current_list_date="2026-09-10", today=date(2026, 11, 14), current_days=7)
    assert h["current_listing_days"] == 65


# ---- a builder's listing of a new home is not "underpriced" -----------------------------------------------------------

def test_a_builders_new_home_is_not_called_underpriced():
    tl = ai._subject_status_timeline({"list_price": 555000, "mls_status": "Active", "days_on_market": 22,
                                      "new_construction_yn": True, "year_built": 2026}, None)
    assert tl.get("builder_listing") is True
    band = ai._v10_build_buyer_offer_band(580000, 555000, status_timeline=tl, range_min=562000, range_max=600000)
    assert band["underpriced"] and band["builder_listing"]
    text, _ = ai._v10_rebuild_buyer_offer_prose("", band, 0.03)
    assert "looks underpriced" not in text
    assert "builder's price for this home as it is offered" in text and "not a discount" in text
    assert "reasonable opening offer is roughly" in text
    # a resale that was new construction once, or an ordinary resale, keeps the ordinary wording
    resale = ai._subject_status_timeline({"list_price": 555000, "mls_status": "Active", "days_on_market": 22,
                                          "new_construction_yn": True, "last_sale_price": 500000, "last_sale_date": "2025-03-01"}, None)
    assert resale.get("builder_listing") is False
    band = ai._v10_build_buyer_offer_band(580000, 555000, status_timeline=resale, range_min=562000, range_max=600000)
    text, _ = ai._v10_rebuild_buyer_offer_prose("", band, 0.03)
    assert "looks underpriced" in text


# ---- a step amount is not a second value ------------------------------------------------------------------------------

def _qa(text):
    trace = {"anchor": {"anchor_value": 2187200, "comps": []}}
    _, _, blocking = ai._v10_pre_export_qa(
        {"methodology_summary": ("Methodology summary", text)}, value=1925000, range_pct=0.07, subject_asking=1950000,
        status_timeline={"is_active": True, "active_market_days": 32}, comps=[], comp_num_by_key={}, addr_by_key={},
        valuation_trace=trace, value_drivers=[], confidence="medium", subject={})
    return [b for b in blocking if "FMV consistency" in b]


def test_the_size_of_a_value_step_is_not_read_as_a_rival_value():
    text = ("The final fair market value is $1,925,000. How the value was reached: the adjusted comparable sales point "
            "to about $2,187,000. This home is listed at $1,950,000. The value is therefore not set above about "
            "$1,935,000. That takes off $262,000. Together: $1,925,000.")
    assert _qa(text) == []


def test_a_real_second_value_is_still_caught():
    assert _qa("The final fair market value is $1,925,000. In our view the fair market value is $2,400,000.")


# ---- the agent is told when one or two sales carry the adjusted-sales figure -----------------------------------------

def _trace(rows):
    return {"anchor": {"comps": [{"label": f"Comp #{i + 1} ({a})", "listing_key": f"k{i}", "size_adjusted_price": p,
                                  "normalized_weight": w} for i, (a, p, w) in enumerate(rows)]}}


def test_two_far_sales_that_lift_the_figure_are_named_for_the_agent():
    note = ai._adjusted_spread_review_note(_trace([
        ("116 ACORN WAY", 540261, 0.304), ("150 DURHAM DR", 500387, 0.25), ("119 JACOBS DR", 646795, 0.247),
        ("1235 W KINGS HWY", 530630, 0.10), ("6142 BEAVER DAM RD", 790840, 0.10)]))
    msg = note["message"]
    assert "119 Jacobs Dr ($647,000 after adjusting) and 6142 Beaver Dam Rd ($791,000 after adjusting)" in msg
    assert "sit well above the other 3" in msg and "$524,000" in msg and "$581,000" in msg and "11% higher" in msg
    assert note["far"] == ["k2", "k4"]


def test_sales_that_agree_or_pull_both_ways_raise_no_note():
    agree = _trace([("1 A ST", 450691, 0.23), ("2 A ST", 475911, 0.21), ("3 A ST", 499008, 0.21), ("4 A ST", 430908, 0.18),
                    ("5 A ST", 452544, 0.17)])
    assert ai._adjusted_spread_review_note(agree) is None
    both = _trace([("1 A ST", 500000, 0.3), ("2 A ST", 505000, 0.3), ("3 A ST", 700000, 0.2), ("4 A ST", 330000, 0.2)])
    assert ai._adjusted_spread_review_note(both) is None
    few = _trace([("1 A ST", 500000, 0.5), ("2 A ST", 505000, 0.3), ("3 A ST", 700000, 0.2)])
    assert ai._adjusted_spread_review_note(few) is None


# ---- condos: the same number of bedrooms counts in the match score ---------------------------------------------------

_SUBURB = {"band": "suburban", "distance_half_life_miles": 1.0, "nearest_distances": [], "candidate_count": 5}
_FLAT = {"listing_key": "subject", "property_type": "CONDO", "beds": 2, "baths": 2, "sqft": 1230, "above_grade_finished_area": 1230,
         "year_built": 1985, "city": "Chesterbrook", "township": "TREDYFFRIN", "school_district": "TREDYFFRIN-EASTTOWN",
         "subdivision_name": "CHESTERBROOK", "latitude": 40.07, "longitude": -75.45}


def _flat_sale(key, **kw):
    base = {"listing_key": key, "status": "sold", "price_verified": True, "is_included": True, "property_type": "CONDO",
            "city": "Chesterbrook", "township": "TREDYFFRIN", "school_district": "TREDYFFRIN-EASTTOWN",
            "subdivision_name": "CHESTERBROOK", "close_date": "2026-08-21", "sale_price": 300000, "close_price": 300000,
            "sqft": 1146, "above_grade_finished_area": 1146, "year_built": 1985, "distance_miles": 0.3, "beds": 2, "baths": 2}
    base.update(kw)
    return base


def test_a_one_bedroom_flat_scores_below_the_same_flat_with_the_subjects_bedroom_count():
    rel = ai._continuous_relevance(_FLAT, [_flat_sale("two"), _flat_sale("one", beds=1, baths=1), _flat_sale("unknown", beds=None)],
                                   density_context=dict(_SUBURB))
    rows = {r["listing_key"]: r for r in rel["comps"]}
    assert rows["two"]["components"]["bedrooms"] == 1.0 and rows["one"]["components"]["bedrooms"] == 0.35
    assert "bedrooms" not in rows["unknown"]["components"]
    assert rows["two"]["base_relevance_score"] > rows["one"]["base_relevance_score"] + 0.03
    assert ai._CONTINUOUS_RELEVANCE_COMPONENT_WEIGHTS["bedrooms"] == 15.0


def test_bedroom_count_is_not_a_match_score_part_for_houses():
    house = dict(_FLAT, property_type="SINGLE_FAMILY", lot_size=10000)
    rel = ai._continuous_relevance(house, [_flat_sale("a", property_type="SINGLE_FAMILY", beds=3, lot_size=10000)],
                                   density_context=dict(_SUBURB))
    assert "bedrooms" not in rel["comps"][0]["components"]


# ---- the MLS field says "Unfinished", the listing text says "finished basement" --------------------------------------

def test_a_basement_conflict_is_said_in_the_write_up_and_the_table_is_left_alone():
    comps = [
        {"street_address": "111 CAMRYN CT", "basement": "Unfinished", "above_grade_finished_area": 2114, "sqft": 2114,
         "property_type": "TOWNHOUSE", "public_remarks": "The finished basement creates additional living space.",
         "ai_commentary": "Comp #2 is an identical-size Brentford with an unfinished basement."},
        {"street_address": "9 PLAIN CT", "basement": "Unfinished", "above_grade_finished_area": 2114, "sqft": 2114,
         "property_type": "TOWNHOUSE", "public_remarks": "Bright and airy.", "ai_commentary": "Comp #3 has an unfinished basement."},
        {"street_address": "110 CAMRYN CT", "basement": "Full, Fully Finished", "below_grade_finished_area": 418,
         "above_grade_finished_area": 2114, "ai_commentary": "Comp #5 has a finished basement."},
    ]
    flags = ai._append_basement_conflict_notes(comps)
    assert len(flags) == 1 and "111 CAMRYN CT" in flags[0]
    note = comps[0]["ai_commentary"]
    assert "lists the basement as unfinished, but its listing text describes a finished basement" in note
    assert "about 530 sq ft of finished lower level" in note and comps[0]["commentary"] == note
    assert comps[1]["ai_commentary"] == "Comp #3 has an unfinished basement."
    assert comps[2]["ai_commentary"] == "Comp #5 has a finished basement."
    assert ai._append_basement_conflict_notes(comps) == []          # said once
    assert ai._V10_BG_FIELD_WINS is False                           # the table still goes with the listing text



# ---- the length of an earlier listing is a real day count -------------------------------------------------------------

def _reasoning_blockers(earlier):
    packet = {"subject": {"dom": 61, "marketing_days_total": 82, "earlier_listing_days": earlier, "current_list_price": 469900},
              "comparables": [{"comp_id": "c1", "dom": 6, "sale_price": 460000}], "deterministic_evidence": {}, "market_tempo": {}}
    result = {"final_fmv": 460000, "range_low": 444000, "range_high": 478000, "confidence": "Medium",
              "final_valuation_summary": "The home has been listed 61 days, 82 in all; its earlier listing came off the market after "
                                   "21 days. The closest sale sold in 6 days."}
    _, blockers = ai._validate_reasoning_valuation(result, packet)
    return [b for b in blockers if "DOM figure" in b]


def test_an_earlier_listings_day_count_is_not_an_invented_figure():
    assert _reasoning_blockers([21]) == []
    assert any("21 days" in b for b in _reasoning_blockers(None))      # a count the record does not hold is still caught


# ---- outside audit of round 5: "the listing shows no inclusions" ------------------------------------------------------

def test_a_claim_that_the_listing_states_no_inclusions_is_not_made_from_a_field_we_do_not_have():
    from app.services import cma_fact_check as fc
    t = ("Keep an inspection contingency. The listing shows no stated inclusions or exclusions, so confirm in the agreement "
         "that the in-unit washer and dryer convey. Keep an appraisal contingency as well.")
    out, flags = fc.fix_inclusion_absence_claims(t)
    assert "shows no stated inclusions" not in out and flags
    assert "The inclusions and exclusions are not in our MLS data, so confirm in the agreement that the in-unit washer and dryer convey." in out
    assert out.startswith("Keep an inspection contingency.") and out.endswith("Keep an appraisal contingency as well.")
    bare, _ = fc.fix_inclusion_absence_claims("The listing lists no inclusions. Ask what stays with the home.")
    assert bare == "Ask what stays with the home."
    same = "Ask the seller for a written list of inclusions and exclusions."
    assert fc.fix_inclusion_absence_claims(same) == (same, [])


# ---- outside audit of round 5: the asking-price figures quoted are the home's own part of the market ----------------

def test_asking_price_statistics_come_from_the_homes_own_segment():
    assert ai._asking_stat_segment("house", 1950000) == "house_1500k"
    assert ai._asking_stat_segment("house", 1200000) == "house_1000k"
    assert ai._asking_stat_segment("house", 555000, builder_listing=True) == "new_construction"
    assert ai._asking_stat_segment("house", 469900) is None and ai._asking_stat_segment("condo", 1800000) is None
    lux = ai._asking_dom_stat("house", 32, "house_1500k")
    assert lux["n"] == 390 and lux["pct_above_ask"] == 8.5 and lux["kind"] == "homes that sold for $1.5 million or more"
    new = ai._asking_dom_stat("house", 22, "new_construction")
    assert new["median_ratio"] == 1.0 and new["kind"] == "newly built homes"
    plain = ai._asking_dom_stat("house", 32, None)
    assert plain["n"] == 19101 and plain["kind"] == "homes"
    assert ai._asking_dom_stat("condo", 60, None)["kind"] == "condos"


# ---- test round 6 (11/15): "a much lot" - a true 'larger lot' said of a SALE was cut out of the summary --------------

def test_a_larger_lot_said_of_a_sale_is_left_alone():
    subject = {"lot_size": 2200}
    comps = [{"street_address": "135 W 6TH ST", "lot_size": 8000}, {"street_address": "709 GEORGE ST", "lot_size": 5347},
             {"street_address": "706 CHAIN ST", "lot_size": 1800}]
    for s in ("The one sale in the same school district, 135 W 6th St, is far larger, with a finished lower level and a much larger lot, so it marks an upper limit.",
              "Of those, 709 George St is the best match on condition; it has a third unit, a larger lot and much lower taxes, so it may overstate this home.",
              "Comp #2 closed at asking. It has a bigger yard and three driveway spaces.",
              "Most of the comparable sales sit on a larger lot than this home."):
        assert ai._reconcile_lot_size_claims(s, "Methodology summary", subject, comps) == (s, [])


def test_a_larger_lot_claimed_for_the_subject_is_still_removed_and_reads_cleanly():
    subject = {"lot_size": 2200}
    comps = [{"street_address": "135 W 6TH ST", "lot_size": 8000}, {"street_address": "709 GEORGE ST", "lot_size": 5347}]
    out, flags = ai._reconcile_lot_size_claims("Supportable given the documented updates and larger lot.", "Offer", subject, comps)
    assert out == "Supportable given the documented updates." and flags
    out, flags = ai._reconcile_lot_size_claims("This home's much larger lot is its main strength.", "Offer", subject, comps)
    assert out == "This home's lot is its main strength." and flags          # no "much lot"


# ---- test round 6 (11/15), 5232 Diamond St: two wording rules --------------------------------------------------------

def test_sold_as_is_is_a_sale_term_not_a_listing_status():
    rx = ai._QA_STATUS_ASSERT_RE
    for s in ("The property is sold as-is with no seller repairs.", "The home is sold as is.", "The listing is sold strictly as-is.",
              "The home is sold in as-is condition.", "It is sold with tenants in place.", "This property is sold at auction.",
              "The property is sold subject to the existing leases."):
        assert not rx.search(s), s
    for s in ("The home is sold.", "The property is sold and closed in June.", "The listing is pending.", "It is now under contract."):
        assert rx.search(s), s


def test_no_included_or_excluded_items_is_the_same_claim_in_other_words():
    from app.services import cma_fact_check as fc
    out, flags = fc.fix_inclusion_absence_claims("The listing is active at 9 days. The listing names no included or excluded items. Be ready to walk away.")
    assert "no included" not in out and flags and out == "The listing is active at 9 days. Be ready to walk away."
    keep = "The washer and dryer are included in the sale."
    assert fc.fix_inclusion_absence_claims(keep) == (keep, [])


# ---- test round 6 (11/15), 2631 S Holbrook St: two more wording rules ------------------------------------------------

def test_a_false_most_recent_sale_still_reads_as_a_sentence():
    comps = [{"close_date": "2026-10-16", "distance_miles": 0.4}, {"close_date": "2026-10-20", "distance_miles": 0.3},
             {"close_date": "2025-12-01", "distance_miles": 0.2}]
    out, flags = ai._fix_comp_note_superlatives("It sold after 27 days, the most recent sale and same subdivision.", "Comp #3", comps[0], comps)
    assert out == "It sold after 27 days, a recent sale and same subdivision." and flags
    out, _ = ai._fix_comp_note_superlatives("It is the most recent sale in the set.", "Comp #5", comps[2], comps)
    assert out == "It is an earlier sale in the set."
    true = "It is the most recent sale in the set."
    assert ai._fix_comp_note_superlatives(true, "Comp #2", comps[1], comps) == (true, [])


def test_a_seller_report_never_tells_the_seller_to_verify_with_the_seller():
    subject = {"street_address": "1 A ST", "public_remarks": "Brick rowhome with an updated kitchen."}
    text = "The home has a newer roof and an updated kitchen. It has no garage."
    buyer, notes_b = ai._soften_unsupported_system_claims(text, subject)
    seller, notes_s = ai._soften_unsupported_system_claims(text, subject, seller=True)
    if notes_b:        # the claim was caught (the rule's own conditions decide that) - then each reader gets their own wording
        assert "with the seller" in buyer
        assert "with the seller" not in seller and "have any records ready for buyers" in seller and notes_s
    else:
        assert buyer == text and seller == text


# ---- outside audit of round 6 (11/15): what the advice says must fit where the home is in its sale -------------------

def test_no_price_cut_is_recommended_in_a_listings_first_days():
    """110 S Valentine Dr: listed four days, told to 'reposition to about $655,000'."""
    comps = [{"sale_price": 615000, "list_price": 629000, "days_on_market": 14, "is_included": True, "status": "sold"},
             {"sale_price": 650000, "list_price": 680000, "days_on_market": 15, "is_included": True, "status": "sold"},
             {"sale_price": 690000, "list_price": 725000, "days_on_market": 20, "is_included": True, "status": "sold"}]
    kw = dict(value=655000, range_min=628000, range_max=680000, subject_asking=685000, comps=comps)
    early = ai._seller_pricing_position(status_timeline={"active_market_days": 4}, **kw)
    assert "We recommend repositioning" not in early
    assert "too soon to read the market's answer" in early and "4 days" in early
    assert "if there is no offer by about day 15, reposition toward $655,000" in early
    later = ai._seller_pricing_position(status_timeline={"active_market_days": 40}, **kw)
    assert "We recommend repositioning to about $655,000" in later and "too soon" not in later
    # with no days on record the paragraph does not guess that it is early
    unknown = ai._seller_pricing_position(status_timeline={}, **kw)
    assert "We recommend repositioning" in unknown


def test_a_home_under_contract_gets_no_launch_or_overpricing_advice():
    """2631 S Holbrook St: under contract, and the seller's report still covered competition, overpricing and launch."""
    strategy = ("**Pricing Position:** The home is under contract. This analysis documents its supported market value.\n\n"
                "**Competition:** Buyers will compare it with Comp #1.\n\n"
                "**Overpricing Risk:** Comp #5 sat 376 days.\n\n"
                "**Launch:** Lead with the updated kitchen.")
    out, flags = ai._under_contract_seller_strategy(strategy, {"is_under_contract": True})
    assert "**Launch:**" not in out and "**Overpricing Risk:**" not in out and "**Competition:**" not in out
    assert out.startswith("**Pricing Position:** The home is under contract.") and "**While Under Contract:**" in out and flags
    # a home that is simply for sale keeps every section
    assert ai._under_contract_seller_strategy(strategy, {"is_active": True}) == (strategy, [])


def test_above_asking_advice_says_when_it_rests_on_condition():
    """911 Pheasant Run: 'looks underpriced', open above asking - while every sale used was a more updated home."""
    tl = ai._subject_status_timeline({"list_price": 365000, "mls_status": "Active", "days_on_market": 10}, None)
    trace = {"weighted_condition_signal": {"signal": 0.796, "direction": "subject_inferior", "weight_covered": 1.0}}
    band = ai._v10_build_buyer_offer_band(385000, 365000, trace=trace, status_timeline=tl, range_min=365000, range_max=410000)
    assert band["underpriced"] and band["condition_dependent"]
    text, _ = ai._v10_rebuild_buyer_offer_prose("", band, 0.03)
    assert "looks underpriced" not in text
    assert "confirm its condition before offering above the asking price" in text and "$385,000" in text
    # comps in similar condition: the ordinary wording stays
    band = ai._v10_build_buyer_offer_band(385000, 365000, trace={"weighted_condition_signal": {"signal": 0.05, "direction": "similar_or_mixed"}},
                                          status_timeline=tl, range_min=365000, range_max=410000)
    text, _ = ai._v10_rebuild_buyer_offer_prose("", band, 0.03)
    assert not band["condition_dependent"] and "looks underpriced" in text


def test_a_multi_family_buildings_baths_never_come_from_one_units_rental_listing():
    """519 Grove St (outside audit of round 6): '5 beds, 2 baths' - the 2 came from a rental listing of one unit."""
    from app.services import cma_service as cs
    lease_unit = (2.0, 3.0, "RESIDENTIAL_LEASE", "Residential Lease")
    assert cs.whole_building_baths(5.0, [lease_unit]) is None                       # unknown, not borrowed
    assert cs.whole_building_baths(5.0, [(1.0, 2.0, "SINGLE_FAMILY", "Residential")]) is None   # fewer bedrooms = one unit
    assert cs.whole_building_baths(5.0, [lease_unit, (2.5, 5.0, "TOWNHOUSE", "Residential")]) == 2.5   # the same building, filed as a home
    assert cs.whole_building_baths(None, [(3.0, 6.0, "SINGLE_FAMILY", "Residential")]) == 3.0
    assert cs.whole_building_baths(5.0, []) is None


# ---- 11/15: the agent-only evidence label, and the plain sentence on an income property -------------------------------
# Three signs, all about the SALES (owner: "it should be based on the comps' similarity score"): the adjusted sales and
# the asking price 10%+ apart, large adjustments, and sales that are not close matches. Never the lot or the price.

def _ev(gross=(6, 8, 9), match=(92, 88, 90), anchor=500000, asking=505000):
    keys = [f"k{i}" for i in range(len(gross))]
    t = {"anchor": {"comps": [{"listing_key": k, "normalized_weight": 1 / len(gross), "gross_adjustment_pct": g} for k, g in zip(keys, gross)]},
         "deterministic_anchor": anchor}
    if asking:
        t.update({"subject_asking": asking, "asking_price_check": {"asking_price": asking}})
    return t, [{"listing_key": k, "match_score": m} for k, m in zip(keys, match)]


def test_evidence_is_strong_when_none_of_the_three_signs_is_present():
    t, comps = _ev()
    e = ai._evidence_strength({"property_type": "SINGLE_FAMILY"}, t, 500000, comps)
    assert e["level"] == "strong" and e["signs"] == [] and e["match_score"] == 90.0
    assert e["message"].startswith("Strong evidence. On past sales that looked like this one")


def test_one_sign_is_fair_and_two_are_limited():
    t, comps = _ev(anchor=440000, asking=505000)                              # sales 13% under the asking price
    one = ai._evidence_strength({}, t, 440000, comps)
    assert one["level"] == "fair" and "13% apart" in one["message"]
    t, comps = _ev(anchor=440000, asking=505000, match=(74, 71, 78))          # ... and the sales are not close matches
    two = ai._evidence_strength({}, t, 440000, comps)
    assert two["level"] == "limited" and len(two["signs"]) == 2 and "Match score about 74" in two["message"]
    t, comps = _ev(gross=(22, 30, 18), match=(70, 72, 68))                    # big adjustments on loose matches
    assert ai._evidence_strength({}, t, 500000, comps)["level"] == "limited"


def test_the_kind_of_home_is_never_a_sign():
    """Owner, 11/15: acreage and a price of $1 million or more are not signs - the sales' Match score is."""
    t, comps = _ev(anchor=1900000, asking=1950000)
    e = ai._evidence_strength({"lot_size": 3.5 * 43560, "property_type": "SINGLE_FAMILY"}, t, 1925000, comps)
    assert e["level"] == "strong" and e["signs"] == []
    assert "acres" not in e["message"] and "million" not in e["message"]


def test_a_manufactured_home_or_a_parking_space_is_declined_like_a_commercial_listing():
    """Owner, 11/15: "We shouldn't do mobile homes... keep it to more traditional real estate." """
    from app.services.cma_service import unusual_structure_problem
    for design in ("Manufactured", "Mobile Pre 1976", "manufactured"):
        msg = unusual_structure_problem(design)
        assert msg.startswith("The MLS lists this as a manufactured (mobile) home.") and "a CMA can't value this one" in msg
    assert "garage or parking space" in unusual_structure_problem("Garage/Parking Space")
    for design in ("Detached", "Interior Row/Townhouse", "End of Row/Townhouse", "Twin/Semi-Detached", "Unit/Flat/Apartment",
                   "Penthouse Unit/Flat/Apartment", "Other", "", None):
        assert unusual_structure_problem(design) is None, design


def test_a_bank_owned_short_or_estate_sale_is_never_strong():
    """Free checks 11/15: on these kinds of SALE the asking price is a weaker guide (18-26% sell more than 10% away
    from it, 7% of ordinary sales), and with no sign at all they missed like ordinary homes with one."""
    t, comps = _ev()
    for subject, words in (({"sale_type": ["Bank Owned/REO"]}, "a bank-owned home"), ({"sale_type": ["HudOwned"]}, "a bank-owned home"),
                           ({"sale_type": ["ShortSale"]}, "a short sale"), ({"sale_type": ["Standard", "ProbateListing"]}, "an estate sale")):
        e = ai._evidence_strength(subject, t, 500000, comps)
        assert e["level"] == "fair" and e["signs"] == [] and e["special_sale"] == words
        assert e["message"].startswith(f"Fair evidence. The sales are close matches, but this is {words}")
    for subject in ({"sale_type": ["Standard"]}, {"sale_type": "Standard"}, {"sale_type": ["ThirdPartyApproval"]}, {}):
        e = ai._evidence_strength(subject, t, 500000, comps)
        assert e["level"] == "strong" and e["special_sale"] is None
    t, comps = _ev(anchor=440000, asking=505000, match=(74, 71, 78))          # already Limited: stays, and says why too
    e = ai._evidence_strength({"sale_type": ["ShortSale"]}, t, 440000, comps)
    assert e["level"] == "limited" and "This is also a short sale" in e["message"]
    t, comps = _ev(asking=None)                                               # no asking price: nothing about one to say
    e = ai._evidence_strength({"sale_type": ["ShortSale"]}, t, 500000, comps)
    assert e["level"] == "fair" and e["special_sale"] is None and "short sale" not in e["message"]


def test_sales_that_merely_disagree_are_not_a_sign():
    t, comps = _ev()
    for r, p in zip(t["anchor"]["comps"], (430000, 500000, 575000)):
        r["size_adjusted_price"] = p
    assert ai._evidence_strength({}, t, 500000, comps)["level"] == "strong"


def test_a_home_with_no_asking_price_is_fair_unless_both_signs_show():
    t, comps = _ev(asking=None)
    e = ai._evidence_strength({}, t, 500000, comps)
    assert e["level"] == "fair" and "no asking price to check the sales against" in e["message"] and e["listed"] is False
    t, comps = _ev(asking=None, match=(70, 72, 68))                           # one sign: still fair
    assert ai._evidence_strength({}, t, 500000, comps)["level"] == "fair"
    t, comps = _ev(asking=None, match=(70, 72, 68), gross=(22, 30, 18))       # both: limited
    assert ai._evidence_strength({}, t, 500000, comps)["level"] == "limited"


def test_a_duplex_is_always_limited_and_says_there_is_no_rent_data():
    subject = {"property_type": "MULTI_FAMILY", "lot_size": 2200, "two_bedroom_units": "1", "three_bedroom_units": "1"}
    t, comps = _ev()
    e = ai._evidence_strength(subject, t, 378000, comps)
    assert e["level"] == "limited" and e["multi_family"] and "no rent or expense data" in e["message"]
    s = ai._income_property_sentence(subject)
    assert s.startswith("This is a two-unit income property.") and "rents and expenses are not in the MLS and were not analyzed" in s
    assert ai._income_property_sentence({"property_type": "SINGLE_FAMILY"}) is None
    assert ai._income_property_sentence({"property_type": "MULTI_FAMILY"}).startswith("This is a multi-unit income property.")


def test_unit_count_comes_from_the_unit_mix_the_mls_sends():
    from app.services import cma_service as cs
    assert cs.multi_family_unit_count({"two_bedroom_units": "1", "three_bedroom_units": "1", "one_bedroom_units": "0"}) == 2
    assert cs.multi_family_unit_count({"raw_mls_data": {"OneBedroomUnits": 2, "TwoBedroomUnits": 1}}) == 3
    assert cs.multi_family_unit_count({"public_remarks": "A five-unit building, fully rented."}) == 5      # the listing's words still work
    assert cs.multi_family_unit_count({"one_bedroom_units": "1"}) is None                                 # one unit is not a count


# ---- 11/15: the accuracy-test time machine and a home's own EARLIER sale ------------------------------------------------

def test_the_time_machine_lets_a_homes_own_earlier_sale_through_but_never_the_hidden_one():
    """2992 2nd St, Norristown: sold June 2024, relisted 2026. Its 2024 sale was public on the as-of date; the test
    report was stopped for "subject carries last_sale_price" before it began."""
    import pytest
    from datetime import date
    from app.services import cma_time_machine as tmm
    tm = tmm.TimeMachine(listing_key="k1", as_of=date(2026, 3, 31), list_price=299900.0, dom=8, close_price=299000.0, close_date=date(2026, 7, 30))
    token = tmm.activate(tm)
    try:
        ok = {"list_price": 299900.0, "last_sale_price": 331700.0, "last_sale_date": "2024-06-07"}
        tmm.check_structured(ok, {"current_status": "Active"}, [], "test")                       # an earlier sale: fine
        for bad in ({"last_sale_price": 331700.0},                                                # no date: cannot be shown to be earlier
                    {"last_sale_price": 331700.0, "last_sale_date": "2026-07-30"},                # dated after the as-of day
                    {"last_sale_price": 299000.0, "last_sale_date": "2024-06-07"},                # the hidden price itself
                    {"close_price": 299000.0}):
            with pytest.raises(tmm.TimeMachineLeak):
                tmm.check_structured({"list_price": 299900.0, **bad}, {"current_status": "Active"}, [], "test")
    finally:
        tmm.deactivate(token)


def test_the_kind_of_sale_and_the_unit_mix_survive_to_the_end_of_the_run():
    """The subject is rebuilt from the saved report before the label is written; a short sale's label came out with no
    word about it (11/15, first browser-built short-sale report)."""
    kept = ai._subject_runtime_facts({"sale_type": ["ShortSale"], "two_bedroom_units": "2", "street_address": "2992 2ND ST", "condo_fee": 0})
    assert kept["sale_type"] == ["ShortSale"] and kept["two_bedroom_units"] == "2" and "street_address" not in kept
    assert ai._special_sale_kind({**{"street_address": "2992 2ND ST"}, **kept}) == "a short sale"


def test_the_homes_earlier_sale_is_stated_once():
    """2992 2nd St (11/15): "The home last sold on Jun 7, 2024 for $331,700; ... It last sold on Jun 7, 2024 for
    $331,700; ..." - the same sale said twice, back to back."""
    import datetime
    from app.services import cma_fact_check as fc
    hist = {"last_sale": {"date": "2024-06-07", "price": 331700}}
    today = datetime.date(2026, 3, 31)
    once = fc.history_sentence("The value is $300,000.", hist, today=today)
    assert once.count("$331,700") == 1 and "This home last sold on Jun 7, 2024 for $331,700" in once
    assert fc.history_sentence(once, hist, today=today) == once                                   # a second pass adds nothing
    written = "The value is $300,000. The home last sold on Jun 7, 2024 for $331,700; what has changed since then should be confirmed with the seller."
    assert fc.history_sentence(written, hist, today=today) == written                             # the writer already said it
    other = "The value is $300,000. A neighbour's home sold for $331,700."                         # same figure, not this home's sale
    assert "This home last sold on Jun 7, 2024" in fc.history_sentence(other, hist, today=today)


def test_cutting_an_engine_clause_never_leaves_a_lead_in_with_nothing_after_it():
    """811 Ridley Creek Dr, comp #3 (11/15): "...Given the distance and the different setting." closed the note."""
    note = ("It has a much smaller lot than the subject's 0.64 acres. Given the distance and the different setting, "
            "it carries a lower weight in the valuation.")
    out = ai._drop_engine_clauses(note)
    assert out == "It has a much smaller lot than the subject's 0.64 acres."
    # a real sentence before the engine clause is still kept
    kept = ai._drop_engine_clauses("It sold in February 2026 after 113 days on the market, which is reflected in its reduced location weight.")
    assert kept == "It sold in February 2026 after 113 days on the market."
    # and a lead-in WITH its main clause is a sentence
    full = ai._drop_engine_clauses("Given the distance, it is the weakest match of the five, which is reflected in its reduced location weight.")
    assert full == "Given the distance, it is the weakest match of the five."


# ---- a rating the AI analysis left blank is a suggestion, shown with the AI's own read (owner 11/15) ---------------

def test_the_ais_own_read_of_each_rating_is_kept_for_the_agents_screen():
    """811 Ridley Creek Dr (11/15): "Finish rating each comp: #3: Layout" stopped a finished report. The agent now
    sees what the AI itself rated a row the record checks left blank."""
    read = ai._ai_quality_read({"layout": "similar", "lot": "above", "amenities": "great", "interior": None, "junk": "below"})
    assert read == {"layout": "similar", "lot": "above"}          # valid ratings only, known rows only
    assert ai._ai_quality_read({}) is None and ai._ai_quality_read(None) is None and ai._ai_quality_read("similar") is None


def test_the_ais_read_is_saved_with_the_comp_but_never_sent_to_a_client():
    from app.schemas.cma import CmaComparable
    from app.services.cma_service import client_comparables
    comp = CmaComparable(street_address="1 Main St", quality={"finish_quality": "similar"}, ai_quality_read={"layout": "similar"})
    saved = comp.model_dump()
    assert saved["ai_quality_read"] == {"layout": "similar"}       # survives a save from the builder
    assert CmaComparable(street_address="2 Main St").ai_quality_read is None
    out = client_comparables([saved, {"street_address": "3 Main St"}])
    assert "ai_quality_read" not in out[0] and out[0]["quality"]["finish_quality"] == "similar"
    assert out[1] == {"street_address": "3 Main St"} and client_comparables(None) == []


# ---- live round 8 (11/15): the value step answered with numbers and no explanation --------------------------------

_HOLLOW = {  # what 21 Cordele Rd's value step really returned: 432 tokens, every explanation empty
    "final_fmv": 382965, "range_low": 367000, "range_high": 398000, "confidence": "medium",
    "independent_preliminary_fmv": 381000, "independent_preliminary_range_low": 366000,
    "independent_preliminary_range_high": 396000, "independent_preliminary_confidence": "medium",
    "anchor_assessment": {"classification": "reasonable", "reason": "Matches my independent read."},
    "v10_assessment": {"classification": "too_low", "reason": "It deducts for condition twice."},
    "comp_roles": [], "primary_comps": [], "floor_comps": [], "ceiling_comps": [],
    "major_value_drivers": [], "major_value_drags": [], "final_valuation_summary": "",
    "market_exposure_assessment": "", "why_final_differs_from_anchor": "", "why_final_differs_from_v10": "",
}
_FULL = {**_HOLLOW, "final_valuation_summary": "Two sales on the same street set the value.",
         "comp_roles": [{"comp_id": "c1", "role": "direct_analog", "reason": "Same street."}]}
_PACKET = {"comparables": [{"comp_id": "c1"}]}


def test_numbers_with_no_explanation_is_a_thin_answer():
    assert ai._reasoning_answer_is_thin(_HOLLOW, _PACKET)
    assert not ai._reasoning_answer_is_thin(_FULL, _PACKET)
    assert ai._reasoning_answer_is_thin({**_FULL, "final_valuation_summary": "  "}, _PACKET)   # no client paragraph
    assert ai._reasoning_answer_is_thin({**_FULL, "comp_roles": []}, _PACKET)                  # comps, none classified
    assert not ai._reasoning_answer_is_thin({**_FULL, "comp_roles": []}, {"comparables": []})  # nothing to classify
    assert not ai._reasoning_answer_is_thin(None, _PACKET)


def _fake_value_step(monkeypatch, answers):
    """Stand in for the provider: each call returns the next of `answers` (a dict, or an Exception to raise)."""
    import asyncio, json
    from types import SimpleNamespace as NS
    asked = []

    async def through(client, kwargs):
        asked.append(kwargs["messages"][0]["content"])
        a = answers[min(len(asked) - 1, len(answers) - 1)]
        if isinstance(a, Exception):
            raise a
        return NS(stop_reason="end_turn", usage=NS(input_tokens=1000, output_tokens=400),
                  content=[NS(type="text", text=json.dumps(a))])

    monkeypatch.setattr(ai, "_provider_client", lambda: object())
    monkeypatch.setattr(ai._cma_claude, "through_the_queue", through)
    monkeypatch.setattr(ai, "_REASONING_RETRY_BACKOFF_S", (0, 0, 0))
    run = lambda: asyncio.run(ai._call_cma_reasoning_claude({"cma_reasoning_model": "claude-fable-5-2"}, _PACKET))  # noqa: E731
    return run, asked


def test_a_thin_answer_is_asked_for_once_more_and_the_full_one_is_used(monkeypatch):
    run, asked = _fake_value_step(monkeypatch, [_HOLLOW, _FULL])
    raw, usage = run()
    assert raw == _FULL and len(asked) == 2
    assert "left the explanation empty" in asked[1] and "left the explanation empty" not in asked[0]
    assert usage["input_tokens"] == 2000                      # both asks are counted for the bill


def test_a_second_thin_answer_is_not_asked_a_third_time(monkeypatch):
    run, asked = _fake_value_step(monkeypatch, [_HOLLOW, _HOLLOW, _FULL])
    raw, _ = run()
    assert raw == _HOLLOW and len(asked) == 2                 # one extra ask, never a loop


def test_the_first_answer_stands_when_the_extra_ask_fails(monkeypatch):
    run, asked = _fake_value_step(monkeypatch, [_HOLLOW, RuntimeError("provider down")])
    raw, _ = run()
    assert raw == _HOLLOW and len(asked) == 2


def test_a_full_answer_is_never_asked_twice(monkeypatch):
    run, asked = _fake_value_step(monkeypatch, [_FULL])
    assert run()[0] == _FULL and len(asked) == 1


def test_a_thin_answer_is_never_stored_and_a_stored_one_is_never_reused():
    import inspect
    body = inspect.getsource(ai._run_cma_reasoning_stage)
    assert "_reasoning_answer_is_thin(cached.get(\"raw\"), evidence_packet)" in body      # stored thin answer = ask again
    assert "if cached is None and not blockers and not _thin:" in body                    # never written to the shared store
    assert "if not blockers and not _thin and (cached is None or cached is not _locked):" in body


# ---- live round 8 (11/15): four price cuts in our own log, and the report said "without a price change" -----------

from app.services import cma_fact_check as fc   # noqa: E402

_SPRUCE = [  # price_history_service.get_history shape, newest first (451 Spruce Dr, Exton - the real log)
    {"date": "2026-11-13", "event": "Price reduced", "price": 649000, "change": -1000},
    {"date": "2026-11-11", "event": "Price reduced", "price": 650000, "change": -13000},
    {"date": "2026-11-04", "event": "Price reduced", "price": 663000, "change": -12000},
    {"date": "2026-10-30", "event": "Price reduced", "price": 675000, "change": -10000},
    {"date": "2026-10-22", "event": "Listed for sale", "price": 685000},
]


def _hist(events, listed="2026-10-22"):
    from datetime import date
    return fc.summarize_history(events, current_list_date=listed, today=date(2026, 11, 15), current_days=24)


def test_every_price_cut_on_the_current_listing_is_read_from_our_log():
    pc = _hist(_SPRUCE)["current_price_changes"]
    assert pc["known"] and pc["first_price"] == 685000 and pc["reductions"] == 4 and pc["increases"] == 0
    assert pc["total_change"] == -36000 and pc["total_change_pct"] == -5.3
    assert pc["steps"][0] == {"date": "2026-10-30", "to": 675000, "from": 685000}
    assert pc["lines"][0] == "This listing: listed Oct 22, 2026 at $685,000"
    assert pc["lines"][1] == "Price reduced Oct 30, 2026, from $685,000 to $675,000"
    assert pc["lines"][-1] == "In all: 4 reductions, now $36,000 (5.3%) below the first asking price"
    assert "Never say the price has not changed" in pc["prompt_note"]


def test_a_listing_we_watched_from_day_one_with_no_cut_may_say_so():
    pc = _hist([{"date": "2026-10-22", "event": "Listed for sale", "price": 685000}])["current_price_changes"]
    assert pc["known"] and pc["steps"] == [] and pc["lines"] == []          # nothing for the record box
    assert pc["prompt_note"].startswith("No price change since it was listed")


def test_a_price_change_we_cannot_size_is_never_called_no_change():
    # Bright says the price changed (its timestamp) but our log has no amounts: the first price is unknown too
    pc = _hist([{"date": "2026-11-02", "event": "Price changed", "price": 640000},
                {"date": "2026-10-22", "event": "Listed for sale", "price": None}])["current_price_changes"]
    assert not pc["known"] and pc["first_price"] is None and pc["steps"] == []
    assert pc["lines"] == ["The MLS shows a price change on this listing whose earlier price is not in our MLS feed"]
    assert "Never say the price has not changed" in pc["prompt_note"]


def test_a_sold_or_ended_listing_has_no_current_price_changes():
    ended = [{"date": "2026-11-10", "event": "Sold", "price": 640000}, *_SPRUCE[1:]]
    assert _hist(ended)["current_price_changes"] is None


def test_a_sentence_saying_the_price_has_not_changed_is_removed_when_it_has():
    subject = {"_listing_history": _hist(_SPRUCE)}
    said = ("The sales support a value below the asking price. This home has been on the market longer than some of the "
            "comparable sales without a price change, which gives a buyer room to negotiate. Keep an inspection contingency.")
    out, flags = fc.fix_no_price_change_claims(said, subject)
    assert "without a price change" not in out and out.startswith("The sales support") and out.endswith("contingency.")
    assert flags and "4 price changes" in flags[0]
    for wrong in ("The seller has not reduced the price since listing.", "The asking price remains unchanged after 24 days.",
                  "There have been no price reductions so far.", "The seller is still at the original asking price."):
        assert fc.fix_no_price_change_claims(wrong, subject)[0] == "", wrong
    for fine in ("No price reduction is needed yet.", "Comp #2 sold in 9 days without a price change.",
                 "The price was reduced four times, most recently on Nov 13."):
        assert fc.fix_no_price_change_claims(fine, subject) == (fine, []), fine


def test_the_same_sentence_is_left_alone_when_the_log_shows_no_change():
    subject = {"_listing_history": _hist([{"date": "2026-10-22", "event": "Listed for sale", "price": 685000}])}
    said = "It has been listed 24 days without a price change."
    assert fc.fix_no_price_change_claims(said, subject) == (said, [])
    assert fc.fix_no_price_change_claims(said, {}) == (said, [])
    assert fc.fix_text(said, subject={"_listing_history": _hist(_SPRUCE)})[0] == ""      # wired into the fact gate


def test_the_price_cuts_reach_the_engine_the_models_and_the_record_box(monkeypatch):
    import asyncio
    from app.services import price_history_service

    async def history(db, key):
        return list(_SPRUCE)
    monkeypatch.setattr(price_history_service, "get_history", history)
    monkeypatch.setattr(ai._tmach, "today", lambda: __import__("datetime").date(2026, 11, 15))
    subject = {"list_price": 649000, "_status_timeline": {"is_active": True, "list_date": "2026-10-22", "active_market_days": 24,
                                                          "current_list_price": 649000, "summary_lines": ["Current status: Active"]}}
    out = asyncio.run(ai._attach_listing_history(None, subject, "805521624708"))
    tl = out["_status_timeline"]
    assert tl["original_list_price"] == 685000 and tl["price_reduction_pct"] == 5.3 and tl["previous_list_price"] == 650000
    assert [e["to_price"] for e in tl["history_events"] if e["event"] == "price_decrease"] == [675000, 663000, 650000, 649000]
    block = " ".join(tl["summary_lines"])
    assert "Price changes on this listing" in block and "from $685,000 to $675,000" in block and "Never say the price has not changed" in block
    # the exposure step now knows the first asking price (it only ever saw today's)
    exp = ai._compute_exposure(out, tl, 20, {"anchor_value": 700000, "anchor_high": 720000})
    assert exp["original_list_price"] == 685000 and exp["price_cut_pct"] == 5.3 and exp["price_reduced"]
    # ... and the report's "This home's record" box lists them, kept through the final rebuild
    kept = ai._subject_runtime_facts(out)
    lines = ai._property_record_facts(kept)["history_lines"]
    assert lines[0] == "This listing: listed Oct 22, 2026 at $685,000" and lines[-1].startswith("In all: 4 reductions")


def test_a_home_that_is_not_for_sale_gets_no_current_price_changes(monkeypatch):
    import asyncio
    from app.services import price_history_service

    async def history(db, key):
        return list(_SPRUCE)
    monkeypatch.setattr(price_history_service, "get_history", history)
    monkeypatch.setattr(ai._tmach, "today", lambda: __import__("datetime").date(2026, 11, 15))
    subject = {"list_price": 649000, "_status_timeline": {"is_active": False, "is_closed": True, "list_date": "2026-10-22"}}
    out = asyncio.run(ai._attach_listing_history(None, subject, "805521624708"))
    assert "price_changes" not in (out.get("_status_timeline") or {})
    assert not (out.get("_listing_history") or {}).get("current_price_changes")


# ---- live round 8 (11/15): a fee gap between two homes in ONE community is a record gap, not a cheaper home --------

def test_a_fee_gap_inside_one_community_is_not_called_a_cheaper_home():
    subject = {"street_address": "3910 ASHLAND DR #215", "zipcode": "19438", "subdivision_name": "BILTMORE ESTATES",
               "city": "HARLEYSVILLE", "condo_fee": 359, "condo_fee_frequency": "Monthly", "home_type": "CONDO"}
    comp = {"street_address": "3930 ASHLAND DR #238", "zipcode": "19438", "subdivision_name": "BILTMORE ESTATES",
            "city": "HARLEYSVILLE", "condo_fee": 227, "condo_fee_frequency": "Monthly", "home_type": "CONDO",
            "ai_commentary": "Same plan, one building over.", "commentary": "Same plan, one building over."}
    flags = ai._append_fee_gap_notes(subject, [comp])
    said = comp["commentary"]
    assert len(flags) == 1 and "monthly fee is $227 on its MLS record, against $359 for this home" in said
    assert "same community" in said and "confirm the current fee with the association" in said
    assert "cheaper to own" not in said and "overstate" not in said
    # a different community keeps the plain comparison
    far = {**comp, "street_address": "12 OAK CT #4", "subdivision_name": "WALNUT MEADOWS",
           "ai_commentary": "A similar flat a mile away.", "commentary": "A similar flat a mile away."}
    ai._append_fee_gap_notes(subject, [far])
    assert "cheaper to own, so this sale may overstate" in far["commentary"]


# ---- live round 8 (11/15): a home under contract that points OUTSIDE the range was called a reason for the range ---

def test_a_pending_home_outside_the_range_is_called_a_caution_not_the_reason_for_the_range():
    final = {"value": 383000, "range_min": 367000, "range_max": 398000}
    home = {"street_address": "16 ANDRIES RD", "list_price": 370000, "implied_value": 333000, "distance_miles": 0.08,
            "days_on_market": 17, "match_score": 80, "moved_value": False}
    out, flags = ai._v10_pending_context_sentence("The value is $383,000.", final=final,
                                                  trace={"pending_competition": {"pendings_used": [home]}})
    assert flags and "implies about $333,000, about 13% below this report's value" in out
    assert "does not change the value or the range. It points below the supported range" in out
    assert "caution on the low side" in out and "as wide as it is" not in out
    high, _ = ai._v10_pending_context_sentence("The value is $383,000.", final=final, trace={"pending_competition": {
        "pendings_used": [{**home, "implied_value": 420000}]}})
    assert "It points above the supported range" in high and "caution on the high side" in high
    inside, _ = ai._v10_pending_context_sentence("The value is $383,000.", final=final, trace={"pending_competition": {
        "pendings_used": [{**home, "implied_value": 380000}]}})
    assert "sits inside the supported range and backs it" in inside


def test_the_listed_line_shows_the_first_asking_price_not_todays(monkeypatch):
    import asyncio
    from app.services import price_history_service

    async def history(db, key):
        return [{"date": "2026-10-15", "event": "Price reduced", "price": 425000, "change": -25000},
                {"date": "2026-09-25", "event": "Listed for sale", "price": 450000}]
    monkeypatch.setattr(price_history_service, "get_history", history)
    monkeypatch.setattr(ai._tmach, "today", lambda: __import__("datetime").date(2026, 11, 15))
    tl = {"is_active": True, "list_date": "2026-09-25", "active_market_days": 52, "current_list_price": 425000,
          "summary_lines": ["Current status: Active", "Listing history:", "  - Listed  2026-09-25  $425,000"],
          "history_events": [{"event": "listed", "source": "mls", "date": "2026-09-25", "price": 425000}]}
    out = asyncio.run(ai._attach_listing_history(None, {"list_price": 425000, "_status_timeline": tl}, "k"))["_status_timeline"]
    assert "  - Listed  2026-09-25  $450,000" in out["summary_lines"] and "  - Listed  2026-09-25  $425,000" not in out["summary_lines"]
    assert out["history_events"][0]["price"] == 450000 and out["history_events"][-1]["to_price"] == 425000
    assert out["original_list_price"] == 450000 and out["price_reduction_pct"] == 5.6


def test_an_earlier_asking_price_is_a_known_figure_not_an_unsupported_one():
    lines = fc.current_price_changes.__globals__["summarize_history"](
        _SPRUCE, current_list_date="2026-10-22", today=__import__("datetime").date(2026, 11, 15))["current_price_changes"]["lines"]
    packet = {"comparables": [{"comp_id": "c1", "sale_price": 700000}],
              "subject": {"current_list_price": 649000, "price_changes_this_listing": lines},
              "deterministic_evidence": {"deterministic_anchor": 703000}}
    raw = {**_FULL, "final_fmv": 640000, "range_low": 610000, "range_high": 700000, "independent_preliminary_fmv": 640000,
           "independent_preliminary_range_low": 610000, "independent_preliminary_range_high": 700000,
           "market_exposure_assessment": "The seller started at $685,000 and has cut to $675,000, $663,000 and $650,000 without a buyer."}
    _, blockers = ai._validate_reasoning_valuation(raw, packet)
    assert not [b for b in blockers if "unsupported dollar figure" in b], blockers
    # without the price-change lines the same sentence is (rightly) stopped
    _, blockers = ai._validate_reasoning_valuation(raw, {**packet, "subject": {"current_list_price": 649000}})
    assert [b for b in blockers if "unsupported dollar figure" in b]
    # the client-text number check reads the valuation record: every step, each cut's size and the total are in it
    tl = {"is_active": True, "active_market_days": 24, "original_list_price": 685000, "history_events": [
        {"event": "price_decrease", "from_price": 663000, "to_price": 650000, "change_amount": 13000}]}
    exp = ai._compute_exposure({"list_price": 649000}, tl, 20, {"anchor_value": 700000, "anchor_high": 720000})
    known = ai._structured_currency_values({"exposure": exp})
    assert {685000.0, 663000.0, 650000.0, 13000.0, 36000.0} <= known


# ---- photos sorted by room before the photo pass (owner 11/15) -----------------------------------------------------
# The old pick (first 12 + last 2) saw a bathroom photo in 3 of 17 test homes and no kitchen in 6 of them.

def _gallery(rooms):
    """rooms: one room name per photo, in gallery order -> (urls, labels as the sorter returns them)."""
    urls = [f"https://img.example/p{i}.jpg" for i in range(1, len(rooms) + 1)]
    labels = [{"n": i, "room": r.rstrip("*"), "shows_finishes": r.endswith("*")} for i, r in enumerate(rooms, 1)]
    return urls, labels


_FOULK = (["exterior_front"] * 5 + ["exterior_back_or_yard"] * 5 + ["living_or_family_room"] * 2          # what the old pick saw
          + ["kitchen*"] * 5 + ["bathroom*"] * 4 + ["bedroom"] * 5 + ["basement_or_lower_level"] * 4 + ["dining_room"]
          + ["garage_or_utility"] * 2 + ["other"] * 5 + ["floor_plan_or_map", "community_or_view", "exterior_back_or_yard"])


def test_the_pick_takes_the_front_kitchens_and_baths_first():
    urls, labels = _gallery(_FOULK)
    pick = ai._pick_photos_by_room(urls, labels, 14)
    room = {urls[r["n"] - 1]: r["room"] for r in labels}
    got = [room[u] for u in pick]
    assert len(pick) == 14 and pick == sorted(pick, key=urls.index)            # gallery order, no repeats
    assert got.count("kitchen") == 3 and got.count("bathroom") == 3
    assert got.count("exterior_front") == 1 and pick[0] == urls[0]              # the front of the home, always
    assert got.count("living_or_family_room") == 2 and "exterior_back_or_yard" in got and "basement_or_lower_level" in got
    assert got.count("other") == 1                                               # a sunroom / bonus room is not left out
    assert "floor_plan_or_map" not in got and "community_or_view" not in got
    # the old pick, for the same gallery: no kitchen, no bath
    old = [room[u] for u in ai._photo_urls({"photos": urls}, 14)]
    assert "kitchen" not in old and "bathroom" not in old
    assert ai._photo_urls({"photos": urls, "_photo_pick": pick}, 14) == pick      # the report uses the sorted pick


def test_the_lead_photo_stays_when_nothing_was_labelled_as_the_front():
    urls, labels = _gallery(["other"] + ["kitchen*"] * 4 + ["bathroom*"] * 4 + ["bedroom"] * 8 + ["living_or_family_room"] * 3)
    pick = ai._pick_photos_by_room(urls, labels, 14)
    assert pick[0] == urls[0] and len(pick) == 14


def test_a_gallery_with_few_kinds_of_room_is_still_filled_to_the_limit():
    urls, labels = _gallery(["exterior_front"] + ["bedroom"] * 12 + ["kitchen*"] * 2 + ["floor_plan_or_map"] * 5)
    pick = ai._pick_photos_by_room(urls, labels, 14)
    rooms = [labels[urls.index(u)]["room"] for u in pick]
    assert len(pick) == 14 and rooms.count("kitchen") == 2 and rooms.count("floor_plan_or_map") == 0


def test_labels_that_cannot_be_trusted_keep_the_old_pick():
    urls, labels = _gallery(_FOULK)
    assert ai._pick_photos_by_room(urls, labels[:10], 14) is None                 # fewer than half the photos labelled
    assert ai._pick_photos_by_room(urls, None, 14) is None and ai._pick_photos_by_room(urls, "junk", 14) is None
    assert ai._pick_photos_by_room(urls[:10], labels[:10], 14) is None            # a short gallery is sent whole anyway
    junk = [{"n": "x", "room": "kitchen"}, {"n": 999, "room": "kitchen"}, "no", *labels]
    assert ai._pick_photos_by_room(urls, junk, 14) == ai._pick_photos_by_room(urls, labels, 14)
    odd = [{**r, "room": "sunroom"} if r["n"] == 20 else r for r in labels]         # a room name outside the list = "other"
    assert len(ai._pick_photos_by_room(urls, odd, 14)) == 14
    # a pick naming photos that are no longer in the gallery is ignored
    assert ai._photo_urls({"photos": urls, "_photo_pick": ["https://img.example/gone.jpg"]}, 14) == urls[:12] + urls[-2:]


def _sorter_world(monkeypatch, label_fn):
    import asyncio
    store, booked, calls = {}, [], []

    async def get(key):
        return store.get(key)

    async def put(key, labels):
        store[key] = labels

    async def label(client, http, gate, urls):
        calls.append(len(urls))
        return label_fn(urls)

    async def record(db, owner_id, _conv, model, tin, tout, cost, *a, **k):
        booked.append((model, tin, tout, round(cost, 6), k.get("event_type")))

    async def add_spend(db, buckets, cost):
        booked.append(("spend", tuple(buckets), round(cost, 6)))

    monkeypatch.setattr(ai, "_photo_rooms_get", get)
    monkeypatch.setattr(ai, "_photo_rooms_put", put)
    monkeypatch.setattr(ai, "_label_gallery", label)
    monkeypatch.setattr(ai, "record_usage_event", record)
    monkeypatch.setattr(ai, "_get_client", lambda: object())
    from app.services import usage_budget_service
    monkeypatch.setattr(usage_budget_service, "add_spend", add_spend)
    monkeypatch.delenv("CMA_PHOTO_SORT", raising=False)
    config = {"model_pricing": {"claude-haiku-5-5": {"input_per_mtok": 0.10, "output_per_mtok": 0.50}}}
    run = lambda s, c: asyncio.run(ai._sort_report_photos(None, "owner", config, s, c, 14))  # noqa: E731
    return run, store, booked, calls


def test_long_galleries_are_sorted_once_and_the_labels_are_kept(monkeypatch):
    urls, labels = _gallery(_FOULK)
    run, store, booked, calls = _sorter_world(monkeypatch, lambda u: (labels[:len(u)], {"input_tokens": 7000, "output_tokens": 1000}))
    subject, short, long_comp = {"photos": urls}, {"photos": urls[:9]}, {"photos": list(reversed(urls))}
    cost = run(subject, [short, long_comp])
    # a gallery under the limit is labelled too (for the viewer's room buttons) but is still sent whole, never re-picked
    assert sorted(calls) == [9, 41, 41] and "_photo_pick" not in short
    assert len(subject["_photo_pick"]) == 14 and len(long_comp["_photo_pick"]) == 14
    assert round(cost, 4) == 0.0036                                                # 21,000 in + 3,000 out on the small model
    assert booked[0] == ("claude-haiku-5-5", 21000, 3000, 0.0036, "cma_ai") and booked[1][0] == "spend"
    # every home carries which room each photo shows; the short one also knows what the AI was shown of it (all of it)
    assert short["_photo_room_by_url"][urls[0]] == labels[0]["room"] and len(short["_photo_room_by_url"]) == 9
    assert short["_photo_rooms_sent"] == sorted({r["room"] for r in labels[:9]})
    assert set(subject["_photo_room_by_url"]) == set(urls)
    # the same homes in another report: no model call, no charge
    calls.clear(); booked.clear()
    again = {"photos": urls}
    assert run(again, []) == 0.0 and calls == [] and booked == [] and again["_photo_pick"] == subject["_photo_pick"]


def test_a_home_the_sorter_cannot_read_keeps_the_old_pick_and_never_fails_the_report(monkeypatch):
    urls, labels = _gallery(_FOULK)

    def flaky(u):
        if u[0].endswith("p41.jpg"):
            raise RuntimeError("provider down")
        return None, {"input_tokens": 500, "output_tokens": 10}                    # answered, but with nothing usable
    run, store, booked, calls = _sorter_world(monkeypatch, flaky)
    a, b = {"photos": urls}, {"photos": list(reversed(urls))}
    cost = run(a, [b])
    assert "_photo_pick" not in a and "_photo_pick" not in b and store == {}       # nothing stored, old pick stands
    assert cost > 0 and booked[0][1] == 500                                        # the one answered call is still paid for
    assert ai._photo_urls(a, 14) == urls[:12] + urls[-2:]


def test_the_sorter_can_be_switched_off_and_never_runs_in_a_dry_run(monkeypatch):
    urls, labels = _gallery(_FOULK)
    run, store, booked, calls = _sorter_world(monkeypatch, lambda u: (labels, {"input_tokens": 1, "output_tokens": 1}))
    monkeypatch.setenv("CMA_PHOTO_SORT", "0")
    s = {"photos": urls}
    assert run(s, []) == 0.0 and calls == [] and "_photo_pick" not in s
    monkeypatch.delenv("CMA_PHOTO_SORT")
    from types import SimpleNamespace as NS
    monkeypatch.setattr(ai._tmach, "current", lambda: NS(fake_client=object()))
    assert run(s, []) == 0.0 and calls == []


# ---- a short client-facing paragraph on the current competition (owner 11/15) --------------------------------------

_CSUBJ = {"street_address": "21 CORDELE RD", "list_price": 379999, "beds": 4, "baths": 2.5, "sqft": 1900, "year_built": 1968}
_CHOMES = [
    {"listing_key": "a", "status": "pending", "street_address": "16 ANDRIES RD", "list_price": 370000, "price_per_sqft": 172,
     "beds": 4, "baths": 2.5, "sqft": 2150, "days_on_market": 17, "distance_miles": 0.08, "match_score": 84},
    {"listing_key": "b", "status": "active", "street_address": "9 DARWIN DR", "list_price": 395000, "price_per_sqft": 201,
     "beds": 4, "baths": 2, "sqft": 1965, "days_on_market": 6, "distance_miles": 0.3, "match_score": 78, "price_cuts": [{"x": 1}]},
]
_CGOOD = ("Two similar homes are part of this picture right now. 9 Darwin Dr is for sale at $395,000, a little above this home's "
          "$379,999 asking price, after 6 days on the market. 16 Andries Rd went under contract in 17 days at a $370,000 asking "
          "price, which shows buyers are moving quickly on homes like this one.")


def test_the_competition_facts_are_the_cards_and_nothing_else():
    f = ai._competition_facts(_CSUBJ, _CHOMES, "buyer", {"active_market_days": 7})
    assert f["audience"] == "buyer" and f["for_sale_count"] == 1 and f["under_contract_count"] == 1
    assert f["subject"]["asking_price"] == "$379,999" and f["subject"]["asking_price_per_sqft"] == "$200" and f["subject"]["days_on_market"] == 7
    first = f["similar_homes"][0]                                            # closest match first
    assert first["address"] == "16 Andries Rd" and first["status"] == "under contract" and first["asking_price"] == "$370,000"
    assert f["similar_homes"][1]["price_reductions"] == 1 and "photos" not in first and "public_remarks" not in first
    off = ai._competition_facts(_CSUBJ, _CHOMES, "seller", {"not_listed": True})
    assert off["audience"] == "seller" and off["subject"]["asking_price"] == "not currently listed" and "days_on_market" not in off["subject"]


def test_a_competition_paragraph_is_shown_only_when_every_price_and_address_is_on_a_card():
    f = ai._competition_facts(_CSUBJ, _CHOMES, "buyer", {"active_market_days": 7})
    assert ai._competition_note_checked(_CGOOD, f) == _CGOOD
    # 11/20: a sentence with a price or an address on no card is cut - no longer the whole paragraph (it had left
    # "What the competition tells us" empty on 10 of 12 live reports). The invented figure still never shows.
    out = ai._competition_note_checked(_CGOOD.replace("$395,000", "$359,000"), f)                 # a price on no card
    assert out is None or "$359,000" not in out
    out = ai._competition_note_checked(_CGOOD.replace("9 Darwin Dr", "12 Hillcroft Rd"), f)        # an address on no card
    assert out is None or "12 Hillcroft Rd" not in out
    assert ai._competition_note_checked(_CGOOD + " This home is worth about $383,000.", f) == _CGOOD  # a value: not its job
    assert ai._competition_note_checked("- Two homes.", f) is None and ai._competition_note_checked("", f) is None
    # nobody looked at photos: a sentence about condition is cut, the rest stays
    cut = ai._competition_note_checked(_CGOOD + " 9 Darwin Dr has a renovated kitchen and updated baths.", f)
    assert cut == _CGOOD
    # a contract price is not public: "sold for" about a home under contract is cut
    sold = ai._competition_note_checked(_CGOOD + " 16 Andries Rd sold for $370,000 last month.", f)
    assert sold == _CGOOD
    assert ai._competition_note_checked("9 Darwin Dr has a renovated kitchen.", f) is None                 # nothing left worth showing


def test_the_competition_paragraph_is_one_small_call_and_never_fails_a_report(monkeypatch):
    import asyncio
    from types import SimpleNamespace as NS
    asked, booked = [], []

    async def create(client, **kw):
        asked.append(kw)
        if kw["model"] == "boom":
            raise RuntimeError("provider down")
        return NS(content=[NS(type="text", text=_CGOOD)], usage=NS(input_tokens=900, output_tokens=200))

    monkeypatch.setattr(ai._cma_claude, "create", create)
    monkeypatch.setattr(ai, "_get_client", lambda: object())
    monkeypatch.setattr(ai, "_note_extra_usage", lambda resp, model, label: booked.append(label))
    monkeypatch.delenv("CMA_COMPETITION_NOTE", raising=False)
    run = lambda homes, model="claude-sonnet-5-5": asyncio.run(  # noqa: E731
        ai._competition_commentary(_CSUBJ, homes, "buyer", {"active_market_days": 7}, model=model))
    assert run(_CHOMES) == _CGOOD and booked == ["competition_commentary"] and len(asked) == 1
    sent = asked[0]["messages"][0]["content"]
    assert "16 Andries Rd" in sent and "photo" not in sent and asked[0]["effort"] == "low"
    assert "Never describe their condition" in asked[0]["system"] and "contract price is not public" in asked[0]["system"]
    assert run([]) is None and run(None) is None and len(asked) == 1               # no similar homes: no call
    assert run(_CHOMES, "boom") is None                                            # a failure is just "no paragraph"
    monkeypatch.setenv("CMA_COMPETITION_NOTE", "0")
    assert run(_CHOMES) is None
    monkeypatch.delenv("CMA_COMPETITION_NOTE")
    monkeypatch.setattr(ai._tmach, "current", lambda: NS(fake_client=None))
    assert run(_CHOMES) is None                                                    # never in a past-sale test


def test_a_free_rerun_keeps_the_paragraph_only_while_the_homes_and_prices_are_the_same():
    sig = ai._competition_signature
    assert sig(_CHOMES) == sig(list(reversed(_CHOMES)))
    assert sig(_CHOMES) != sig(_CHOMES[:1]) and sig(_CHOMES) != sig([{**_CHOMES[0], "list_price": 365000}, _CHOMES[1]])
    assert sig(_CHOMES) != sig([{**_CHOMES[0], "status": "active"}, _CHOMES[1]]) and sig(None) == []
    import inspect
    body = inspect.getsource(ai.recompute_valuation)
    assert "_competition_commentary(" not in body and "_competition_signature(_competition)" in body   # the free path never calls the model


def test_a_direct_call_never_goes_through_the_half_price_queue(monkeypatch):
    import asyncio
    from types import SimpleNamespace as NS
    from app.services import cma_claude
    queued, sent = [], []

    async def queue(client, kw, **_):
        queued.append(kw)
        return NS(content=[], usage=NS(input_tokens=1, output_tokens=1), stop_reason="end_turn")

    class Client:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**kw):
                sent.append(kw)
                return NS(content=[], usage=NS(input_tokens=1, output_tokens=1), stop_reason="end_turn")

    monkeypatch.setattr(cma_claude, "through_the_queue", queue)
    asyncio.run(cma_claude.create(Client(), direct=True, model="claude-haiku-5-5", max_tokens=10, messages=[]))
    assert len(sent) == 1 and queued == []
    asyncio.run(cma_claude.create(Client(), model="claude-haiku-5-5", max_tokens=10, messages=[]))
    assert len(queued) == 1 and len(sent) == 1                      # everything else still may
    import inspect
    assert "_cma_claude.create(client, direct=True, model=_PHOTO_SORT_MODEL" in inspect.getsource(ai._label_gallery)


# ---- Kitchen and Bathrooms rating rows: shown and written up, never priced (owner 11/16) ---------------------------

def test_kitchen_and_bathrooms_are_rating_rows_the_ai_fills():
    assert ai._QUALITY_FIELD_VALUES["kitchen"] == {"below", "similar", "above"} == ai._QUALITY_FIELD_VALUES["bathrooms"]
    form = ai._SUBMIT_TOOL["input_schema"]["properties"]["comps"]["items"]["properties"]["quality"]["properties"]
    assert form["kitchen"]["enum"] == ["below", "similar", "above"] and "BOTH kitchens" in form["kitchen"]["description"]
    assert form["bathrooms"]["enum"] == ["below", "similar", "above"] and "BOTH homes" in form["bathrooms"]["description"]
    assert "KITCHEN AND BATHROOMS, RATED ON THEIR OWN" in ai._SYSTEM_PROMPT and "carry no adjustment of their own" in ai._SYSTEM_PROMPT
    assert "never fill it from the listing text alone" in ai._SYSTEM_PROMPT
    assert ai._ai_quality_read({"kitchen": "above", "bathrooms": "below", "interior": "similar"}) == {
        "kitchen": "above", "bathrooms": "below", "interior": "similar"}
    from app.schemas.cma import CmaComparable
    saved = CmaComparable(street_address="1806 BELLEWOOD RD", quality={"kitchen": "above", "bathrooms": "below"}).model_dump()
    assert saved["quality"]["kitchen"] == "above" and saved["quality"]["bathrooms"] == "below"       # survive a save
    assert CmaComparable(street_address="x", quality={"interior": "similar"}).model_dump()["quality"]["kitchen"] is None


def test_the_two_rows_never_reach_the_price():
    q = {"finish_quality": "similar", "interior": "above", "kitchen": "above", "bathrooms": "below", "lot": "below"}
    assert ai._priced_ratings(q) == {"finish_quality": "similar", "interior": "above", "lot": "below"}
    assert ai._priced_ratings(None) == {} and ai._priced_ratings("x") == {}
    assert not set(ai._DESCRIPTIVE_QUALITY_ROWS) & set(ai._V10_ALL_DIMS)                 # not a dimension the engine weighs
    import inspect
    src = inspect.getsource(ai)
    assert '"better_worse_ratings": _priced_ratings(comp.get("quality"))' in src          # the value step's evidence
    assert '"better_worse_ratings": comp.get("quality")' not in src
    # no rule that prices condition names either row
    for line in src.splitlines():
        if '"finish_quality", "interior"' in line and ("kitchen" in line or "bathrooms" in line) and "describe THIS COMP" not in line:
            raise AssertionError(line)


def _room_qa(text, quality):
    comp = {"listing_key": "k1", "street_address": "1806 BELLEWOOD RD", "is_included": True, "strength": "strong", "quality": quality,
            "sale_price": 415000}
    res, _, blocking = ai._v10_pre_export_qa(
        {"methodology_summary": ("Methodology summary", text)}, value=398000, range_pct=0.05, subject_asking=405000,
        status_timeline={"is_active": True, "active_market_days": 25}, comps=[comp], comp_num_by_key={"k1": 1},
        addr_by_key={"k1": "1806 BELLEWOOD RD"}, valuation_trace={"anchor": {"anchor_value": 398000, "comps": []}},
        value_drivers=[], confidence="medium", subject={})
    hits = lambda xs: [x for x in xs if "structured rating" in x or "comparison for Comp #1" in x]  # noqa: E731
    return hits(blocking), hits(res.get("warnings") or [])


def test_a_sentence_about_the_kitchen_answers_to_the_kitchen_row():
    said = "The final fair market value is $398,000. Comp #1 has a more updated kitchen than this home."
    # new kitchen, original baths, finish 'below' overall: "a more updated kitchen" is TRUE - it used to be stopped
    assert _room_qa(said, {"finish_quality": "below", "kitchen": "above", "bathrooms": "below"}) == ([], [])
    # ... and is still stopped when the kitchen row says the opposite
    blocking, _ = _room_qa(said, {"finish_quality": "above", "kitchen": "below"})
    assert blocking and "kitchen" in blocking[0]
    # no kitchen row: the finish rating answers for it, as before
    blocking, _ = _room_qa(said, {"finish_quality": "below"})
    assert blocking and "finish quality" in blocking[0]
    baths = "The final fair market value is $398,000. Comp #1 has more dated baths than this home."
    assert _room_qa(baths, {"finish_quality": "above", "bathrooms": "below"}) == ([], [])


def test_a_kitchen_or_bath_rating_needs_the_room_shown_in_both_homes():
    q = {"kitchen": "above", "bathrooms": "below", "interior": "above"}
    both = ["bathroom", "exterior_front", "kitchen"]
    assert ai._drop_unseen_room_ratings(q, both, both) == q
    # 1014 Foulk Rd under the old photo pick: not one photo of the subject's kitchen or baths was sent
    assert ai._drop_unseen_room_ratings(q, ["exterior_front", "living_or_family_room"], both) == {"interior": "above"}
    assert ai._drop_unseen_room_ratings(q, both, ["kitchen"]) == {"kitchen": "above", "interior": "above"}      # comp has no bath photo
    # a short gallery sent whole was never labelled: nothing is known, the AI's rating stands
    assert ai._drop_unseen_room_ratings(q, None, None) == q and ai._drop_unseen_room_ratings(q, None, both) == q
    assert ai._drop_unseen_room_ratings(None, both, both) == {}
    urls, labels = _gallery(_FOULK)
    pick = ai._pick_photos_by_room(urls, labels, 14)
    seen = ai._rooms_in_pick(urls, labels, pick)
    assert "kitchen" in seen and "bathroom" in seen and "exterior_front" in seen and seen == sorted(seen)
    assert ai._rooms_in_pick(urls, labels, urls[:3]) == ["exterior_front"] and ai._rooms_in_pick(urls, None, pick) == []


# ---- the free "sales now point to" figure for the report editor (owner 11/16) --------------------------------------

def _preview_world(monkeypatch, comps, anchor, *, usable=True, boom=False):
    import asyncio
    from types import SimpleNamespace as NS
    seen = {}
    report = NS(mode="buyer", subject={"street_address": "1 MAIN ST", "list_price": 400000}, comparables=comps,
                pricing={"ai_estimate": {"value": 398000, "valuation_trace": {
                    "deterministic_anchor": 396400, "anchor": {"comps": [{"listing_key": "a"}, {"listing_key": "b"}, {"listing_key": "c"}]}}}})

    async def owned(db, owner_id, report_id):
        return report

    async def enrich(db, subject, cs, *, live_status=True):
        seen["live_status"] = live_status
        if boom:
            raise RuntimeError("map servers down")
        return dict(subject), [dict(c) for c in cs]

    async def proximity(db, subject):
        return {}

    monkeypatch.setattr(ai.cma_service, "_get_owned", owned)
    monkeypatch.setattr(ai, "_enrich_with_property_data", enrich)
    monkeypatch.setattr(ai, "load_local_proximity_context", proximity)
    monkeypatch.setattr(ai, "_usable_sale_comp", lambda s, c: usable)
    monkeypatch.setattr(ai, "_v10_provisional_inputs", lambda s, cs, tl, density_context=None: (anchor, {}))
    return asyncio.run(ai.adjusted_sales_preview(None, "owner", "r1")), seen


_PCOMPS = [{"listing_key": "a", "street_address": "2 OAK ST", "quality": {"interior": "similar"}},
           {"listing_key": "b", "street_address": "3 ELM ST", "quality": {"finish_quality": "above"}},
           {"listing_key": "d", "street_address": "9 NEW RD"},                                  # added from inside the report
           {"listing_key": "c", "street_address": "4 ASH ST", "is_included": False, "quality": {"interior": "below"}}]
_PANCHOR = {"mode": "closed_only", "anchor_value": 411650, "anchor_low": 392100, "anchor_high": 428900,
            "comps": [{"normalized_weight": 0.5}, {"normalized_weight": 0.3}, {"normalized_weight": 0.2}]}


def test_the_free_figure_shows_what_the_sales_point_to_now_and_never_calls_the_mls(monkeypatch):
    out, seen = _preview_world(monkeypatch, _PCOMPS, _PANCHOR)
    assert out["adjusted_sales"] == 412000 and out["low"] == 392000 and out["high"] == 429000 and out["comps_used"] == 3
    assert out["report_adjusted_sales"] == 396400 and out["report_value"] == 398000
    assert out["comps_changed"] is True and out["added"] == ["9 NEW RD"] and out["removed_count"] == 1
    assert out["unrated"] == ["9 NEW RD"] and out["message"] is None
    assert seen["live_status"] is False                                    # no live MLS status call for a preview


def test_the_free_figure_says_so_plainly_when_there_is_nothing_to_show(monkeypatch):
    same = [c for c in _PCOMPS if c["listing_key"] != "d"]
    for c in same:
        c = dict(c)
    out, _ = _preview_world(monkeypatch, [{**c, "is_included": True} for c in same], _PANCHOR)
    assert out["comps_changed"] is False and out["added"] == [] and out["removed_count"] == 0
    out, seen = _preview_world(monkeypatch, _PCOMPS, _PANCHOR, usable=False)
    assert out["adjusted_sales"] is None and "at least 2 sold homes" in out["message"] and "live_status" not in seen
    out, _ = _preview_world(monkeypatch, _PCOMPS, _PANCHOR, boom=True)
    assert out["adjusted_sales"] is None and "Try again" in out["message"]
    out, _ = _preview_world(monkeypatch, _PCOMPS, {"mode": "insufficient", "anchor_value": 300000})
    assert out["adjusted_sales"] is None and "not enough" in out["message"]


def test_the_free_figure_route_makes_no_ai_call():
    import inspect
    body = inspect.getsource(ai.adjusted_sales_preview)
    for paid in ("_cma_claude", "_get_client", "reserve_cma", "_run_cma_reasoning_stage", "record_usage_event"):
        assert paid not in body, paid
    from app.api import cma_routes
    assert "adjusted-sales-preview" in inspect.getsource(cma_routes)


# ---- Interior set from the rooms: Kitchen 40%, Bathrooms 40%, the rest of the interior 20% (owner 11/16) ----

def test_the_two_rooms_together_decide_interior_whatever_the_rest_looks_like():
    f = ai._interior_from_rooms
    assert f("above", "above", "similar") == "above" and f("above", "above", "below") == "above"
    assert f("below", "below", "similar") == "below" and f("below", "below", "above") == "below"
    assert f("similar", "similar", "above") == "similar"        # other rooms alone are 20%: not enough to move it
    assert f("similar", "similar", "below") == "similar"


def test_one_room_alone_never_flips_interior_and_opposite_rooms_cancel():
    f = ai._interior_from_rooms
    assert f("above", "similar", "similar") == "similar"        # 40%
    assert f("above", "similar", "above") == "above"            # 40% + the rest 20%
    assert f("similar", "below", "below") == "below"
    assert f("above", "below", "above") == "similar"            # new kitchen, worse baths (1806 Bellewood)
    assert f("above", "below", "similar") == "similar" and f("below", "above", "below") == "similar"


def test_a_room_the_ai_could_not_compare_hands_its_share_to_the_overall_read():
    f = ai._interior_from_rooms
    assert f("above", None, "above") == "above"                 # 40% + 60%
    assert f("above", "", "similar") == "similar"               # 40% alone
    assert f("above", None, "below") == "similar"               # +40% - 60% = -20%
    assert f(None, "below", "below") == "below"
    assert f(None, None, "above") == "above" and f(None, None, "below") == "below"   # no rows: the AI's own read stands
    assert f(None, None, None) is None and f("", "", "n/a") is None                  # nothing to go on
    assert f("above", "above", None) == "above" and f("above", None, None) == "similar"   # no overall read: neutral


def test_interior_from_the_rooms_is_on_and_can_be_switched_off(monkeypatch):
    import inspect
    monkeypatch.delenv("CMA_INTERIOR_TIE", raising=False)
    assert ai.interior_tie_on() is True
    monkeypatch.setenv("CMA_INTERIOR_TIE", "0")
    assert ai.interior_tie_on() is False
    body = inspect.getsource(ai._generate_ai_analysis_impl)
    tie, fill, read = body.index("if interior_tie_on():"), body.index("for field, allowed in _QUALITY_FIELD_VALUES.items():"), \
        body.index('comp["ai_quality_read"] = _ai_quality_read(ai_quality)')
    assert tie < fill < read                                     # set before the ratings are filled in ...
    assert "not existing_quality.get(field)" in body[fill:read]  # ... which only ever fills a blank (the agent's own Interior stays)
    # ... and the AI's own read is kept as it gave it, not the tied value
    assert "_merge_quality = dict(ai_quality)" in body[:tie] and '_merge_quality["interior"] = _tied' in body[tie:fill]


# ---- Comps are locked once the AI has written the report (owner 11/16) ----

class _LockDb:
    async def commit(self): pass
    async def refresh(self, _): pass


def _lock_world(monkeypatch, ai_done=True):
    import asyncio
    from types import SimpleNamespace
    from app.services import cma_service as cs
    from app.schemas.cma import CmaReportUpdate, CmaComparable
    comps = [CmaComparable(id=k, listing_key="L" + k, street_address=k + " MAIN ST", is_included=k != "c").model_dump() for k in ("a", "b", "c")]
    report = SimpleNamespace(id="r1", status="DRAFT", comparables=comps, ai_analysis={"generated_at": "x"} if ai_done else None,
                             subject={}, pricing={}, market_context={}, improvements=[], presentation={}, client_name="x", mode="seller",
                             use_signature_card=False, generation_status=None, generation_started_at=None)

    async def owned(db, owner, rid): return report
    monkeypatch.setattr(cs, "_get_owned", owned)

    def save(new_comps):
        payload = CmaReportUpdate(comparables=[CmaComparable(**c) for c in new_comps])
        return asyncio.run(cs.update_report(_LockDb(), "o", "r1", payload))
    return cs, report, comps, save


def test_after_the_ai_run_no_comp_can_be_added_taken_off_or_switched(monkeypatch):
    import pytest
    from fastapi import HTTPException
    cs, report, comps, save = _lock_world(monkeypatch)
    new = {**comps[0], "id": "z", "listing_key": "Lz", "street_address": "9 NEW RD"}
    for changed in ([comps[0], comps[2]],                                   # one taken off the list
                    [{**comps[0], "is_included": False}, comps[1], comps[2]],   # one switched off
                    [comps[0], comps[1], {**comps[2], "is_included": True}],    # one that sat out the run switched on
                    comps + [new],                                          # one added
                    [comps[0], new, comps[2]]):                             # one swapped
        with pytest.raises(HTTPException) as e:
            save(changed)
        assert e.value.status_code == 409 and "start a new CMA" in e.value.detail
    assert [c["id"] for c in report.comparables] == ["a", "b", "c"]          # nothing was saved


def test_notes_and_order_stay_editable_after_the_run_but_ratings_and_weight_are_locked(monkeypatch):
    import pytest
    from fastapi import HTTPException
    cs, report, comps, save = _lock_world(monkeypatch)
    report.comparables[1]["quality"] = {"interior": "similar", "kitchen": "above", "exterior": None}
    report.comparables[1]["strength"] = "supporting"
    comps = [dict(c) for c in report.comparables]
    save([{**comps[1], "commentary": "my note"}, comps[0], comps[2]])                       # a note + a new order: saved
    assert report.comparables[0]["commentary"] == "my note" and cs.comps_locked(report) is True
    comps = [dict(c) for c in report.comparables]
    for change in ({"quality": {**comps[0]["quality"], "interior": "above"}},            # a rating changed
                   {"quality": {**comps[0]["quality"], "exterior": "below"}},            # a blank rating filled in
                   {"quality": {"interior": "similar"}},                                 # a rating cleared
                   {"strength": "strong"}):                                              # the weight class changed
        with pytest.raises(HTTPException) as e:
            save([{**comps[0], **change}, comps[1], comps[2]])
        assert e.value.status_code == 409 and "ratings are locked" in e.value.detail
    # the same ratings sent back with blanks spelled differently is an ordinary save
    save([{**comps[0], "quality": {"interior": "similar", "kitchen": "above", "lot": None}, "strength": "supporting"}, comps[1], comps[2]])


def test_ratings_change_freely_before_the_ai_run(monkeypatch):
    cs, report, comps, save = _lock_world(monkeypatch, ai_done=False)
    save([{**comps[0], "quality": {"interior": "above"}, "strength": "strong"}, comps[1], comps[2]])
    assert report.comparables[0]["quality"]["interior"] == "above"


def test_before_the_ai_run_comps_change_freely(monkeypatch):
    cs, report, comps, save = _lock_world(monkeypatch, ai_done=False)
    save([comps[0]])
    assert [c["id"] for c in report.comparables] == ["a"] and cs.comps_locked(report) is False


# ---- The HOA / condo fee is saved on each comp for the comparison table (owner 11/16) ----

def test_each_comps_monthly_fee_is_saved_for_the_table_and_zero_means_none_on_record():
    import inspect
    from app.schemas.cma import CmaComparable
    assert ai._record_monthly_fee({"association_fee": 975, "association_fee_frequency": "Quarterly"}) == 325.0
    assert ai._record_monthly_fee({"association_fee": 200, "association_fee_frequency": "Monthly",
                                   "condo_fee": 300, "condo_fee_frequency": "Monthly"}) == 500.0
    assert ai._record_monthly_fee({}) == 0.0 and ai._record_monthly_fee(None) == 0.0
    assert ai._record_monthly_fee({"association_fee": 500}) == 0.0            # no frequency on record: not shown as a monthly fee
    assert CmaComparable(monthly_fee=325).model_dump()["monthly_fee"] == 325 and CmaComparable().monthly_fee is None
    for fn in (ai._generate_ai_analysis_impl, ai.recompute_valuation):         # the first run and the refresh both save it
        assert 'comp["monthly_fee"] = _record_monthly_fee(_enr)' in inspect.getsource(fn)


# ---- "Sold as-is" is a sale term: it never forces a comp below a subject that is in no better shape (11/16) ----

def _asis_case(subject, comp_extra, quality):
    comp = {"is_included": True, "strength": "supporting", "street_address": "302 ROGERS RD",
            "public_remarks": "Great pool and yard. Property to be sold AS-IS.", "quality": dict(quality), **comp_extra}
    flags = ai._v10_condition_rating_consistency_check(subject, [comp])
    return comp["quality"], flags


def test_an_as_is_comp_is_not_forced_below_a_subject_that_needs_major_rehab(monkeypatch):
    monkeypatch.setattr(ai, "_V10_ASIS_GUARD", True)
    subject = {"property_condition": ["Major Rehab Needed", "Below Average"], "public_remarks": "The interior requires major rehab."}
    q, flags = _asis_case(subject, {"property_condition": ["Average"]}, {"finish_quality": "above", "interior": "above", "kitchen": "above"})
    assert q["finish_quality"] == "above" and q["interior"] == "above" and not flags     # the AI's photo read stands (2992 2nd St)
    q, _ = _asis_case(subject, {}, {"finish_quality": "above", "interior": "above"})      # comp with no condition field: still not forced below
    assert q["finish_quality"] == "above" and q["interior"] == "above"
    q, _ = _asis_case(subject, {}, {"finish_quality": "below", "interior": "below"})      # and a "below" is softened, as before
    assert q["finish_quality"] == "similar" and q["interior"] == "similar"


def test_an_as_is_comp_whose_record_is_no_worse_than_the_subjects_keeps_its_ratings(monkeypatch):
    monkeypatch.setattr(ai, "_V10_ASIS_GUARD", True)
    q, _ = _asis_case({"property_condition": ["Average"]}, {"property_condition": ["Average"]}, {"finish_quality": "similar", "interior": "above"})
    assert q["finish_quality"] == "similar" and q["interior"] == "above"


def test_an_as_is_comp_is_still_rated_below_a_subject_in_better_recorded_shape(monkeypatch):
    monkeypatch.setattr(ai, "_V10_ASIS_GUARD", True)
    for subject, extra in (({"property_condition": ["Very Good"]}, {"property_condition": ["Average"]}),   # comp's record is worse
                           ({"property_condition": ["Average"]}, {}),                                    # comp has no condition field
                           ({"public_remarks": "A lovely home."}, {"property_condition": ["Average"]})):  # subject's condition unknown
        q, flags = _asis_case(subject, extra, {"finish_quality": "above", "interior": "similar"})
        assert q["finish_quality"] == "below" and q["interior"] == "below" and flags, (subject, extra)


def test_the_old_as_is_rule_comes_back_with_the_switch(monkeypatch):
    monkeypatch.setattr(ai, "_V10_ASIS_GUARD", False)
    subject = {"property_condition": ["Major Rehab Needed", "Below Average"]}
    q, _ = _asis_case(subject, {"property_condition": ["Average"]}, {"finish_quality": "above", "interior": "above"})
    assert q["finish_quality"] == "below" and q["interior"] == "below"


# ---- Which room each photo shows is kept on the report, for the viewer's Kitchen / Baths buttons (owner 11/16) ----

def test_the_room_of_each_photo_is_saved_in_gallery_order():
    urls = [f"https://x/p{i}.jpg" for i in range(1, 7)]
    labels = [{"n": 1, "room": "exterior_front"}, {"n": 2, "room": "kitchen"}, {"n": 3, "room": "kitchen"},
              {"n": 5, "room": "bathroom"}, {"n": 9, "room": "bedroom"}, {"n": 6, "room": "not a room"}, "junk"]
    by_url = ai._photo_room_by_url(urls, labels)
    assert by_url == {urls[0]: "exterior_front", urls[1]: "kitchen", urls[2]: "kitchen", urls[4]: "bathroom"}
    # the saved list lines up with the report's own photo list - a photo the sorter never saw is None
    assert ai._photo_rooms_for(urls + ["https://x/p61.jpg"], by_url) == ["exterior_front", "kitchen", "kitchen", None, "bathroom", None, None]
    assert ai._photo_rooms_for(["https://other/a.jpg"], by_url) is None and ai._photo_rooms_for(urls, None) is None


def test_labels_for_too_few_photos_are_not_saved():
    urls = [f"https://x/p{i}.jpg" for i in range(1, 11)]
    assert ai._photo_room_by_url(urls, [{"n": 1, "room": "kitchen"}, {"n": 2, "room": "bathroom"}]) is None   # 2 of 10
    assert ai._photo_room_by_url(urls, None) is None and ai._photo_room_by_url([], [{"n": 1, "room": "kitchen"}]) is None


def test_short_gallery_labelling_can_be_switched_off(monkeypatch):
    urls, labels = _gallery(_FOULK)
    run, store, booked, calls = _sorter_world(monkeypatch, lambda u: (labels[:len(u)], {"input_tokens": 100, "output_tokens": 10}))
    monkeypatch.setattr(ai, "_PHOTO_SORT_LABEL_SHORT", False)
    short = {"photos": urls[:9]}
    assert run({"photos": urls[:5]}, [short]) == 0.0 and calls == [] and "_photo_room_by_url" not in short


def test_the_report_keeps_the_rooms_on_the_subject_and_on_every_comp():
    import inspect
    from app.schemas.cma import CmaComparable, CmaSubjectProperty
    body = inspect.getsource(ai._generate_ai_analysis_impl)
    assert 'comp["photo_rooms"] = _pr' in body and '"photo_rooms": _subject_rooms' in body
    assert CmaComparable(photos=["a", "b"], photo_rooms=["kitchen", None]).model_dump()["photo_rooms"] == ["kitchen", None]
    assert CmaSubjectProperty(photo_rooms=["bathroom"]).photo_rooms == ["bathroom"] and CmaComparable().photo_rooms is None


def test_comps_are_locked_while_the_ai_is_writing_the_report_but_not_by_a_run_that_died(monkeypatch):
    import pytest
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException
    cs, report, comps, save = _lock_world(monkeypatch, ai_done=False)
    report.generation_status, report.generation_started_at = "generating", datetime.now(timezone.utc) - timedelta(minutes=2)
    with pytest.raises(HTTPException) as e:
        save([comps[0]])
    assert e.value.status_code == 409 and cs.comps_locked(report) is True
    report.generation_status, report.generation_started_at = "generating", datetime.now(timezone.utc) - timedelta(hours=9)
    save([comps[0]])                                             # left "generating" by a crash long ago: not a lock
    assert [c["id"] for c in report.comparables] == ["a"]
    report.generation_status = "failed"
    assert cs.comps_locked(report) is False
