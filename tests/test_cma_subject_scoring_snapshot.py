from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import app.main as _app_main  # noqa: F401 - mirrors the production service import order
from app.schemas.cma import CmaCompCandidate
from app.services.cma_ai_service import _continuous_relevance, _v10_sqft
from app.services.cma_service import (
    _merge_authoritative_subject_facts,
    _score_comparable_candidates,
    _subject_scoring_fingerprint,
    _subject_scoring_snapshot,
)
from app.services.cma_similarity_service import calibrate_match_percentage


def _lesley_property():
    return SimpleNamespace(
        above_grade_finished_area=5611,
        below_grade_finished_area=587,
        architectural_style=["Colonial"],
        township="RADNOR",
        subdivision_name="ARDROSSAN",
        latitude=40.043,
        longitude=-75.366,
        living_area=6198,
        lot_size=37026,
        year_built=1929,
        home_type="SINGLE_FAMILY",
        school_district_name="RADNOR TOWNSHIP",
        description=None,
        raw_mls_data={
            "SchoolDistrictName": "RADNOR TOWNSHIP",
            "PublicRemarks": "Pool and apartment above detached garage.",
        },
    )


def test_thin_saved_subject_is_filled_from_one_authoritative_property_record():
    subject = {
        "listing_key": "805142852090",
        "source": "mls",
        "street_address": "847 LESLEY RD",
        "sqft": 6198,
    }

    merged = _merge_authoritative_subject_facts(subject, _lesley_property())

    assert merged["above_grade_finished_area"] == 5611
    assert merged["architectural_style"] == ["Colonial"]
    assert merged["township"] == "RADNOR"
    assert merged["subdivision_name"] == "ARDROSSAN"
    assert merged["public_remarks"] == "Pool and apartment above detached garage."


def test_agent_entered_subject_fact_is_not_silently_overwritten():
    subject = {
        "listing_key": "805142852090",
        "source": "mls",
        "above_grade_finished_area": 5700,
        "township": "Agent-confirmed township",
    }

    merged = _merge_authoritative_subject_facts(subject, _lesley_property())

    assert merged["above_grade_finished_area"] == 5700
    assert merged["township"] == "Agent-confirmed township"
    assert merged["architectural_style"] == ["Colonial"]


def test_scoring_fingerprint_covers_every_authoritative_match_fact():
    merged = _merge_authoritative_subject_facts({}, _lesley_property())
    first = _subject_scoring_fingerprint(merged)
    assert _subject_scoring_snapshot(merged)["above_grade_finished_area"] == 5611

    changed = {**merged, "above_grade_finished_area": 5612}
    assert _subject_scoring_fingerprint(changed) != first
    assert _subject_scoring_fingerprint(dict(merged)) == first


def test_backend_candidate_scoring_overwrites_stale_badges_and_owns_the_trace():
    subject = {
        **_merge_authoritative_subject_facts({}, _lesley_property()),
        "listing_key": "subject",
        "source": "mls",
    }
    context = {
        "version": "adaptive-proximity-v3",
        "distance_half_life_miles": 2.025,
        "local_market_half_life_miles": 2.025,
        "reference_count": 59,
        "density_distances_used_miles": [1.98, 2.02, 2.03],
        "fallback_used": False,
        "subject_snapshot": _subject_scoring_snapshot(subject),
        "subject_snapshot_sha256": _subject_scoring_fingerprint(subject),
    }
    stale = CmaCompCandidate(
        listing_key="wayne",
        status="sold",
        street_address="230 S WAYNE AVE",
        city="WAYNE",
        distance_miles=0.82,
        sale_price=2_200_000,
        close_price=2_200_000,
        close_date="2026-08-17",
        sale_date="2026-08-17",
        price_verified=True,
        sqft=3706,
        above_grade_finished_area=3706,
        lot_size=11761,
        year_built=1929,
        architectural_style=["Colonial"],
        township="RADNOR",
        subdivision_name="SOUTH WAYNE",
        school_district="RADNOR TOWNSHIP",
        property_type="SINGLE_FAMILY",
        public_remarks="Future opportunity for an in-law or au pair space.",
        match_score=99,
        similarity_details={"scoring_authority": "stale_browser_value"},
    )

    scored = _score_comparable_candidates(subject, [stale], context)[0]

    assert scored.match_score != 99
    assert scored.match_score == scored.similarity_details["display_match_score"]
    assert scored.similarity_details["final_similarity_score"] == scored.match_score
    assert scored.similarity_details["scoring_authority"] == "backend_continuous_relevance"
    assert scored.similarity_details["subject_snapshot_sha256"] == context["subject_snapshot_sha256"]


def test_cma_workspace_has_no_browser_match_score_calculation_path():
    workspace = Path(__file__).resolve().parents[2] / "frontend" / "app" / "studio" / "cma" / "[id]" / "page.tsx"
    source = workspace.read_text(encoding="utf-8")
    comps_step = source[source.index("function CompsStep"):source.index("function CompCandidateCard")]

    assert "getMatchScore(" not in comps_step
    assert "computeMatchScoreDetails(" not in comps_step
    assert "candidate.match_score" in comps_step


# ---------------------------------------------------------------------------
# Product/amenity modifier removal (2026-09-13): pool, guest-apartment/ADU, guest-house, and
# property-type-mismatch wording used to multiply the weighted 8-component base score by a
# 0.50-1.0 "product modifier" AFTER it was computed. Match %, ranking, sorting, auto-selection,
# and continuous-relevance weighting are now the 8 core components only - no post-score modifier
# of any kind. The underlying amenity-detection helpers (_amenity_signals,
# _explicit_accessory_features, etc.) are untouched and still feed Astra's valuation/adjustment
# evidence elsewhere in cma_ai_service.py; they are simply no longer wired into this scorer.

_MODIFIER_REMOVAL_DENSITY = {
    "version": "adaptive-proximity-v3",
    "distance_half_life_miles": 2.025,
    "local_market_half_life_miles": 2.025,
}


def _modifier_removal_subject():
    return {
        "listing_key": "subject",
        "source": "mls",
        "above_grade_finished_area": 5611,
        "sqft": 6198,
        "lot_size": 37026,
        "year_built": 1929,
        "architectural_style": ["Colonial"],
        "property_type": "SINGLE_FAMILY",
        "school_district": "RADNOR TOWNSHIP",
        "township": "RADNOR",
        "subdivision_name": "ARDROSSAN",
        "city": "VILLANOVA",
        "public_remarks": "A stately Colonial with a sparkling pool and an apartment above the detached garage.",
    }


def _modifier_removal_comp(listing_key: str, **overrides) -> dict:
    comp = {
        "listing_key": listing_key,
        "distance_miles": 0.8,
        "above_grade_finished_area": 5500,
        "sqft": 6000,
        "lot_size": 36000,
        "year_built": 1935,
        "architectural_style": ["Colonial"],
        "property_type": "SINGLE_FAMILY",
        "school_district": "RADNOR TOWNSHIP",
        "township": "RADNOR",
        "subdivision_name": "ARDROSSAN",
        "city": "VILLANOVA",
        "status": "sold",
        "price_verified": True,
        "sale_price": 3_000_000,
        "close_price": 3_000_000,
        "close_date": (date.today() - timedelta(days=30)).isoformat(),
        "public_remarks": "",
    }
    comp.update(overrides)
    return comp


def test_pool_and_accessory_wording_does_not_change_match_percentage():
    """Same 8-core-component facts, only the remarks text differs (mentions both features,
    mentions neither, or explicitly denies both) - Match % must be identical either way."""
    subject = _modifier_removal_subject()
    comps = [
        _modifier_removal_comp(
            "mentions-both",
            public_remarks="Gorgeous colonial with a heated pool and a guest apartment above the garage.",
        ),
        _modifier_removal_comp(
            "mentions-neither",
            public_remarks="Gorgeous colonial on a quiet street with a two-car garage.",
        ),
        _modifier_removal_comp(
            "explicit-denial",
            public_remarks="No pool. No in-law suite or guest apartment on this property.",
        ),
    ]

    relevance = _continuous_relevance(subject, comps, density_context=_MODIFIER_REMOVAL_DENSITY)
    rows_by_key = {row["listing_key"]: row for row in relevance["comps"]}

    base_scores = {row["base_relevance_score"] for row in rows_by_key.values()}
    adjusted_scores = {row["adjusted_relevance_score"] for row in rows_by_key.values()}
    assert len(base_scores) == 1, rows_by_key
    assert len(adjusted_scores) == 1, rows_by_key
    # Nothing runs after the weighted 8-component sum - adjusted must equal base for every row.
    for row in rows_by_key.values():
        assert row["adjusted_relevance_score"] == row["base_relevance_score"]

    match_percentages = {calibrate_match_percentage(row["adjusted_relevance_score"]) for row in rows_by_key.values()}
    assert len(match_percentages) == 1


def test_amenity_wording_does_not_change_ranking_or_continuous_weighting():
    """Ranking (proposed_role) and comp-weighting (continuous_weight_input) must also be
    unaffected by amenity wording alone, not just the raw/display score."""
    subject = _modifier_removal_subject()
    comps = [
        _modifier_removal_comp("with-pool", public_remarks="Stunning home with an in-ground pool."),
        _modifier_removal_comp("no-mention", public_remarks="Stunning home on a private lane."),
    ]

    relevance = _continuous_relevance(subject, comps, density_context=_MODIFIER_REMOVAL_DENSITY)
    rows = relevance["comps"]

    assert rows[0]["proposed_role"] == rows[1]["proposed_role"]
    assert rows[0]["continuous_weight_input"] == rows[1]["continuous_weight_input"]
    assert rows[0]["mechanically_eligible"] == rows[1]["mechanically_eligible"]


def test_property_type_mismatch_is_not_double_penalized_beyond_its_core_component():
    """A property-type mismatch used to also trigger a 0.55x post-score modifier on top of the
    core `property_type` component already scoring 0. That extra penalty is gone - the mismatch
    shows up exactly once, inside the weighted property_type component."""
    subject = _modifier_removal_subject()
    comp = _modifier_removal_comp("condo-mismatch", property_type="CONDO")

    relevance = _continuous_relevance(subject, [comp], density_context=_MODIFIER_REMOVAL_DENSITY)
    row = relevance["comps"][0]

    assert row["components"]["property_type"] == 0.0
    assert row["adjusted_relevance_score"] == row["base_relevance_score"]


def test_continuous_relevance_rows_carry_no_product_modifier_fields():
    """Locks in the architectural removal: the per-comp row can never again carry a
    product_modifier / product_mismatch_reasons / documented_major_product_mismatch key that a
    future change could silently start multiplying back into the score."""
    subject = _modifier_removal_subject()
    comp = _modifier_removal_comp("plain")

    relevance = _continuous_relevance(subject, [comp], density_context=_MODIFIER_REMOVAL_DENSITY)
    row = relevance["comps"][0]

    assert "product_modifier" not in row
    assert "product_mismatch_reasons" not in row
    assert "documented_major_product_mismatch" not in row


# ---------------------------------------------------------------------------
# Property-type ROLE/eligibility guard (2026-09-13): a structurally incompatible property type
# (condo vs single-family, townhouse vs detached single-family, etc.) can no longer become a
# Strong/primary-auto-selected comp - but this is an eligibility rule only, layered on TOP of the
# unchanged 8-component Match %. It must never move base_relevance_score/adjusted_relevance_score/
# match_score beyond the property_type component's own normal 12-point contribution, and it must
# never fire for two property types that are just different labels for the same general housing
# product (e.g. "SINGLE_FAMILY" vs "DETACHED").

_PROPERTY_TYPE_WEIGHT = 12.0 / 100.0  # matches _CONTINUOUS_RELEVANCE_COMPONENT_WEIGHTS["property_type"]


def test_property_type_eligibility_guard_does_not_change_match_percentage():
    """A condo comp's score differs from an otherwise-identical single-family comp by EXACTLY the
    property_type component's own 12/100 weight - nothing more. No separate penalty is stacked on
    top by the new eligibility guard."""
    subject = _modifier_removal_subject()
    same_type = _modifier_removal_comp("same-type", property_type="SINGLE_FAMILY")
    condo = _modifier_removal_comp("condo-mismatch", property_type="CONDO")

    relevance = _continuous_relevance(subject, [same_type, condo], density_context=_MODIFIER_REMOVAL_DENSITY)
    rows_by_key = {row["listing_key"]: row for row in relevance["comps"]}
    same_type_row = rows_by_key["same-type"]
    condo_row = rows_by_key["condo-mismatch"]

    assert same_type_row["components"]["property_type"] == 1.0
    assert condo_row["components"]["property_type"] == 0.0
    assert condo_row["base_relevance_score"] == round(same_type_row["base_relevance_score"] - _PROPERTY_TYPE_WEIGHT, 4)
    # Adjusted (post-eligibility) score is still just the base score - the guard changes role,
    # not the number that becomes Match %.
    assert condo_row["adjusted_relevance_score"] == condo_row["base_relevance_score"]
    assert calibrate_match_percentage(condo_row["adjusted_relevance_score"]) == calibrate_match_percentage(
        condo_row["base_relevance_score"]
    )


def test_fundamentally_incompatible_property_type_cannot_become_strong_or_auto_selected():
    """Even when every other component is a near-perfect match (score would otherwise clear the
    Strong threshold), a condo comp against a single-family subject is held to 'weak' - which also
    zeroes its continuous_weight_input, so it cannot be a primary anchor comp - and is flagged for
    the frontend's auto-pick filter (property_type_incompatible / _score_comparable_candidates)."""
    subject = _modifier_removal_subject()
    near_perfect_condo = _modifier_removal_comp(
        "near-perfect-condo",
        property_type="CONDO",
        distance_miles=0.05,
        above_grade_finished_area=subject["above_grade_finished_area"],
        sqft=subject["sqft"],
        lot_size=subject["lot_size"],
        year_built=subject["year_built"],
        architectural_style=subject["architectural_style"],
        close_date=date.today().isoformat(),
    )

    relevance = _continuous_relevance(subject, [near_perfect_condo], density_context=_MODIFIER_REMOVAL_DENSITY)
    row = relevance["comps"][0]

    # It would clear the Strong bar on score alone...
    assert row["adjusted_relevance_score"] >= 0.72
    # ...but the eligibility guard still holds it to weak / zero anchor weight.
    assert row["structurally_incompatible_property_type"] is True
    assert row["proposed_role"] == "weak"
    assert row["continuous_weight_input"] == 0.0

    # The search-candidate path (used by the frontend's auto-select) surfaces the same signal.
    context = {
        "distance_half_life_miles": 2.025,
        "subject_snapshot_sha256": _subject_scoring_fingerprint(subject),
    }
    candidate = CmaCompCandidate(listing_key="near-perfect-condo", match_score=1, **{
        k: v for k, v in near_perfect_condo.items() if k not in ("listing_key",)
    })
    scored = _score_comparable_candidates(subject, [candidate], context)[0]
    assert scored.similarity_details["property_type_incompatible"] is True
    assert scored.similarity_details["proposed_role"] == "weak"


def test_compatible_property_type_variant_is_not_flagged_or_held_back():
    """Different labels for the SAME general housing product (e.g. 'SINGLE_FAMILY' vs 'DETACHED')
    must not trip the incompatibility guard - a strong match stays eligible for Strong."""
    subject = _modifier_removal_subject()
    detached_variant = _modifier_removal_comp(
        "detached-variant",
        property_type="DETACHED",
        distance_miles=0.05,
        above_grade_finished_area=subject["above_grade_finished_area"],
        sqft=subject["sqft"],
        lot_size=subject["lot_size"],
        year_built=subject["year_built"],
        architectural_style=subject["architectural_style"],
        close_date=date.today().isoformat(),
    )

    relevance = _continuous_relevance(subject, [detached_variant], density_context=_MODIFIER_REMOVAL_DENSITY)
    row = relevance["comps"][0]

    assert row["components"]["property_type"] == 1.0
    assert row["structurally_incompatible_property_type"] is False
    assert row["adjusted_relevance_score"] >= 0.72
    assert row["proposed_role"] == "strong"
    assert row["continuous_weight_input"] == row["adjusted_relevance_score"]


# ---------------------------------------------------------------------------
# Display calibration v2 (2026-09-14): replaced the raw^0.70 power curve (v1) with a piecewise
# curve. v1 always lifted a LOW raw score proportionally more than a high one (d/dx(x^k) is
# decreasing in x for k<1), which a 240-real-candidate benchmark across Lesley/Cherry/Erika/
# Hamilton showed in practice - raw~0.56 got +10.6pts of lift, raw~0.84 got only +4.5pts. v2 is
# identity (no lift) through the mediocre/decent range and only opens up above the empirical
# real-world ceiling (~raw 0.70-0.90), so lift now INCREASES with raw instead of decreasing.
# This changes displayed Match % only - ranking, the 8 component weights, adaptive proximity,
# comp roles/eligibility, and valuation math (which consume raw_relevance_score directly, never
# this display value) are all untouched.


def test_calibration_is_identity_below_first_breakpoint():
    """A mediocre/decent raw score (<= 0.70) must display as-is, with zero lift."""
    assert calibrate_match_percentage(0.30) == 30
    assert calibrate_match_percentage(0.50) == 50
    assert calibrate_match_percentage(0.65) == 65
    assert calibrate_match_percentage(0.70) == 70


def test_calibration_lift_increases_with_raw_in_the_real_world_range():
    """The whole point of v2: across the range real comps actually occupy (raw <= ~0.90, per the
    240-candidate benchmark - nothing observed ever exceeded 0.89), the display lift (display -
    raw*100) must get LARGER as raw increases, not smaller - the opposite of v1's power-curve
    shape. (The 0.70-0.90 segment's own slope of 1.25 guarantees this; above 0.90 the curve
    deliberately eases back to meet (1.0, 100) exactly, which is never exercised by real data.)"""
    def lift(raw: float) -> float:
        return calibrate_match_percentage(raw) - raw * 100

    assert lift(0.60) == 0  # below the first breakpoint: no lift at all
    assert lift(0.60) < lift(0.75) < lift(0.85) < lift(0.90)


def test_calibration_reaches_high_80s_90s_for_exceptional_raw_scores():
    """A raw score at/above the empirical real-world ceiling (~0.85-0.90) should read as
    exceptional to a real estate agent, not just 'good'."""
    assert calibrate_match_percentage(0.85) >= 88
    assert calibrate_match_percentage(0.90) == 95
    assert calibrate_match_percentage(1.0) == 100


def test_calibration_is_continuous_monotonic_and_bounded():
    """Every segment must meet exactly at its breakpoint (no jump) and the whole function must
    stay monotonic (ranking safety) and within [1, 100] across the full raw domain."""
    samples = [value / 200 for value in range(0, 201)]  # 0.00, 0.005, ..., 1.00
    displayed = [calibrate_match_percentage(value) for value in samples]

    assert displayed == sorted(displayed)
    assert all(1 <= value <= 100 for value in displayed)
    # No discontinuity at either breakpoint.
    assert abs(calibrate_match_percentage(0.70) - calibrate_match_percentage(0.7001)) <= 1
    assert abs(calibrate_match_percentage(0.90) - calibrate_match_percentage(0.9001)) <= 1


# ---------------------------------------------------------------------------
# CMA sqft display (2026-09-14): the CMA UI now shows an explicit "Above Grade" / "Total
# Finished" breakdown on comp cards whenever they meaningfully differ, specifically so an agent
# never sees one sqft number on the card while a different one drove the Match %. This is a
# presentation-only change - the similarity engine itself is untouched (above-grade primary,
# living_area/sqft fallback only when above-grade is missing, per _v10_sqft). The regression this
# guards: the number the frontend will label "Above Grade" must be the EXACT same number and same
# field (above_grade_finished_area) that _v10_sqft used for the size component - never a
# recomputed, rounded, or substituted value that could quietly drift from what was scored.


def test_displayed_above_grade_value_matches_similarity_scoring_input():
    comp = _modifier_removal_comp(
        "ag-display-check", above_grade_finished_area=5728, sqft=7732,
    )

    # The scoring input itself: _v10_sqft must pick the smaller above-grade figure, not the
    # larger total (this is the existing, unchanged behavior - not what this test is locking in).
    assert _v10_sqft(comp) == 5728

    subject = _modifier_removal_subject()
    candidate = CmaCompCandidate(
        listing_key="ag-display-check", status="sold", price_verified=True,
        sale_price=3_000_000, close_price=3_000_000, distance_miles=0.8,
        above_grade_finished_area=5728, sqft=7732, lot_size=36000, year_built=1935,
        architectural_style=["Colonial"], property_type="SINGLE_FAMILY",
    )
    context = {
        "distance_half_life_miles": 2.025,
        "subject_snapshot_sha256": _subject_scoring_fingerprint(subject),
    }
    scored = _score_comparable_candidates(subject, [candidate], context)[0]

    # What the frontend receives (and would render as "Above Grade") must be the exact same
    # value, from the exact same field, that similarity scoring used - not a copy that could
    # silently diverge (e.g. via rounding, relabeling, or falling back to the total instead).
    assert scored.above_grade_finished_area == _v10_sqft(comp)
    assert scored.above_grade_finished_area == 5728
    # The total figure the UI would label "Total Finished" stays separately available and
    # unchanged - it was never the scoring input, and this doesn't make it one.
    assert scored.sqft == 7732
