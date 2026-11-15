"""Grader round 3 (11/14 night, three articles: Ardmore 89 / Ardmore-cheaper 83 / Gladwyne 87).

The findings that were real engine problems - each fixed where the fact is made, so every article gets it:
  1 (High)  "99% of homes within a 15-minute walk": the figure is a straight-line distance, not a walking time
  2 (High)  "monthly cost incl. HOA fees": three listings showed a fee and the article had no dollar amounts
  7         "condo, townhome or twin": 10 of 19 "townhome" sales were twins and the article said it had no twin figure
  8         "what my sales say about pricing": only price and month - we held the asking price and days on market
  4, 5      the train answered from a map estimate; a parking lot "shown as full"
  9         the town card's transit line disagreed with the article's own text
No network and no database."""
import asyncio
from types import SimpleNamespace

from app.services.ai import ai_blog_pipeline_service as pipe
from app.services.areas import market_stats_service as ms
from app.services.areas import town_knowledge_service as tk


# ----------------------------------------------------------------------------- 1. distance, not walking time

def test_a_straight_line_share_is_worded_as_a_distance():
    class A:
        name, state, slug, facts = "Ardmore", "PA", "ardmore-pa", {
            "walk": {"homes_checked": 400, "walk_minutes": 15, "computed_at": "2026-11-01T00:00:00+00:00", "stations": ["Ardmore", "Ardmore Junction"],
                     "rows": [{"key": "transit", "label": "A train or trolley stop", "pct": 99}, {"key": "grocery", "label": "A grocery store", "pct": 0}]}}
    items = tk.research_items(A())
    share = next(i for i in items if "99%" in i["claim"])
    assert "within about 0.6 miles of a train or trolley stop, measured in a straight line" in share["claim"]
    assert "not a walking time" in share["claim"] and "minute walk" not in share["claim"]
    assert any(i["claim"].startswith("Almost no Ardmore homes are within about 0.6 miles of a grocery store") for i in items)
    stops = next(i for i in items if "Ardmore Junction" in i["claim"])
    assert "within about 0.6 miles (straight line) of some Ardmore homes" in stops["claim"] and "minute walk" not in stops["claim"]
    assert tk.walk_miles(15) == "0.6" and tk.walk_miles(10) == "0.4" and tk.walk_miles(None) == "0.6"


def test_a_share_given_as_walking_time_is_caught_in_the_article():
    bad = ("About 99% of Ardmore homes are within a 15-minute walk of a train or trolley stop. "
           "Nearly all homes here are in walking distance of a park.")
    assert len(pipe.walk_time_claims(bad)) == 2
    fine = ("About 99% of Ardmore homes are within about 0.6 miles of a train or trolley stop, measured in a straight line. "
            "For a first home in Ardmore, being able to walk to the train is worth more than an extra bedroom. "     # the agent's opinion: no share in it
            "Homes sold in a median of 6 days, and 17 of the 33 sold above asking.")
    assert pipe.walk_time_claims(fine) == []
    problems = pipe.code_problems(bad, brief={"topic": "Buying in Ardmore"}, protections=[])
    walk = [p for p in problems if p["kind"] == "walk_time"]
    assert len(walk) == 2 and walk[0]["pass"] == "math" and "straight line" in walk[0]["problem"] and not walk[0]["cut"]
    assert "straight line" in pipe._ENGINE_RULES and "Never turn it into a walking time" in pipe._ENGINE_RULES
    # the data note under the article follows whichever wording is used
    assert any("not a walking time" in n for n in pipe.how_counted_notes(fine, None))


# ----------------------------------------------------------------------------- 2. a few listings' fees

def test_a_handful_of_listing_fees_is_given_as_a_range_not_withheld():
    area = SimpleNamespace(name="Ardmore", state="PA", slug="ardmore-pa")
    cost = {"as_of": "2026-11-14", "hoa_few": {"townhouse": {"homes": 3, "of": 13, "low_monthly": 85, "high_monthly": 310}}}
    (item,) = [i for i in tk.cost_items(area, cost) if "fee" in i["claim"]]
    assert item["claim"] == ("Ardmore: the 3 of the 13 townhomes for sale that list a monthly condo or association fee show $85 to $310/month, all fees "
                             "combined - too few listings for a typical figure, so ask for the fee on any home you're considering.")
    assert item["origin"] == "mls_live" and item["as_of"] == "2026-11-14"
    one = tk.few_fees_claim({"homes": 1, "of": 1, "low_monthly": 430, "high_monthly": 430}, "condos")
    assert one.startswith("one condo for sale that lists a monthly condo or association fee shows $430/month")
    # ten or more still get the median line, and never both
    many = {"as_of": "2026-11-14", "hoa": {"condo": {"homes": 12, "of": 14, "median_monthly": 410, "covers": []}},
            "hoa_few": {"condo": {"homes": 12, "of": 14, "low_monthly": 200, "high_monthly": 900}}}
    claims = [i["claim"] for i in tk.cost_items(area, many)]
    assert any("median of $410/month" in c for c in claims) and not any("too few listings" in c for c in claims)


# ----------------------------------------------------------------------------- 7. twins

def test_twins_are_told_apart_from_row_townhomes():
    assert ms.attached_style(["Twin/Semi-Detached"]) == "twin" and ms.attached_style(["End of Row/Townhouse"]) == "row"
    assert ms.attached_style(["Interior Row/Townhouse"]) == "row" and ms.attached_style(None) is None and ms.attached_style(["Detached"]) is None
    note = tk.attached_styles_note({"twin": {"count": 10, "median_close_price": 520000}, "row": {"count": 9, "median_close_price": 480000}})
    assert note == (" Of those 19 townhome sales, 10 were twins (a house that shares one side wall with its neighbor) with a $520K median "
                    "and 9 were row townhomes with a $480K median.")
    few = tk.attached_styles_note({"twin": {"count": 3}, "row": {"count": 8, "median_close_price": 455000}})
    assert "3 were twins (a house that shares one side wall with its neighbor) (too few for a median of their own)" in few and "8 were row townhomes with a $455K median" in few
    assert tk.attached_styles_note(None) == "" and tk.attached_styles_note({"row": {"count": 9}}) == ""      # no twins: nothing to add


# ----------------------------------------------------------------------------- 8. the agent's own sales

def test_the_agents_own_sale_says_how_it_compared_with_asking():
    assert pipe.sale_vs_asking(7150000, 7495000, 125) == "; its last asking price was $7,495,000, so it closed about 4.6% below asking, after 125 days on the market"
    assert pipe.sale_vs_asking(1665000, 1850000, 75).startswith("; its last asking price was $1,850,000, so it closed about 10.0% below asking")
    assert "above asking" in pipe.sale_vs_asking(520000, 500000, None) and "at its asking price" in pipe.sale_vs_asking(500000, 500000, 0)
    assert pipe.sale_vs_asking(500000, None, 12) == "; it took 12 days on the market" and pipe.sale_vs_asking(500000, None, None) == ""
    sales = [{"id": "804812985848", "address": "26 Old Gulph Rd", "city": "Gladwyne", "price": 7150000, "sold_date": "2026-10-08"},
             {"id": "x", "address": "1 Other St", "city": "Gladwyne", "price": 900000, "sold_date": "2026-09-01"}]
    items = pipe._sale_items(sales, {"804812985848": {"asking": 7495000.0, "days": 125}})
    assert "closed about 4.6% below asking, after 125 days on the market" in items[0]["claim"] and "buyer/seller side not recorded" in items[0]["claim"]
    assert "asking" not in items[1]["claim"]                       # no record: nothing is made up
    assert pipe._sale_items(sales)[0]["claim"].count("asking") == 0


def test_sale_records_never_sink_an_article():
    class Db:
        async def execute(self, *_a, **_k):
            raise RuntimeError("database is busy")
    assert asyncio.run(pipe.sale_records(Db(), [{"id": "1"}])) == {}
    assert asyncio.run(pipe.sale_records(Db(), [])) == {}

    class Ok:
        async def execute(self, *_a, **_k):
            return SimpleNamespace(all=lambda: [("1", 500000.0, 12)])
    assert asyncio.run(pipe.sale_records(Ok(), [{"id": 1}])) == {"1": {"asking": 500000.0, "days": 12}}


# ----------------------------------------------------------------------------- 4, 5. research rules

def test_a_train_question_makes_the_timetable_a_required_look_up():
    brief = {"topic": "First-time buyer guide to Ardmore", "key_points": "- Getting to Center City by train"}
    research, writer_side = pipe._coverage(brief, 1), pipe._coverage(brief, 2)
    assert "open the line's current timetable" in research and "door-to-door estimate is not a train time" in research
    # round G: the train took 4 of 8 searches and all 3 page reads on a brief where the commute was one point of five
    assert "three in all, never the same page twice" in research
    assert "not the train's timetable" in writer_side
    assert "timetable" not in pipe._coverage({"topic": "Staging your home to sell", "key_points": ""}, 1)
    assert "Never report a passing status as a fact" in pipe._RESEARCH_SYSTEM and "the lot has 109 spaces" in pipe._RESEARCH_SYSTEM


# ----------------------------------------------------------------------------- 6. low-down-payment routes

def test_a_first_home_brief_asks_for_every_low_down_payment_route_we_hold():
    """11/15 same-brief rerun: HomeReady was named in one Ardmore article and left out of the next two, at the same
    writer setting - the fact is in every ledger, the mention was the writer's pick that day."""
    brief = {"topic": "First-time buyer guide to Ardmore", "key_points": "- First steps: preapproval and help with the down payment",
             "audiences": ["First-time buyers"]}
    writer_side = pipe._coverage(brief, 2)
    assert "HomeReady" in writer_side and "FHA" in writer_side and "VA (for those eligible)" in writer_side and "state housing agency" in writer_side
    assert "HomeReady" not in pipe._coverage(brief, 1)                       # already supplied: not a look-up
    assert any("HomeReady" in i["claim"] for i in tk.federal_items())        # and it is in every ledger
    assert "HomeReady" in pipe._coverage({"topic": "How much down payment do I need in Media?", "key_points": ""}, 2)
    assert "HomeReady" not in pipe._coverage({"topic": "Staging your home to sell", "key_points": ""}, 2)
    assert "\n\n" not in pipe._coverage(brief, 1) and not pipe._coverage(brief, 1).startswith("\n")


# ----------------------------------------------------------------------------- 9. the town card

def test_the_town_card_lists_the_stops_the_article_talks_about():
    card = {"slug": "gladwyne-pa", "nearby": {"transportation": ["Manayunk"], "parks": ["Gladwyne Park"]}}
    pipe.card_transit(card, {"walk": {"stations": ["Miquon", "Spring Mill", "", "Conshohocken", "Ivy Ridge"]}})
    assert card["nearby"]["transportation"] == ["Miquon", "Spring Mill", "Conshohocken"] and card["nearby"]["parks"] == ["Gladwyne Park"]
    untouched = {"slug": "x", "nearby": {"transportation": ["Manayunk"]}}
    pipe.card_transit(untouched, {"walk": {"stations": []}})
    pipe.card_transit(untouched, None)
    assert untouched["nearby"]["transportation"] == ["Manayunk"]
    pipe.card_transit({"slug": "y"}, {"walk": {"stations": ["A"]}})      # a card with no "nearby" block is left alone
