"""Outside grader, 11/14, on the Kennett Square (88/100) and Fishtown (84/100) re-writes. Root causes, not article patches:

Fishtown 1 (high): a Philadelphia abatement's newer step-down schedule was stated as the rule for every abated home.
    Abatements permitted through 2021 stay exempt in full and end all at once - both kinds are for sale today.
    -> a verified fact states BOTH sides of its dividing line; research, writer and checker are told the same.
Fishtown 2 (high): "$6,900-$17,250" closing costs beside a transfer tax whose buyer's half alone was $7,900.
    -> where we hold a place's whole transfer tax, the closing-cost low end can never sit below that half.
Kennett 1: a $990,000 payment example (asking prices) while houses sold for a median of $738,000.
    -> a house example is priced at the sold median when there are enough sales.
Kennett 2: "40 condos or townhomes" then "the 40 townhomes". -> the group is named by what is in it.
Kennett 3-6, Fishtown 4: walk shares, drive times, lot sizes and the tax estimate gave no method or sample.
    -> "how we counted" notes, and the tax estimate says how many listings it uses."""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services.ai import ai_blog_pipeline_service as pipe  # noqa: E402
from app.services.areas import town_knowledge_service as tk  # noqa: E402

DAY = "2026-11-14"
PHILLY = {"town": "Fishtown", "counties": ["Philadelphia County"], "norm": {"counties": {"philadelphia"}, "townships": {"philadelphia city"}, "districts": set()}}
BRIEF = {"areas": ["Fishtown"], "topic": "Buying a Fishtown rowhome - how the 10-year tax abatement works and what happens when it ends"}
RATE = {"rate_30": 7.11, "as_of": "2026-11-12"}


# ------------------------------------------------------------------ Fishtown 1: both sides of a dividing line
def test_the_abatement_fact_gives_both_schedules_and_says_to_find_out_which():
    items = tk.philadelphia_abatement_items("PA", [PHILLY], BRIEF, today=DAY)
    first = items[0]["claim"]
    assert "TWO SCHEDULES" in first and "January 1, 2022" in first
    # second grader round: the Code's dividing line is when the abatement was APPLIED FOR, not the permit date
    assert "applied for on or before December 31, 2021" in first and "permitted" not in first and "not the building-permit date" in first
    assert first and items[0]["source_url"].startswith("https://codelibrary.amlegal.com/") and "Section 19-1303.4(5)" in items[0]["source_name"]
    assert "exempt in full for the whole ten-year term" in first and "all at once when the abatement ends" in first
    assert "2022 and later" in first and "declines each year" in first and "steps up every year" in first
    assert "has to be confirmed for that home" in first
    assert "%" not in " ".join(i["claim"] for i in items)


def test_an_article_that_gives_only_the_newer_schedule_is_sent_back():
    ledger = tk.philadelphia_abatement_items("PA", [PHILLY], BRIEF, today=DAY)
    # the 11/14 article: correct for 2022+, silent on the older abatements still for sale
    one_sided = ("On the new-construction abatement, the untaxed share of the building gets smaller every year, so your tax bill goes up a step "
                 "each year during the abatement. A home with a 10-year abatement isn't eligible for the Homestead Exemption. The City runs "
                 "separate abatement programs for new construction and for rehab, so confirm which program a listing has.")
    missing = pipe.abatement_checks(one_sided, ledger)
    assert len(missing) == 1 and "TWO schedules" in missing[0] and "older ones stay in full and end all at once" in missing[0]
    both = one_sided + (" That step-up applies to abatements applied for after December 31, 2021. An abatement applied for on or before that "
                        "date stays in full for the whole ten years and then ends all at once, so ask which abatement the home has.")
    assert pipe.abatement_checks(both, ledger) == []
    # the right two schedules pinned to the wrong date are sent back too (the 11/14 afternoon article said "permitted")
    permit = one_sided + (" That step-up is the current version. For projects permitted by the end of 2021, whose abatement was applied for then, "
                          "the building is exempt in full and the bill rises all at once.")
    wrong = pipe.abatement_checks(permit, ledger)
    assert len(wrong) == 1 and "permit date" in wrong[0] and "APPLIED FOR" in wrong[0]
    started = both + " The year the abatement started decides which schedule you get."
    assert len(pipe.abatement_checks(started, ledger)) == 1
    assert pipe.abatement_checks(both + " It is the application date that counts, not the permit date or 2021 construction.", ledger) == []


def test_research_writer_and_checker_are_all_told_about_dividing_lines():
    assert "report BOTH sides in the same claim" in pipe._RESEARCH_SYSTEM
    assert "state both sides and tell the reader to find out which one applies" in pipe._writer_system("PA")
    assert "one side of a dividing line such as an application date or program generation" in pipe._REVIEW_SYSTEM
    assert "BOTH of them" in pipe._coverage(BRIEF, 2)


# ------------------------------------------------------------------ Fishtown 2: closing costs agree with the transfer tax
ENTRY = {"homes": 195, "p25": 345_000, "median": 450_000, "tax_pct_near_p25": 1.1, "tax_homes": 41,
         "houses": 37, "house_median": 990_000, "tax_pct_near_house": 1.19, "tax_homes_house": 12,
         "tax_pct_near_house_sold": 1.3, "tax_homes_house_sold": 9}


def test_the_closing_cost_low_end_is_never_below_the_buyers_half_of_a_transfer_tax_we_hold():
    tax = tk.transfer_tax_facts("PA", [PHILLY], None, today=DAY)
    assert tax["total_pct"] == 4.578 and tax["place"] == "Philadelphia"
    ex = pipe.payment_example(ENTRY, RATE, ["Fishtown"], transfer_tax=tax)
    assert ex["price"] == 345_000 and ex["transfer_tax"] == {"place": "Philadelphia", "total": 15_800, "half": 7_900}
    # second grader round: a low end AT the half left $3 for the lender, title and everything else. The half is its
    # own part and the national 2-5% guide covers the rest: 7,900 + 6,900 and 7,900 + 17,250.
    assert ex["closing_low"] == 14_800 and ex["closing_high"] == 25_150
    assert "your half of Philadelphia's realty transfer tax (about $7,900" in ex["closing_basis"] and "plus the CFPB's typical 2-5%" in ex["closing_basis"]
    assert pipe.closing_note_for(ex, "PA") == "Ask the settlement company for an itemized estimate - it is the real figure."
    claim = next(i["claim"] for i in pipe.payment_example_items(ex) if "closing costs" in i["claim"])
    assert "$14,800-$25,150" in claim and "built from two parts" in claim and "about $7,900" in claim and "$15,800 in all" in claim
    # at every price there is real room above the transfer tax for the other costs
    for price in (150_000, 345_000, 420_000, 900_000, 2_000_000):
        e = pipe.payment_example({**ENTRY, "p25": price}, RATE, ["Fishtown"], transfer_tax=tax)
        assert e["closing_low"] - e["transfer_tax"]["half"] == round(e["price"] * 0.02) and e["closing_low"] < e["closing_high"]


def test_without_a_whole_transfer_tax_on_file_the_national_range_and_the_state_note_stay():
    kennett = {"town": "Kennett Square", "counties": ["Chester County"], "norm": {"counties": {"chester"}, "townships": {"kennett"}, "districts": set()}}
    tax = tk.transfer_tax_facts("PA", [kennett], None, today=DAY)
    assert tax["total_pct"] is None
    ex = pipe.payment_example(ENTRY, RATE, ["Kennett Square"], transfer_tax=tax)
    assert ex["transfer_tax"] is None and ex["closing_low"] == 6_900 and ex["closing_basis"] is None
    assert pipe.closing_note_for(ex, "PA") == pipe._CLOSING_NOTES["PA"] and pipe.closing_note_for(ex, "NJ") is None


# ------------------------------------------------------------------ Kennett 1: a house example at the sold price
def test_a_house_example_uses_what_houses_sold_for_when_there_are_enough_sales():
    sold = {"price": 738_000, "count": 42, "window": "the 90 days ending 2026-11-13"}
    ex = pipe.payment_example(ENTRY, RATE, ["Kennett Square"], basis="house", sold_house=sold, conforming_limit=832_750, limit_area="Chester County")
    assert ex["price"] == pipe._round_price(738_000)
    assert ex["price_basis"] == "About the median price of the 42 single-family houses sold in Kennett Square in the 90 days ending 2026-11-13."
    assert ex["tax_pct"] == 1.3 and ex["tax_basis"] == "tax bills of the 9 similar-priced single-family houses for sale, scaled to this price"
    assert [s["jumbo"] for s in ex["scenarios"]] == [False, False, False]      # the $990,000 version had two jumbo loans
    # too few sales, or not a house article: the listing-based example as before
    few = pipe.payment_example(ENTRY, RATE, ["Kennett Square"], basis="house", sold_house={**sold, "count": 6})
    assert few["price"] == 990_000 and "median list price of the 37 single-family houses for sale" in few["price_basis"]
    assert few["tax_basis"] == "tax bills of the 12 similar-priced single-family houses for sale, scaled to this price"
    entry = pipe.payment_example(ENTRY, RATE, ["Fishtown"], basis="entry", sold_house=sold)
    assert entry["price"] == 345_000 and entry["tax_basis"] == "tax bills of the 41 similar-priced homes for sale, scaled to this price"


# ------------------------------------------------------------------ Kennett 2: one name for one group of homes
class _Area:
    name, state, slug = "Kennett Square", "PA", "kennett-square-pa"


def _mix_claims(attached, townhomes):
    cost = {"as_of": "2026-11-14",
            "mix": {"homes": attached + 37, "attached": {"count": attached, "median": 559_000}, "house": {"count": 37, "median": 990_000},
                    "low_quarter": {"count": 20, "attached": 17}, "one_level": {"count": 1}}}
    if townhomes is not None:
        cost["townhome_fees"] = {"of": townhomes, "condo_fee": 0, "hoa_only": townhomes - 1, "no_fee": 1}
    return [i["claim"] for i in tk.cost_items(_Area(), cost) if "homes for sale on" in i["claim"] or "lowest-priced quarter" in i["claim"]]


def test_the_attached_group_is_named_by_what_is_in_it():
    all_town = _mix_claims(40, 40)
    assert "40 are townhomes (median list price $559K) (all townhomes - no condos are for sale)" in all_town[0]
    assert "17 of 20 are townhomes" in all_town[1] and "mostly reflects townhomes, not houses" in all_town[1]
    mixed = _mix_claims(174, 152)
    assert "174 are condos or townhomes (median list price $559K) (22 condos and 152 townhomes)" in mixed[0]
    assert "are condos or townhomes" in mixed[1] and "condos and townhomes, not houses" in mixed[1]
    assert "are condos or townhomes" in _mix_claims(40, None)[0]                 # no townhome count on file: the general name


# ------------------------------------------------------------------ how we counted
def test_how_we_counted_notes_follow_what_the_article_uses():
    text = ("About 34% of homes are within a 15-minute walk of a grocery store. Downtown Wilmington is about 31 minutes by car. "
            "The median lot was about 0.76 acres. The borough has about 6,096 residents according to the Census Bureau.")
    notes = pipe.how_counted_notes(text, None, town_card=True)
    assert len(notes) == 5
    assert any("measured in a straight line from the home - not a walking time" in n for n in notes) and any("center of the town's map area" in n for n in notes)
    assert any("5 or more such sales" in n for n in notes) and any("American Community Survey" in n for n in notes)
    assert any("middle 90% of homes sold in the last two years" in n for n in notes)
    assert pipe.how_counted_notes("Prices rose this fall.", None) == []
    assert len(pipe.how_counted_notes("Prices rose this fall.", None, town_card=True)) == 1


def test_measured_facts_say_how_they_were_measured():
    class A:
        name, state, slug, facts = "Kennett Square", "PA", "kennett-square-pa", {
            "commute": {"departure": "weekday 8 AM", "computed_at": "2026-10-26T00:00:00+00:00",
                        "destinations": [{"name": "Downtown Wilmington, DE", "miles": 12.4, "drive_minutes": 31}]},
            "walk": {"homes_checked": 400, "walk_minutes": 15, "computed_at": "2026-10-26T00:00:00+00:00",
                     "rows": [{"key": "grocery", "label": "A grocery store", "pct": 34}]}}
    items = tk.research_items(A())
    drive = next(i for i in items if "minutes by car" in i["claim"])
    assert "measured from the center of Kennett Square - a specific home's trip will differ" in drive["claim"]
    # grader 11/14 round 3: the figure is a straight-line distance, so the fact is worded as one - never as a walking time
    walk = next(i for i in items if "0.6 miles" in i["claim"])
    assert "measured in a straight line" in walk["claim"] and "not a walking time" in walk["claim"] and "15-minute walk" not in walk["claim"]
    assert "Never as a walking time" in walk["note"]
