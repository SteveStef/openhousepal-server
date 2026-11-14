"""CMA test 11/13 (418 Reedville Rd, Oxford - seller report): comp #1's client note read "at $303 per
above-grade square foot, well above the subject's $344 list rate". The direction word between a comp's
$/sq ft and the subject's must match the numbers (cma_fact_check.fix_psf_direction, run by fix_text)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.cma_fact_check import fix_psf_direction, fix_text  # noqa: E402

SUBJECT = {"list_price": 849900, "above_grade_finished_area": 2469, "sqft": 3619}   # $344 / above-grade sq ft
COMP = {"sale_price": 675000, "above_grade_finished_area": 2228, "sqft": 2928}


def test_wrong_direction_is_corrected_with_the_numbers():
    t = ("This sale closed quickly (23 days on market) at $303 per above-grade square foot, well above the "
         "subject's $344 list rate on an above-grade basis.")
    out, flags = fix_psf_direction(t, SUBJECT)
    assert "well below the subject's $344 list rate" in out and "above the subject" not in out
    assert flags
    # Through the whole comp-note gate too.
    out2, _ = fix_text(t, subject=SUBJECT, comp=COMP, who="Comp #1")
    assert "well below the subject's $344" in out2


def test_subject_figure_from_the_report_when_the_sentence_has_none():
    out, _ = fix_psf_direction("It sold at $380 per square foot, below the subject's own list rate.", SUBJECT)
    assert "above the subject's own list rate" in out
    out, _ = fix_psf_direction("It sold at $237 per above-grade square foot - below the subject's own list rate.", SUBJECT)
    assert out == "It sold at $237 per above-grade square foot - below the subject's own list rate."


def test_adverb_matches_the_gap():
    out, _ = fix_psf_direction("At $200/sq ft, slightly higher than the subject's $344/sq ft.", SUBJECT)
    assert "well lower than the subject's" in out
    out, _ = fix_psf_direction("At $340 per sq ft, far above the subject's $344 list rate.", SUBJECT)
    assert "below the subject's $344" in out and "far" not in out


def test_right_direction_and_near_ties_are_left_alone():
    for t in ("selling at $197 per above-grade square foot, below the subject's $210 list rate.",
              "sold recently at $205 per above-grade square foot, just under the subject's $210 list rate.",
              "at $209 per sq ft, slightly above the subject's $210 list rate."):          # within 1%
        assert fix_psf_direction(t, {"list_price": 499500, "above_grade_finished_area": 2376}) == (t, [])


# CMA test 11/13 (701 Sturbridge Dr, Bryn Mawr): comp #4's note said "a quiet cul-de-sac, which is a genuine
# plus versus the subject's more trafficked road setting" - a subdivision street, with no road data on record.
from app.services.cma_fact_check import fix_subject_busy_road_claim  # noqa: E402

_NOTE = ("It is on a quiet cul-de-sac, which is a genuine plus versus the subject's more trafficked road setting, "
         "and it closed quickly (4 days) at 120% of list.")


def test_unsupported_busy_road_claim_about_the_subject_is_dropped():
    subj = {"street_address": "701 STURBRIDGE DR", "road_context": None}
    out, flags = fix_subject_busy_road_claim(_NOTE, subj)
    assert out == "It is on a quiet cul-de-sac, which is a genuine plus, and it closed quickly (4 days) at 120% of list."
    assert flags
    out2, _ = fix_text(_NOTE, subject={**subj, "list_price": 1875000, "above_grade_finished_area": 4241}, comp={}, who="Comp #4")
    assert "trafficked" not in out2
    # The road lookup said quiet: dropped too.
    assert "trafficked" not in fix_subject_busy_road_claim(_NOTE, {"street_address": "12 MAIN RD", "road_context": {"verdict": "quiet"}})[0]


def test_claim_kept_when_the_subject_road_is_or_may_be_busy():
    for subj in ({"street_address": "701 STURBRIDGE DR", "road_context": {"verdict": "through"}},
                 {"street_address": "538 S MEDIA LINE RD", "road_context": None}):
        assert fix_subject_busy_road_claim(_NOTE, subj) == (_NOTE, [])


# CMA test 11/13 (418 Reedville Rd comp #5): "a production new-construction home on a roughly 8.8-acre lot" -
# no lot size on record (the community's lots run .21-.40 acres).
from app.services.cma_fact_check import fix_comp_lot_acres  # noqa: E402

_SUBJ_LOT = {"lot_size": 326700, "list_price": 849900, "above_grade_finished_area": 2469}   # 7.50 acres


def test_comp_lot_acres_must_match_the_record():
    t = "An outlier result for a production new-construction home on a roughly 8.8-acre lot that the data flags."
    out, flags = fix_comp_lot_acres(t, _SUBJ_LOT, {"lot_size": 0.0})
    assert out == "An outlier result for a production new-construction home on a lot that the data flags." and flags
    out, _ = fix_comp_lot_acres("It sits on a 3.1-acre lot.", _SUBJ_LOT, {"lot_size": 100188})      # 2.30 acres
    assert out == "It sits on a 2.30-acre lot."
    # Through the comp-note gate.
    assert "8.8" not in fix_text(t, subject=_SUBJ_LOT, comp={"lot_size": None}, who="Comp #5")[0]


def test_comp_lot_acres_left_alone_when_right_or_about_the_subject():
    assert fix_comp_lot_acres("It sits on a 2.3-acre lot.", _SUBJ_LOT, {"lot_size": 100188}) == ("It sits on a 2.3-acre lot.", [])
    t = "Unlike the subject, which sits on a 7.5-acre parcel, this one is small."
    assert fix_comp_lot_acres(t, _SUBJ_LOT, {"lot_size": 0}) == (t, [])
