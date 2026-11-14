"""Grader audit 11/13, finding 26 - facts that must agree across a whole CMA:
mid-rise vs high-rise in one report, "Public Sewer, Public Septic" on a tower condo, a
"one-bedroom-plus-den" discussed under a 2-bedroom cover, and an unfinished comp basement called
"a step down from the subject's full basement" when the subject's has no finished area on record."""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import cma_fact_check as fc  # noqa: E402

TOWER = {"street_address": "50 S 16TH ST #4407", "property_type": "CONDO", "beds": 2,
         "public_remarks": "Designed as a one-bedroom plus den or home office, it could easily become a two-bedroom."}


def test_a_44th_floor_unit_is_in_a_high_rise():
    assert fc.building_height_class(TOWER) == "high"
    assert fc.building_height_class({"street_address": "12 Elm St #204"}) is None      # 2nd floor: says nothing
    assert fc.building_height_class({"street_address": "418 Reedville Rd"}) is None


def test_mid_rise_is_corrected_everywhere():
    text = "As a mid-rise condo unit, the building roof is the association's responsibility. For a 1990-built high-rise, ask for the reserve study."
    out, flags = fc.fix_building_height_words(text, TOWER)
    assert "mid-rise" not in out and out.startswith("As a high-rise condo unit") and len(flags) == 1
    assert fc.fix_building_height_words(text, {"street_address": "9 Oak Ln"}) == (text, [])


def test_bedroom_record_note_names_both_counts():
    note = fc.bedroom_record_note(TOWER)
    assert note and "2 bedrooms on the MLS record" in note and "one bedroom plus a den" in note
    assert fc.prompt_facts_subject(TOWER)["bedroom_record"] == note
    assert fc.prompt_facts_subject(TOWER)["building_type"].startswith("high-rise building")


def test_no_bedroom_note_when_the_counts_agree():
    assert fc.bedroom_record_note({**TOWER, "beds": 1}) is None
    assert fc.bedroom_record_note({"beds": 3, "public_remarks": "Three bedrooms and a finished basement."}) is None


def test_unfinished_vs_full_basement_is_not_a_difference_when_the_subject_has_no_finished_area():
    subject = {"basement": "Full", "below_grade_finished_area": 0}
    note = ("It was renovated in 2024. Its unfinished basement is a step down from the subject's full basement, but that is "
            "outweighed by the renovation quality. It sold in 6 days.")
    out, flags = fc.fix_basement_unlike_comparison(note, subject, {"basement": "Unfinished, Full"})
    assert "step down" not in out and "the subject's has no finished area on record either" in out
    assert out.startswith("It was renovated in 2024.") and out.endswith("It sold in 6 days.") and flags


def test_a_finished_subject_basement_keeps_the_comparison():
    subject = {"basement": "Full, Finished", "below_grade_finished_area": 800}
    note = "Its unfinished basement is a step down from the subject's full basement."
    assert fc.fix_basement_unlike_comparison(note, subject, {"basement": "Unfinished"}) == (note, [])


def test_public_sewer_drops_the_septic_entry():
    assert fc.clean_sewer(["Public Sewer", "Public Septic"]) == ["Public Sewer"]
    assert fc.clean_sewer(["On Site Septic"]) == ["On Site Septic"]
    assert fc.clean_sewer(["No Sewer System", "On Site Septic"]) == ["No Sewer System", "On Site Septic"]
    assert fc.clean_sewer("Public Sewer") == "Public Sewer"


def test_both_run_in_the_text_pipeline():
    out, flags = fc.fix_text("As a mid-rise condo unit, the roof is the association's.", subject=TOWER)
    assert "high-rise" in out and flags


# ---- finding 27: the report's map lost its top and bottom pins ---------------------------------------
def test_map_is_requested_in_the_frames_own_shape_inside_googles_limit():
    from app.services.cma_manual_subject_service import CMA_MAP_SIZES, google_map_size
    assert google_map_size("900x420") == "640x299"      # was sent as 900x420 -> Google returned 640x420 -> cropped
    assert google_map_size("1200x420") == "640x224"
    assert google_map_size("640x360") == "640x360"
    for size in CMA_MAP_SIZES:
        w, h = (int(x) for x in size.split("x"))
        gw, gh = (int(x) for x in google_map_size(size).split("x"))
        assert max(gw, gh) <= 640 and abs(gw / gh - w / h) < 0.01
