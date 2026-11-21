"""Older CMAs stored comps with no id; the editor gives each comp an id when it opens. Editing a comp's wording must
still save (it was refused as "comps are locked"), while really swapping or re-rating a comp stays refused."""
import copy

import app.main  # noqa: F401 - load the app first (the AI service has a circular import when loaded alone)

from app.services.cma_service import _comp_key, _comp_ratings, _comps_on_report

STORED = [
    {"listing_key": "804622414338", "street_address": "46 ROSEDALE RD", "is_included": True, "strength": "primary",
     "quality": {"interior": "similar"}, "notes": "46 Rosedale Rd sold for $1,240,000."},
    {"listing_key": "804951699378", "street_address": "143 TRENT RD", "is_included": True, "strength": "supporting"},
]


def _as_editor_sends(comps):
    sent = copy.deepcopy(comps)
    for i, c in enumerate(sent):
        c["id"] = f"uuid-{i}"  # the editor's own id for each comp
    return sent


def test_wording_edit_on_an_old_report_is_not_a_comp_change():
    sent = _as_editor_sends(STORED)
    sent[0]["notes"] += " X"
    assert _comps_on_report(sent) == _comps_on_report(STORED)
    assert _comp_ratings(sent) == _comp_ratings(STORED)


def test_swapping_or_rerating_a_comp_is_still_caught():
    swapped = _as_editor_sends(STORED)
    swapped[1]["listing_key"] = "999"
    assert _comps_on_report(swapped) != _comps_on_report(STORED)
    rerated = _as_editor_sends(STORED)
    rerated[0]["quality"] = {"interior": "above"}
    assert _comp_ratings(rerated) != _comp_ratings(STORED)
    off = _as_editor_sends(STORED)
    off[1]["is_included"] = False
    assert _comps_on_report(off) != _comps_on_report(STORED)


def test_manual_comp_without_listing_uses_its_id():
    assert _comp_key({"id": "abc", "street_address": "9 Elm St"}) == "abc"
    assert _comp_key({"street_address": "9 Elm St"}) == "9 Elm St"
