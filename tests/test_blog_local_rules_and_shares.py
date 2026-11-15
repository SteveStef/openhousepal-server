"""Grader audit 11/13, blogs:
- 11/12 (critical): a Cherry Hill selling article never mentioned the township's resale Certificate of
  Continued Occupancy or its smoke / carbon-monoxide certificate. A selling article now has a required
  research step for municipal sale requirements and an automated check that the article names them.
- 7/8: an abatement article must say the bill can step up during the abatement, and the homestead clash.
- 16: Kennett Square's municipality shares read 102%.
- 2: "right now" on MLS numbers from data weeks old.
- 19: dodged questions (commute, land)."""
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.services.ai import ai_blog_pipeline_service as pipe  # noqa: E402
from app.services.areas import town_knowledge_service as tk  # noqa: E402

SELL = {"topic": "Selling a home in Cherry Hill in winter", "audiences": ["Sellers"], "key_points": "pricing, prep, closing costs"}
BUY = {"topic": "Buying a Fishtown rowhome", "audiences": ["Buyers"], "key_points": "tax abatement, offers"}
CCO = {"topic": "regulation", "origin": "research", "source_url": "https://chnj.gov/893/Residential-Resale-Inspections",
       "claim": "Cherry Hill Township requires a Certificate of Continued Occupancy before the resale of a single-family home."}
SMOKE = {"topic": "regulation", "origin": "research", "source_url": "https://www.chnj.gov/232/Smoke-Carbon-Monoxide-Alarm-Compliance",
         "claim": "Cherry Hill requires a smoke and carbon monoxide alarm compliance certificate before settlement."}


def test_selling_article_gets_the_municipal_research_and_writer_checklists():
    assert "LOCAL SALE REQUIREMENTS ARE REQUIRED RESEARCH" in pipe._coverage(SELL, 1)
    assert "certificate of (continued) occupancy" in pipe._coverage(SELL, 1)
    assert "MUST be told about every municipal resale requirement" in pipe._coverage(SELL, 2)
    assert "LOCAL SALE REQUIREMENTS" not in pipe._coverage(BUY, 1)


def test_abatement_article_asks_whether_it_steps_down_and_about_homestead():
    r, w = pipe._coverage(BUY, 1), pipe._coverage(BUY, 2)
    assert "STEPS DOWN each year" in r and "homestead exemption" in r
    assert "rises in steps DURING the abatement" in w and "no percentages" in w


def test_article_that_skips_both_requirements_is_flagged_twice():
    text = "Price it right, clear the walks, and review the New Jersey disclosure form with your attorney."
    out = pipe.local_sale_requirement_checks(text, [CCO, SMOKE], SELL)
    assert len(out) == 2 and "Certificate of Continued Occupancy" in out[0] and "chnj.gov/893" in out[0]
    assert "smoke" in out[1].lower()


def test_article_that_names_them_passes():
    text = ("Cherry Hill requires a Certificate of Continued Occupancy before a resale, and a smoke and carbon monoxide alarm "
            "certificate before settlement - apply early.")
    assert pipe.local_sale_requirement_checks(text, [CCO, SMOKE], SELL) == []


def test_nothing_found_still_requires_the_confirm_with_the_municipality_sentence():
    out = pipe.local_sale_requirement_checks("Price it right and clear the walks.", [], SELL)
    assert len(out) == 1 and "confirm with the municipality" in out[0]
    ok = "Many municipalities require a resale inspection or a smoke-detector certificate before settlement - check with the township early."
    assert pipe.local_sale_requirement_checks(ok, [], SELL) == []


def test_a_buyer_article_is_not_checked_for_seller_paperwork():
    assert pipe.local_sale_requirement_checks("Offer advice.", [CCO], BUY) == []


def test_kennett_square_shares_no_longer_total_102():
    exact = [41.6, 25.7, 17.6, 9.6, 1.6, 1.5, 1.5]          # each rounded alone: 42+26+18+10+2+2+2 = 102
    rows = [{"name": str(i), "share": round(x), "sort": x} for i, x in enumerate(exact)]
    assert sum(r["share"] for r in rows) == 102
    tk.fit_shares(rows)
    assert sum(r["share"] for r in rows) == round(sum(exact)) == 99
    assert [r["share"] for r in rows][:2] == [42, 26] or [r["share"] for r in rows][0] in (41, 42)


def test_shares_that_already_add_up_are_unchanged():
    rows = [{"share": 67, "sort": 67.0}, {"share": 33, "sort": 33.0}]
    tk.fit_shares(rows)
    assert [r["share"] for r in rows] == [67, 33]


def test_right_now_on_old_mls_numbers_is_flagged():
    stale = {"fresh_wording": False, "current_through_label": "Oct 19, 2026"}
    text = "Right now there are 183 homes for sale in Fishtown. Walk the block at night before you offer."
    out = pipe.stale_wording_checks(text, stale)
    assert len(out) == 1 and "183 homes" in out[0] and "Oct 19, 2026" in out[0]
    assert pipe.stale_wording_checks(text, {"fresh_wording": True}) == []


def test_reviewer_and_writer_are_told_about_dodged_questions():
    assert "DODGED QUESTION" in pipe._REVIEW_SYSTEM


# --- finding 19, the "how much land" half: the ledger now holds lot sizes, so there is an answer to give ---
class _Area:
    name, state, slug = "Kennett Square", "PA", "kennett-square-pa"


LOT = {"count": 154, "from": "2025-11-20", "to": "2026-11-12", "median_sqft": 33106, "p25_sqft": 13000, "p75_sqft": 60000, "acre_plus_count": 61,
       "by_municipality": [{"name": "EAST MARLBOROUGH", "count": 86, "median_sqft": 30734}, {"name": "KENNETT", "count": 38, "median_sqft": 43560},
                           {"name": "KENNETT SQUARE", "count": 18, "median_sqft": 10713}, {"name": "SOMEWHERE ELSE", "count": 9, "median_sqft": 9000}]}
GEO = {"townships": [{"name": "East Marlborough Township", "share": 42}, {"name": "Kennett Township", "share": 28},
                     {"name": "Kennett Square Borough", "share": 18}]}


def test_lot_sizes_reach_the_ledger_overall_and_by_municipality():
    items = tk.lot_items(_Area(), LOT, GEO)
    assert len(items) == 2 and all(i["origin"] == "mls_live" and i["as_of"] == "2026-11-12" for i in items)
    assert "154 single-family houses sold from 2025-11-20 to 2026-11-12" in items[0]["claim"]
    assert "median lot of about 0.76 acres" in items[0]["claim"] and "61 of the 154 had an acre or more" in items[0]["claim"]
    by = items[1]["claim"]
    assert "East Marlborough Township about 0.71 acres (86 sales)" in by and "Kennett Township about 1 acre (38 sales)" in by
    assert "Kennett Square Borough about 0.25 acres (18 sales)" in by
    assert "SOMEWHERE" not in by and "Somewhere" not in by      # a municipality the town's verified geography doesn't list is left out


def test_lot_sizes_say_nothing_when_there_is_nothing_to_say():
    assert tk.lot_items(_Area(), {}, GEO) == [] and tk.lot_items(_Area(), None, GEO) == []
    assert len(tk.lot_items(_Area(), LOT, None)) == 1            # no verified municipalities -> the overall figure only
    assert tk._lot_words(1181) == "about 1,200 square feet" and tk._lot_words(43560 * 4.2) == "about 4.2 acres"


def test_research_lists_sent_as_text_are_read_as_lists():
    # 11/13 Fishtown: "not_found" arrived as a JSON string and was walked one character at a time
    assert pipe.research_list('["Official SEPTA schedule", "2026 millage"]') == ["Official SEPTA schedule", "2026 millage"]
    assert pipe.research_list(["a", "b"]) == ["a", "b"]
    assert pipe.research_list("Nothing on the abatement schedule") == ["Nothing on the abatement schedule"]
    assert pipe.research_list(None) == [] and pipe.research_list("  ") == [] and pipe.research_list({"a": 1}) == []
    assert pipe.research_list('[{"claim": "x", "source_url": "https://a.gov"}]')[0]["claim"] == "x"


# --- 11/14 Kennett Square re-write: two more causes of a dodged or lost answer ---
def _stored(n, topic, claim):
    return {"origin": "idx_research", "town": "Kennett Square, PA", "topic": topic, "claim": f"{claim} #{n}"}


KENNETT_BRIEF = {"areas": ["Kennett Square"], "topic": "A relocation guide to Kennett Square, PA - the borough versus the surrounding townships, schools, commute, home prices and day-to-day life",
                 "key_points": "- Commute to Wilmington and Philadelphia\n- What homes cost and how much land you get",
                 "personal_take": "People moving here are usually surprised how quickly the walkable borough turns into open farmland - five minutes out you're on a few acres."}


def test_what_the_brief_asks_about_survives_the_per_town_cap():
    # 60 stored facts; the two drive times sit near the end, where the cap used to cut them
    items = [_stored(n, "downtown", "A restaurant on State Street serves dinner") for n in range(58)]
    items += [_stored(58, "transit", "From Kennett Square, Downtown Wilmington, DE is about 12.4 miles away: about 31 minutes by car, leaving at weekday 8 AM"),
              _stored(59, "transit", "From Kennett Square, Center City Philadelphia is about 39.4 miles away: about 73 minutes by car")]
    mls = [{"origin": "mls_live", "town": "Kennett Square, PA", "topic": "market", "claim": f"Kennett Square market number {n}"} for n in range(30)]
    cut, _ = pipe._build_ledger(mls + items)
    assert not any("minutes by car" in i["claim"] for i in cut)                      # the old order loses them
    kept, text = pipe._build_ledger(mls + pipe.brief_first(items, KENNETT_BRIEF))
    assert sum("minutes by car" in i["claim"] for i in kept) == 2 and "31 minutes by car" in text
    assert sum(i["origin"] == "mls_live" for i in kept) == 30                         # MLS numbers never count toward the cap
    assert sum(i["origin"] == "idx_research" for i in kept) == 55
    # nothing asked about: the order is left alone
    assert pipe.brief_first(items, {"topic": "Kennett Square", "areas": ["Kennett Square"]}) == items


def test_the_agents_own_take_is_not_an_invented_story():
    from app.services.ai import blog_final_checks as bfc
    own = "In my experience, people moving here are usually surprised by how quickly the walkable borough turns into open farmland."
    assert bfc.final_lint(own, KENNETT_BRIEF) == []
    made_up = "In my experience, sellers in Kennett Square always accept the first offer within a day."
    assert [h["kind"] for h in bfc.final_lint(made_up, KENNETT_BRIEF)] == ["invented_experience"]
    assert [h["kind"] for h in bfc.final_lint(own, {"topic": "x", "personal_take": ""})] == ["invented_experience"]
    assert [h["kind"] for h in bfc.final_lint("I've helped dozens of buyers find acreage in the townships.", KENNETT_BRIEF)] == ["invented_experience"]


# --- 11/14 Fishtown re-write ---
FISHTOWN_BRIEF = {"areas": ["Fishtown"], "audiences": ["Buyers", "First-time buyers"],
                  "topic": "What buyers should know before making an offer on a Fishtown rowhome in 2026 - prices, tax abatements, older-home inspections and how competitive it is",
                  "key_points": "- How the 10-year tax abatement works on newer homes and what happens when it ends\n- How quickly homes are selling and how to price an offer"}
PHILLY_GEO = {"town": "Fishtown", "counties": ["Philadelphia County"], "norm": {"counties": {"philadelphia"}, "townships": {"philadelphia city"}, "districts": set()}}
CHESTER_GEO = {"town": "Kennett Square", "counties": ["Chester County"], "norm": {"counties": {"chester"}, "townships": {"kennett"}, "districts": set()}}


def test_homes_are_selling_does_not_make_it_a_seller_article():
    # the buyer article got the seller research list and a demanded seller caveat
    assert "LOCAL SALE REQUIREMENTS" not in pipe._coverage(FISHTOWN_BRIEF, 1)
    assert pipe.local_sale_requirement_checks("A buyer article about Fishtown rowhomes.", [], FISHTOWN_BRIEF) == []
    for topic in ("How to sell a home in Cherry Hill, NJ during the winter", "Selling your house this spring", "When to sell and what you'll net",
                  "Thinking of selling a Main Line home", "What are my net proceeds"):
        assert "LOCAL SALE REQUIREMENTS" in pipe._coverage({"topic": topic}, 1), topic
    assert "LOCAL SALE REQUIREMENTS" in pipe._coverage({"topic": "Winter market update", "audiences": ["Sellers"]}, 1)
    assert "LOCAL SALE REQUIREMENTS" not in pipe._coverage({"topic": "Why homes are selling fast in Wayne", "audiences": ["Buyers"]}, 1)


def test_philadelphia_abatement_facts_are_supplied_only_where_they_apply():
    items = tk.philadelphia_abatement_items("PA", [PHILLY_GEO], FISHTOWN_BRIEF, today="2026-11-14")
    assert len(items) == 3 and all(i["origin"] == "verified_local" and ("phila.gov" in i["source_url"] or "codelibrary.amlegal.com" in i["source_url"]) for i in items)
    text = " ".join(i["claim"] for i in items)
    assert "declines each year" in text and "not eligible for the Homestead Exemption" in text and "separate abatement programs" in text
    assert "%" not in text                                                         # general words only - no percentages in the ledger
    assert tk.philadelphia_abatement_items("PA", [CHESTER_GEO], FISHTOWN_BRIEF, today="2026-11-14") == []      # not Philadelphia
    assert tk.philadelphia_abatement_items("PA", [PHILLY_GEO, CHESTER_GEO], FISHTOWN_BRIEF, today="2026-11-14") == []
    assert tk.philadelphia_abatement_items("PA", [PHILLY_GEO], {"topic": "Fishtown restaurants"}, today="2026-11-14") == []   # not asked about
    assert tk.philadelphia_abatement_items("PA", [PHILLY_GEO], FISHTOWN_BRIEF, today="2027-07-01") == []     # lapsed until re-read
    assert len(tk.drop_other_county_items(items, [PHILLY_GEO])) == 3


def test_an_abatement_article_must_carry_the_three_facts():
    ledger = tk.philadelphia_abatement_items("PA", [PHILLY_GEO], FISHTOWN_BRIEF, today="2026-11-14")
    vague = "Newer Fishtown homes may come with a property tax abatement. A 2022 report describes how it works, so ask me about it."
    missing = pipe.abatement_checks(vague, ledger)
    assert len(missing) == 3 and all(m.startswith("missing_abatement_fact") for m in missing)
    good = ("Newer Fishtown homes may come with a property tax abatement. On new construction the abated share of the building's value "
            "declines each year, so the tax bill steps up during the abatement. An abated home can't also get the Homestead Exemption until "
            "the abatement ends. Rehab abatements are a separate program, so confirm which abatement program the listing has. That step-up "
            "is for abatements applied for after December 31, 2021; one applied for on or before that date stays in full and ends all at once.")
    assert pipe.abatement_checks(good, ledger) == []
    assert pipe.abatement_checks(vague, []) == []                                  # nothing held, nothing demanded
