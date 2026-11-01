"""11/01 grader round (719 Beechwood Rd, Media PA - a total-rehab home on 1.2 acres with septic, listed at
$250,000 with "auction format" notes). The inputs are the shapes that report had; the assertions are the
rules, never a hardcoded value."""
import app.main  # noqa: F401
from app.services import cma_ai_service as ai
from app.services import cma_fact_check as fc
from app.services import cma_manual_subject_service as manual
from app.services import cma_service as cs


# ---- auction listings ----------------------------------------------------------------------------

def test_auction_found_from_sale_type_or_listing_text():
    assert ai._auction_listing({"sale_type": ["Auction"]})["source"] == "mls_sale_type"
    assert ai._auction_listing({"sale_type": ["Auction", "Standard"]})
    assert ai._auction_listing({"public_remarks": "Listed price is an opening bid only and is not indicative of the final sale price."})
    assert ai._auction_listing({"public_remarks": "Online auction Mon. Oct 12th"})
    assert not ai._auction_listing({"public_remarks": "Not an auction. Lovely colonial on a quiet street."})
    assert not ai._auction_listing({"sale_type": ["Standard"], "public_remarks": "Bring your ideas."})


def _comp_packets(ratio=1.0, n=4):
    return [{"sale_price": 300000, "sold_list_ratio": ratio, "dom": 10} for _ in range(n)]


def test_asking_price_check_skipped_for_an_auction():
    packet = {"current_list_price": 250000, "dom": 5}
    exposure = {"state": "insufficient_exposure"}
    assert ai._asking_price_market_check(packet, {}, exposure, _comp_packets()) is not None
    packet["auction_listing"] = {"source": "mls_sale_type", "phrase": "Auction"}
    assert ai._asking_price_market_check(packet, {}, exposure, _comp_packets()) is None


def test_auction_list_price_is_never_a_value_ceiling():
    subject = {"list_price": 250000, "days_on_market": 30, "sale_type": ["Auction"]}
    tl = {"active_market_days": 30, "current_list_price": 250000}
    exp = ai._compute_exposure(subject, tl, 20, {"anchor_value": 278000, "anchor_high": 290000})
    assert exp["auction_listing"] is True
    assert not exp.get("exposure_ceiling") and not exp.get("hard_upper")


def test_auction_offer_is_a_bidding_plan_with_the_value_as_the_limit():
    band = ai._v10_build_buyer_offer_band(278000, 250000, trace={"exposure": {"auction_listing": True}},
                                          confidence="medium", status_timeline={"active_market_days": 5})
    assert band["auction"] and band["strong_offer"] == band["ceiling"] == 278000
    assert band["opening_offer_low"] == 250000 < band["opening_offer_high"] <= 278000
    text, _ = ai._v10_rebuild_buyer_offer_prose(
        "Include a strong inspection contingency. Negotiate repairs after the inspection. Use an escalation clause.", band, 5)
    low = text.lower()
    assert "opening bid" in low and "auction" in low and "$278,000" in text
    assert "contingency so" not in low and "negotiate repairs" not in low and "escalation" not in low
    assert "% below" not in low and "under list" not in low


def test_auction_explained_in_the_value_notes():
    trace = {"exposure": {"auction_listing": True, "current_list_price": 250000}}
    text, flags = ai._v10_auction_sentence("The sales point to about $278,000.", trace=trace)
    assert "auction" in text and "$250,000" in text and "opening bid" in text and flags
    again, flags2 = ai._v10_auction_sentence(text, trace=trace)
    assert again == text and not flags2
    assert ai._v10_auction_sentence("x.", trace={"exposure": {}})[0] == "x."


# ---- offer advice that contradicts itself ---------------------------------------------------------

def test_short_inspection_period_dropped_next_to_an_inspection_contingency():
    band = ai._v10_build_buyer_offer_band(278000, 290000, trace={"exposure": {"state": "normal_exposure"}},
                                          confidence="medium", status_timeline={"active_market_days": 20})
    text, _ = ai._v10_rebuild_buyer_offer_prose(
        "Include a strong inspection contingency. Keep a short inspection period to stay competitive.", band, 5)
    assert "inspection contingency" in text and "short inspection period" not in text
    same, _ = ai._v10_rebuild_buyer_offer_prose(
        "Ask for a strong inspection contingency with a short inspection period.", band, 5)
    assert "short inspection period" not in same and "inspection contingency" in same


# ---- lot and sewer ----------------------------------------------------------------------------------

def test_sewer_kinds():
    assert ai._sewer_kind({"sewer": ["On Site Septic"]}) == "septic"
    assert ai._sewer_kind({"sewer": '["Public Sewer"]'}) == "public"
    assert ai._sewer_kind({"sewer": "Public Septic"}) is None          # Bright's mixed value = unknown
    assert ai._sewer_kind({"sewer": "Cess Pool"}) is None              # cesspools have their own line
    assert ai._sewer_kind({}) is None


def _grid(subject, comp, monkeypatch, **knobs):
    for k, v in knobs.items():
        monkeypatch.setattr(ai, k, v)
    row = {"_c": comp, "price": 300000, "ag": 1500, "bg": 0, "bg_estimated": False}
    return ai._v10_grid_adjustments(subject, row, 1500, 0, False, 200)


def test_big_lot_gap_gets_a_lot_line_small_gap_does_not(monkeypatch):
    subject = {"lot_size": 1.2 * 43560, "sewer": ["Public Sewer"]}
    big = _grid(subject, {"lot_size": 0.25 * 43560, "sewer": ["Public Sewer"]}, monkeypatch, _V10_FEATURE_LOT_BIG_PCT=0.03)
    assert big["lot"] > 0 and any("lot" in n for n in big["notes"])
    small = _grid(subject, {"lot_size": 1.0 * 43560, "sewer": ["Public Sewer"]}, monkeypatch, _V10_FEATURE_LOT_BIG_PCT=0.03)
    assert small["lot"] == 0
    smaller_subject = _grid({"lot_size": 0.25 * 43560}, {"lot_size": 1.2 * 43560}, monkeypatch, _V10_FEATURE_LOT_BIG_PCT=0.03)
    assert smaller_subject["lot"] < 0


def test_septic_line_direction(monkeypatch):
    on_septic = _grid({"sewer": ["On Site Septic"]}, {"sewer": ["Public Sewer"]}, monkeypatch, _V10_FEATURE_SEPTIC_PCT=0.03)
    assert on_septic["sewer"] < 0                     # subject on septic: a public-sewer sale moves down
    comp_septic = _grid({"sewer": ["Public Sewer"]}, {"sewer": ["On Site Septic"]}, monkeypatch, _V10_FEATURE_SEPTIC_PCT=0.03)
    assert comp_septic["sewer"] > 0
    assert abs(on_septic["sewer"]) <= ai._V10_FEATURE_SEPTIC_MAX


def test_lot_acres_is_one_two_decimal_figure():
    assert fc.lot_acres_text({"lot_size": 50094}).startswith("1.15 acres")
    assert fc.lot_acres_text({"lot_size_acres": 1.2}).startswith("1.20 acres")
    assert fc.lot_acres_text({}) is None


# ---- condition words ---------------------------------------------------------------------------------

def test_total_rehabilitation_needed_reads_as_a_project():
    assert ai._condition_tier({"public_remarks": "Total rehabilitation needed. Large lot."}) <= 0.5
    assert ai._condition_tier({"public_remarks": "Substantially renovated in 2024."}) == 4.0


def test_renovated_remarks_are_not_flagged_needs_major_work():
    assert not [i for i in ai._material_system_issues({"public_remarks": "Once a fixer-upper, now substantially renovated."})
                if "major work" in i]
    assert ai._material_system_issues({"public_remarks": "Handyman special - needs work."})


# ---- text polish -------------------------------------------------------------------------------------

def test_open_quote_closed_where_the_phrase_ends():
    out = ai._close_open_quotes('The listing calls it "total rehabilitation needed. Budget for a full rehab.')
    assert out.count('"') == 2 and '"total rehabilitation needed".' in out
    assert ai._close_open_quotes('It says "as is" plainly.') == 'It says "as is" plainly.'
    assert ai._close_open_quotes("A 1.2 acre lot.") == "A 1.2 acre lot."


# ---- client page pieces -------------------------------------------------------------------------------

def test_weak_active_listings_left_off_the_client_page():
    assert cs.COMPETITION_MIN_SCORE >= ai._PENDING_STRONG_MATCH


def test_location_map_only_draws_the_reports_own_pins():
    subject = {"latitude": 39.91539, "longitude": -75.37126}
    comps = [{"latitude": 39.9001, "longitude": -75.3602}]
    pins = manual.parse_map_pins("S:39.91539,-75.37126|1:39.9001,-75.3602")
    assert manual.map_pins_belong(pins, subject, comps)
    assert not manual.map_pins_belong(manual.parse_map_pins("S:40.0,-75.0"), subject, comps)
    for bad in ("", "SS:1,2", "S:abc,1", "S:95,1", "|".join(f"1:1,{i}" for i in range(13))):
        try:
            manual.parse_map_pins(bad)
            assert False, bad
        except (ValueError, TypeError):
            pass


def test_lot_and_septic_differences_are_explained_when_not_priced():
    subject = {"lot_size": 1.2 * 43560, "sewer": ["On Site Septic"]}
    rows = [{"_c": {"lot_size": a * 43560, "sewer": ["Public Sewer"]}} for a in (0.1, 0.25, 0.36)]
    out = ai._lot_and_sewer_uncertainties(subject, rows)
    assert any("1.20-acre lot" in u and "larger" in u for u in out)
    assert any("septic" in u and "3 of the 3" in u for u in out)
    assert not ai._lot_and_sewer_uncertainties({"lot_size": 0.3 * 43560, "sewer": ["Public Sewer"]}, rows[1:])
    text, flags = ai._v10_lot_sewer_sentences("The sales point to about $278,000.", trace={"anchor": {"main_uncertainties": out}})
    assert "acre" in text and "septic" in text and flags
    same, _ = ai._v10_lot_sewer_sentences("Its large lot and septic system are weighed.", trace={"anchor": {"main_uncertainties": out}})
    assert same == "Its large lot and septic system are weighed."


def test_single_quote_left_open_is_closed_apostrophes_untouched():
    out = ai._close_open_quotes("The listing's own words, 'total rehabilitation needed' and 'sold as-is. Buyers should budget.")
    assert "'sold as-is'." in out and "listing's" in out
    same = "The buyers' agent said it's fine and 'as-is' is stated."
    assert ai._close_open_quotes(same) == same


def test_auction_not_explained_twice():
    trace = {"exposure": {"auction_listing": True, "current_list_price": 250000}}
    text = "The advertised list price is an opening bid, not market value."
    assert ai._v10_auction_sentence(text, trace=trace)[0] == text


def test_what_if_renovation_wording_is_not_swapped():
    subject = {"street_address": "719 Beechwood Rd", "public_remarks": "Total rehabilitation needed. Sold as-is."}
    text = "It shows what a comparable footprint commands once renovated, next to the subject."
    assert "condition-unverified" not in ai._fix_subject_condition_words(text, subject)


def test_wrong_subject_psf_in_a_comp_note_is_corrected():
    subject = {"list_price": 300000, "above_grade_finished_area": 1200}
    out, flags = fc.fix_subject_psf_numbers("It sold for $256/sqft, above the subject's list-price rate of $256/sqft.", subject)
    assert "rate of $250/sqft" in out and flags
    same, f2 = fc.fix_subject_psf_numbers("Above the subject's $250/sq ft list rate.", subject)
    assert not f2
