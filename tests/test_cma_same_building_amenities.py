"""CMA test 11/13 (50 S 16th St #4407, a Center City condo): units in the subject's own building share its
amenities. #4504 was flipped to "Comp Superior" by the feature-record check and back to Similar, and
#5203 (same building) was cleared "for an agent call" - and because both counted as "corrected",
every comp's written note was replaced by the stiff fact template.
cma_ai_service._same_building_amenities is used by both the full run and the recompute."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.cma_ai_service import _same_building_amenities  # noqa: E402

SUBJECT = {"property_type": "Condo", "street_address": "50 S 16TH ST #4407", "zipcode": "19102"}


def _comp(addr, amenities, reconciled=True):
    c = {"street_address": addr, "zipcode": "19102", "quality": {"lot": "n_a", "interior": "similar"}}
    if amenities is not None:
        c["quality"]["amenities"] = amenities
    if reconciled:
        c["_amenities_fact_reconciled"] = True
    return c


def test_flipped_and_back_is_not_a_correction():
    c = _comp("50 S 16TH ST #4504", "above")          # the record check made it Superior; the AI said similar
    flags = _same_building_amenities(SUBJECT, c, "similar", "Comp #1")
    assert c["quality"]["amenities"] == "similar" and flags
    assert "_amenities_fact_reconciled" not in c     # the AI's own note is kept


def test_a_cleared_rating_in_the_same_building_is_similar_too():
    c = _comp("50 S 16TH ST #5203", None)
    _same_building_amenities(SUBJECT, c, "similar", "Comp #2")
    assert c["quality"]["amenities"] == "similar" and "_amenities_fact_reconciled" not in c


def test_a_real_change_still_counts_as_corrected():
    # AI said "below", the building rule makes it similar - a real change: the note is rewritten.
    c = _comp("50 S 16TH ST #3801", "below")
    _same_building_amenities(SUBJECT, c, "below", "Comp #3")
    assert c["quality"]["amenities"] == "similar" and c["_amenities_fact_reconciled"]


def test_other_buildings_and_houses_untouched():
    other = _comp("1414 S PENN SQ #14E", "above")
    assert _same_building_amenities(SUBJECT, other, "similar", "Comp #4") == []
    assert other["quality"]["amenities"] == "above" and other["_amenities_fact_reconciled"]
    house = {**SUBJECT, "property_type": "Single Family"}
    c = _comp("50 S 16TH ST #4504", "above")
    assert _same_building_amenities(house, c, "similar", "Comp #1") == [] and c["quality"]["amenities"] == "above"
