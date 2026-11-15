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
    assert s.startswith("This is a two-unit income property.") and "rents and expenses are not in the MLS and were not analysed" in s
    assert ai._income_property_sentence({"property_type": "SINGLE_FAMILY"}) is None
    assert ai._income_property_sentence({"property_type": "MULTI_FAMILY"}).startswith("This is a multi-unit income property.")


def test_unit_count_comes_from_the_unit_mix_the_mls_sends():
    from app.services import cma_service as cs
    assert cs.multi_family_unit_count({"two_bedroom_units": "1", "three_bedroom_units": "1", "one_bedroom_units": "0"}) == 2
    assert cs.multi_family_unit_count({"raw_mls_data": {"OneBedroomUnits": 2, "TwoBedroomUnits": 1}}) == 3
    assert cs.multi_family_unit_count({"public_remarks": "A five-unit building, fully rented."}) == 5      # the listing's words still work
    assert cs.multi_family_unit_count({"one_bedroom_units": "1"}) is None                                 # one unit is not a count
