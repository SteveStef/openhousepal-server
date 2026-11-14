"""Owner, 11/13: a new-construction comp that closed far above its list price is most likely a
well-optioned home over the builder's BASE price, not a bidding war. 418 Reedville Rd's report read
two Wicklow Heights builds as "113%" and "156% of asking" and "strong demand for new builds"."""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services import cma_fact_check as fc  # noqa: E402
from app.services.cma_ai_service import (  # noqa: E402
    _asking_price_market_check, _builder_base_price_sale, _seller_comp_market_read,
)

WICKMERE = {"new_construction_yn": True, "sale_price": 867620, "list_price": 554900, "status": "sold", "days_on_market": 1}


def test_new_construction_well_over_list_is_a_builder_base_price_sale():
    assert _builder_base_price_sale(WICKMERE)
    assert _builder_base_price_sale({"builder_base_price": True})                      # the stored flag, on a re-run


def test_not_flagged_without_both_halves():
    assert not _builder_base_price_sale({**WICKMERE, "new_construction_yn": False})    # a resale bidding war stays one
    assert not _builder_base_price_sale({**WICKMERE, "new_construction_yn": None})
    assert not _builder_base_price_sale({**WICKMERE, "sale_price": 560000})            # new build sold near its list


def test_sellers_market_read_leaves_the_builder_sale_out():
    resale = [{"status": "sold", "sale_price": 495000, "list_price": 500000, "days_on_market": 20},
              {"status": "sold", "sale_price": 500000, "list_price": 500000, "days_on_market": 10}]
    with_builder = _seller_comp_market_read(resale + [WICKMERE])
    assert with_builder["n"] == 2 and with_builder["sale_to_list"] < 1.0


def test_asking_price_check_ignores_a_ratio_the_packet_left_blank():
    comps = [{"sale_price": 495000, "sold_list_ratio": 0.99, "dom": 20}, {"sale_price": 500000, "sold_list_ratio": 1.0, "dom": 10},
             {"sale_price": 867620, "sold_list_ratio": None, "dom": 1}]   # the builder sale: no ratio on purpose
    check = _asking_price_market_check({"current_list_price": 520000, "dom": 5}, {}, {"state": "normal"}, comps)
    assert check["comp_ratios_counted"] == 2 and check["comp_median_sold_to_list_ratio"] < 1.0
    assert check["over_ask_pattern"] is None


def test_note_loses_the_demand_claim_and_gains_the_plain_reason():
    comp = {"builder_base_price": True}
    note = ("This new-construction Wicklow Heights home sold for $627,790, well above its $554,900 list price, reflecting "
            "strong demand for new builds in this East Nottingham/Oxford submarket, but on a sub-half-acre lot.")
    out, flags = fc.fix_builder_base_price_claims(note, comp)
    assert "strong demand" not in out and "builder's base price" in out and "rather than a bidding war" in out
    assert len(flags) == 2


def test_sold_to_list_aside_is_removed():
    comp = {"builder_base_price": True}
    note = ("This brand-new build sold for $867,620 against a $554,900 list price - a sold/list ratio of over 1.5 - after "
            "just one day on market.")
    out, _ = fc.fix_builder_base_price_claims(note, comp)
    assert "ratio" not in out and "after just one day" in out and out.endswith(fc.BUILDER_BASE_PRICE_SENTENCE)


def test_bidding_situation_wording_becomes_upgrades():
    note = ("At $301 per above-grade square foot it is priced below the subject's rate, but that figure reflects an unusually "
            "competitive new-construction bidding situation rather than a reliable gauge of established-home value.")
    out, flags = fc.fix_builder_base_price_claims(note, {"builder_base_price": True})
    assert "bidding" not in out and "reflects upgrades over the builder's base price rather than a reliable gauge" in out and flags


def test_a_resale_note_and_a_note_that_already_explains_are_left_alone():
    resale = "It sold 8% over its asking price in 4 days, reflecting strong demand."
    assert fc.fix_builder_base_price_claims(resale, {"builder_base_price": False}) == (resale, [])
    ok = "It closed above the builder's base price, most likely on upgrades."
    assert fc.fix_builder_base_price_claims(ok, {"builder_base_price": True}) == (ok, [])


def test_runs_inside_the_comp_note_pipeline():
    out, flags = fc.fix_text("It sold well above its list price, reflecting strong demand for new builds.",
                             subject={}, comp={"builder_base_price": True})
    assert "strong demand" not in out and "base price" in out and flags
