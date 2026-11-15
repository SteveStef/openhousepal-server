"""Owner decision 11/13 ("stay safe but be informative"): an article MAY state a transfer-tax rate, worded
as an estimate, when we hold it from the government's own page - and never a rate we do not hold. This
replaces the 10/24 rule "never state a transfer tax rate" (grader audit 11/13, findings 9 and 14: the
Fishtown and Cherry Hill articles named the tax but gave readers no idea what it costs)."""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services.ai import ai_blog_pipeline_service as pipe  # noqa: E402
from app.services.ai import blog_final_checks as bfc  # noqa: E402
from app.services.areas import town_knowledge_service as tk  # noqa: E402

DAY = "2026-11-14"
PHILLY = {"town": "Fishtown", "counties": ["Philadelphia County"],
          "norm": {"counties": {"philadelphia"}, "townships": {"philadelphia city"}, "districts": set()}}
KENNETT = {"town": "Kennett Square", "counties": ["Chester County"],
           "norm": {"counties": {"chester"}, "townships": {"kennett township", "east marlborough township"}, "districts": set()}}
CHERRY = {"town": "Cherry Hill", "counties": ["Camden County"],
          "norm": {"counties": {"camden"}, "townships": {"cherry hill township"}, "districts": set()}}
SOLD = {"price": 403_000, "count": 61, "from": "2026-08-16", "to": "2026-11-13", "town": "Fishtown"}


def test_philadelphia_gets_the_verified_total_and_a_worked_example():
    f = tk.transfer_tax_facts("PA", [PHILLY], SOLD, today=DAY)
    assert f["rates"] == [4.578, 3.578, 1.0]
    # $403,000 -> a $405,000 example (nearest $5,000); 4.578% = $18,541 -> about $18,500; half of that, exactly
    assert f["amounts"] == [405_000, 18_500, 9_250]
    text = " ".join(i["claim"] for i in f["items"])
    assert "4.578%" in text and "$18,500" in text and "$9,250" in text and "61 homes sold in Fishtown" in text
    assert "phila.gov" in f["source_url"] and f["items"][0]["source_url"] == f["source_url"]
    assert "about 4.578%" in f["note"] and "$18,500" in f["note"]


def test_no_dollar_example_without_enough_sales_behind_the_price():
    f = tk.transfer_tax_facts("PA", [PHILLY], {**SOLD, "count": 3}, today=DAY)
    assert f["amounts"] == [] and len(f["items"]) == 1 and "$" not in f["note"]
    assert tk.transfer_tax_facts("PA", [PHILLY], None, today=DAY)["amounts"] == []


def test_rest_of_pennsylvania_gets_the_state_rate_and_never_a_town_total():
    f = tk.transfer_tax_facts("PA", [KENNETT], {**SOLD, "town": "Kennett Square"}, today=DAY)
    assert f["rates"] == [1.0, 1.0] and f["amounts"] == []
    assert "do NOT hold the local rate" in f["items"][0]["claim"]
    assert "never give a total percent or a dollar figure" in f["note"] and "$" not in f["note"]
    # two towns, one of them outside Philadelphia: Philadelphia's rate must not be used for both
    assert tk.transfer_tax_facts("PA", [PHILLY, KENNETT], SOLD, today=DAY)["rates"] == [1.0, 1.0]
    # a general article with no towns
    assert tk.transfer_tax_facts("PA", [], None, today=DAY)["rates"] == [1.0, 1.0]


def test_new_jersey_fee_follows_the_published_schedule():
    assert tk.nj_realty_transfer_fee(350_000) == 2_105      # 600 + 335 + 1,170
    assert tk.nj_realty_transfer_fee(500_000) == 4_175      # 870 + 425 + 2,880
    assert tk.nj_realty_transfer_fee(900_000) == 870 + 425 + 3_360 + 3_180 + 580
    assert tk.nj_realty_transfer_fee(1_000_001) is None and tk.nj_realty_transfer_fee(0) is None
    f = tk.transfer_tax_facts("NJ", [CHERRY], {"price": 498_000, "count": 140, "from": "2026-08-16", "to": "2026-11-13", "town": "Cherry Hill"}, today=DAY)
    assert f["rates"] == [] and f["amounts"] == [500_000, 4_200]
    assert "paid by the seller" in f["note"] and "reduced rate" in f["note"] and "Give no per-$500 rates" in f["note"]
    # no sold price to work from -> nothing to state; and no example above $1,000,000
    assert tk.transfer_tax_facts("NJ", [CHERRY], None, today=DAY) is None
    assert tk.transfer_tax_facts("NJ", [CHERRY], {"price": 1_400_000, "count": 20, "from": "a", "to": "b", "town": "X"}, today=DAY) is None


def test_states_we_hold_nothing_for_and_expired_rows_state_nothing():
    for st in ("DE", "MD", "VA", "DC", "WV", None):
        assert tk.transfer_tax_facts(st, [CHERRY], SOLD, today=DAY) is None
    assert tk.transfer_tax_facts("PA", [PHILLY], SOLD, today="2027-07-01") is None    # Philadelphia's row has lapsed: nothing, not the state row
    assert tk.transfer_tax_facts("PA", [KENNETT], None, today="2027-12-01") is None
    assert tk.transfer_tax_facts("NJ", [CHERRY], SOLD, today="2027-07-01") is None


def test_the_items_survive_the_place_filters():
    f = tk.transfer_tax_facts("PA", [PHILLY], SOLD, today=DAY)
    assert len(tk.drop_stale_editions(tk.drop_other_county_items(f["items"], [PHILLY]))) == 2
    g = tk.transfer_tax_facts("PA", [KENNETT], None, today=DAY)
    assert len(tk.drop_other_county_items(g["items"], [KENNETT])) == 1


BRIEF = {"topic": "Buying a Fishtown rowhome", "audiences": ["Buyers"]}
RATES = [4.578, 3.578, 1.0]


def _kinds(text, rates=None):
    return [(h["kind"], h["fix"][:60]) for h in bfc.final_lint(text, BRIEF, rates)]


def test_held_rate_worded_as_an_estimate_passes():
    ok = ("Philadelphia's realty transfer tax is currently about 4.578% of the sale price in total - 3.578% to the City and 1% to the state. "
          "On a $405,000 home that comes to roughly $18,500, and buyer and seller commonly split it.")
    assert _kinds(ok, RATES) == []
    assert _kinds("In Pennsylvania the state's share of the transfer tax is about 1% of the price, with a local tax added on top.", [1.0, 1.0]) == []


def test_without_a_held_rate_the_old_rule_still_stands():
    s = "Philadelphia's realty transfer tax is currently about 4.578% of the sale price in total."
    assert [k for k, _ in _kinds(s, None)] == ["rule_detail"]
    assert [k for k, _ in _kinds(s, [])] == ["rule_detail"]


def test_a_rate_we_do_not_hold_is_sent_back_with_the_ones_we_do():
    found = bfc.final_lint("The combined transfer tax here is about 4.278% of the sale price.", BRIEF, RATES)
    assert len(found) == 1 and "don't hold" in found[0]["fix"] and "4.578%" in found[0]["fix"]
    # Kennett Square: the state part may be stated, a town total may not
    found = bfc.final_lint("Expect a transfer tax of about 2% in Kennett Square.", BRIEF, [1.0, 1.0])
    assert len(found) == 1 and found[0]["kind"] == "rule_detail"


def test_a_held_rate_stated_flat_is_sent_back_to_be_worded_as_an_estimate():
    found = bfc.final_lint("The realty transfer tax in Philadelphia is 4.578% of the sale price.", BRIEF, RATES)
    assert len(found) == 1 and "as an estimate" in found[0]["fix"]


def test_other_fine_print_is_untouched_by_the_exception():
    # a property tax as a percent, a millage rate and a program limit are still fine print
    assert [k for k, _ in _kinds("Property taxes run about 1% of the home's value each year.", RATES)] == ["rule_detail"]
    assert [k for k, _ in _kinds("The school district's millage rate of 14.2 mills applies.", RATES)] == ["rule_detail"]
    # the grader's item and the code checks carry the rates through
    out = bfc.code_checks("The realty transfer tax in Philadelphia is about 4.578% of the sale price.", BRIEF, [], "Cora Maddox", RATES)
    assert "fine_print" not in out
    assert "fine_print" in bfc.code_checks("The realty transfer tax in Philadelphia is about 4.578% of the sale price.", BRIEF, [], "Cora Maddox")


def test_writer_reviewer_and_grader_are_told_the_same_rule():
    assert "TRANSFER TAX YOU MAY STATE" in pipe._writer_system("PA")
    assert 'begins "TRANSFER TAX"' in pipe._REVIEW_SYSTEM
    assert 'begins "TRANSFER TAX"' in pipe._GRADE_SYSTEM
    assert "given as an estimate" in dict((p[0], p[2]) for p in bfc.PASS_LIST)["fine_print"]
