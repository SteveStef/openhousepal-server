"""11/20 seller audit (four live seller CMAs + an outside grader). Each test uses the real sentence or record that
went wrong."""
import datetime

from app.services import cma_ai_service as ai
from app.services import cma_comp_pick as pick
from app.services import cma_fact_check as fc


# --- comp search: the same floor plan one floor down (901 N Penn St #F810) was out-scored by smaller units ---------

def test_mixed_sewer_words_are_unknown_not_private():
    assert ai._private_utilities({"water_source": ["Public"], "sewer": ["Public Septic", "Public Sewer"]}) is None
    assert ai._private_utilities({"water_source": ["Public"], "sewer": ["Public Sewer"]}) is False
    assert ai._private_utilities({"water_source": ["Public"], "sewer": ["Septic Exists"]}) is True
    assert ai._private_utilities({"water_source": ["Well"], "sewer": ["Public Sewer"]}) is True
    assert ai._private_utilities({"water_source": ["Public"], "sewer": ["Cesspool"]}) is True
    assert ai._private_utilities({}) is None


def test_same_building_is_same_neighborhood_in_the_city():
    subj = {"street_address": "901 N PENN ST #F910", "zipcode": "19123", "subdivision_name": "WATERFRONT SQUARE",
            "school_district": "THE SCHOOL DISTRICT OF PHILADELPHIA", "city": "PHILADELPHIA"}
    twin = {"street_address": "901 N PENN ST #F810", "zipcode": "19123", "subdivision_name": "NORTHERN LIBERTIES",
            "school_district": "THE SCHOOL DISTRICT OF PHILADELPHIA", "city": "PHILADELPHIA"}
    score, detail = ai._relevance_market_area_score(subj, twin, urban=True)
    assert score == 1.0 and detail["neighborhood"] == "same building"
    other = {**twin, "street_address": "1001 N 2ND ST"}
    assert ai._relevance_market_area_score(subj, other, urban=True)[0] < 1.0      # a different building: as before


# --- the home's last sale read like the home under contract's ------------------------------------------------------

def test_last_sale_is_its_own_paragraph_naming_this_home():
    text = ("Homes under contract (market context, not a sale): 901 N Penn St #R2003 went under contract at a $658,000 "
            "asking price.")
    hist = {"last_sale": {"date": "2024-09-30", "price": 582500}, "last_sale_is_this_listing": True}
    out = fc.history_sentence(text, hist, today=datetime.date(2026, 11, 20), listed=False)
    assert "\n\nThis home last sold on Sep 30, 2024 for $582,500." in out
    assert fc.history_sentence(out, hist, today=datetime.date(2026, 11, 20), listed=False) == out   # never twice


# --- the photo comparison sentence printed twice (17 Foxtrail Rd) ----------------------------------------------------

def test_placement_sentence_not_added_again_after_rewording():
    trace = {"placement_check": {"placement": "upper_half", "how": "comp_marks", "source": "photos", "where": "level with the homes it matches",
                                 "placement_words": "nicer than 3 of the comparable sales and about the same as 2"}}
    polished = ("The home was compared with each sale on condition and finishes, using its photos, which the agent confirmed "
                "show it as it is today. Sale records can't show this. It is nicer than 3 of the comparable sales and about "
                "the same as 2.")
    assert ai._v10_placement_sentence(polished, trace=trace) == (polished, [])
    fresh, flags = ai._v10_placement_sentence("The value is $510,000.", trace=trace)
    assert "something sale records can't show" in fresh and flags


# --- "the subject's list price" on a home that isn't for sale -------------------------------------------------------

def test_no_list_price_rate_for_a_home_not_for_sale():
    for status in ("Not currently listed", "Closed", "Expired"):
        subj = {"list_price": 599500, "sqft": 1455, "above_grade_finished_area": 1455, "mls_status": status}
        assert fc.subject_list_psf(subj) is None
        assert pick.asking_price_now(subj) is None
    listed = {"list_price": 599500, "sqft": 1455, "above_grade_finished_area": 1455, "mls_status": "Active"}
    assert round(fc.subject_list_psf(listed)) == 412                                  # a home for sale: unchanged


# --- "$1" taxes on two 2025 new builds (6979 Mayfair Ter) -----------------------------------------------------------

def test_placeholder_taxes_are_not_a_tax_gap():
    subj = {"annual_taxes": 3204}
    comp = {"annual_taxes": 1, "sale_price": 499990}
    assert ai._annual_taxes(comp) is None
    assert ai._tax_gap_sentence(subj, comp, 389000) is None
    assert ai._annual_taxes({"annual_taxes": 1889}) == 1889                           # a real low bill still counts


# --- wrong facts about a named sale in a sentence naming two sales -------------------------------------------------

def _gate(text, subject, comps):
    nums = {c["listing_key"]: i + 1 for i, c in enumerate(comps)}
    addrs = {c["listing_key"]: c["street_address"].title() for c in comps}
    return ai._reconcile_directional_claims(text, "Methodology summary", subject, comps, nums, addrs)


def test_lot_direction_checked_per_sale_in_a_two_sale_sentence():
    subject = {"lot_size": 0.18, "beds": 4, "baths": 2.5}
    comps = [{"listing_key": "a", "street_address": "117 DURHAM LN", "lot_size": 0.30},
             {"listing_key": "b", "street_address": "63 JOSEPH WICK DR", "lot_size": 0.29, "beds": 5},
             {"listing_key": "c", "street_address": "93 LILAC WAY", "lot_size": 0.20, "beds": 3}]
    text = ("63 JOSEPH WICK DR, with more bedrooms and baths and a smaller lot, marks the upper limit, while 93 LILAC WAY, "
            "smaller and without a basement, marks the lower limit.")
    out, flags, blocking = _gate(text, subject, comps)
    assert "with more bedrooms and baths and a larger lot, marks the upper limit" in out
    assert "93 LILAC WAY, smaller and without a basement" in out and not blocking and flags


def test_community_pool_is_not_a_pool():
    subject = {"lot_size": 0.30}
    comps = [{"listing_key": "a", "street_address": "22 SAINT ANTHONY CT", "pool": ["Yes - Community"]},
             {"listing_key": "b", "street_address": "11 STAFFORDSHIRE RD", "pool": None}]
    text = ("22 Saint Anthony Ct, which is larger and has a pool and other amenities, marks the upper limit. "
            "11 Staffordshire Rd marks the lower end.")
    out, flags, _b = _gate(text, subject, comps)
    assert "has access to a community pool and other amenities" in out and flags
    private = [{**comps[0], "pool": ["Yes - Personal", "In Ground"]}, comps[1]]
    assert _gate(text, subject, private)[0] == text                                  # a real pool: left alone


def test_unit_letter_keeps_its_case():
    assert ai._tidy_caps_address("901-00 N PENN ST #P1103") == "901-00 N Penn St #P1103"
    assert ai._tidy_caps_address("1522 EVERGREEN LN") == "1522 Evergreen Ln"
