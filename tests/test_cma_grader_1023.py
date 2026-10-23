"""10/23 grader round (4913 Chancellor St, 417 Dogwood Ln V19, 239 Windham Dr): claims built on a
comp's numbers, client jargon, the decimal-point sentence cut and the comp-share self-check."""
import app.main  # noqa: F401
from app.services import cma_ai_service as ai

SUBJ = {"list_price": 450000, "above_grade_finished_area": 2010, "baths": 3.5}


def _comp(price, ag, baths, bg=0):
    return {"sale_price": price, "above_grade_finished_area": ag, "baths": baths, "below_grade_finished_area": bg}


def _blocks(text, subject, comp):
    return ai._comp_note_claim_blocks(text, "Comp #1", subject, comp)


def test_psf_direction_claim_against_subject():
    s = {"list_price": 500000, "above_grade_finished_area": 1831, "baths": 3}
    assert _blocks("About $246/sqft, well above the subject's list-price $/sqft.", s, _comp(420000, 1710, 2))
    assert not _blocks("At $187/sqft it sits below the subject's $224/sqft asking rate.", SUBJ, _comp(379000, 2025, 2.5))


def test_basement_basis_and_understates_claims():
    assert _blocks("It sold at $260/sqft above grade on a reported basis that includes roughly 1,000 sqft of finished basement.",
                   SUBJ, _comp(858000, 3300, 3.5, 1000))
    assert _blocks("Its $195/sqft above grade raw figure understates its value because of the basement.",
                   SUBJ, _comp(370000, 1900, 2.5, 700))


def test_bath_count_and_garbled_sentences():
    assert _blocks("Its half-bath-fewer configuration limits its weight.", SUBJ, _comp(305000, 1200, 1.5))
    assert not _blocks("Its one bath fewer layout matters.", SUBJ, _comp(379000, 2025, 2.5))
    assert _blocks("This comp but warrants an upward condition adjustment.", SUBJ, _comp(362000, 1760, 2.5))
    assert _blocks("Homes here closed above list (median 1.", SUBJ, _comp(362000, 1760, 2.5))


def test_client_jargon_is_plain_english():
    out, _ = ai._final_polish_pass("It supports value through its lower-level area normalization. It is flagged as a "
                                   "statistical outlier and down-weighted accordingly. Comp #2, which the analyst notes is on well.", "x")
    low = out.lower()
    assert "normaliz" not in low and "statistical" not in low and "analyst" not in low and "down-weight" not in low


def test_offer_split_keeps_decimals():
    t = "Homes here closed above list (median 1.065), so the seller may hold firm. Keep an appraisal contingency."
    assert "(median 1.065), so the seller may hold firm." in ai._strip_under_contract_offer_frame(t)


def test_share_check_matches_the_capped_weights():
    w = ai._v10_capped_weights([(5, 0.05), (1, 0.30), (1, 0.30), (1, 0.30), (1, 0.30)])
    rows = [{"label": f"Comp #{i + 1}", "normalized_weight": x, "gross_adjustment_pct": 5 if i == 0 else 30,
             "size_adjusted_price": 380000} for i, x in enumerate(w)]
    assert round(w[0], 3) == 0.467
    assert ai._anchor_integrity_errors({"comps": rows, "anchor_value": 380000}) == []
    rows[0]["normalized_weight"] = 0.6   # a real overweight is still caught
    assert ai._anchor_integrity_errors({"comps": rows, "anchor_value": 380000})
