"""Grader round 10 (11/15 night: five typical articles written ON LIVE through the real screens - Wayne 88, Bryn Mawr
schools 91, Kennett Square 90, Radnor 80, Delaware County 78). What each finding turned out to be:

  A1     REAL. Radnor: the article said "the 32 listings that show an annual property tax" while the payment card said
         its tax estimate used "the 42 similar-priced homes for sale". Two bugs in the card: with too few tax bills near
         the example's price it fell back to EVERY listing but kept the words "similar-priced"; and it counted bills of
         any tax year while the article's fact uses one tax year.
  A2     REAL. Delaware County: "From Bryn Mawr, the 7:48 AM weekday train ..." - the right timetable, read one row off
         (7:48 is Rosemont). Clock times are now left on the timetable: the article gives the ride's length and how
         often trains run. Same family as phone numbers and township fees (owner 11/15: "just say where to get it").
  B1, B2 REAL. Wayne's $290,000 example sat among condos (12 fees, $352 median) and Kennett Square's among townhomes
         (38 fees, $215 median); the card left the fee out because the ARTICLE was not about condos.
  B3     NOT a defect: every personal line is word for word in the agent's own About text (read on live).
  B4, C2 On purpose, and the agent is told before writing ("We don't hold county-wide totals").
  B5, B6 Not defects (too few sales per municipality; the grader has no MLS access).
  Also   "1 was a twins" in a sold-homes fact; the writer tied the example's price to an unrelated number that equalled it.
No network and no database."""
import inspect
from collections import Counter

from app.services.ai import ai_blog_pipeline_service as pipe
from app.services.areas import market_stats_service as ms
from app.services.areas import town_knowledge_service as tk

RATE = {"rate_30": 7.11, "as_of": "2026-11-12"}
BUYER = {"topic": "Living in Delaware County, PA", "audiences": ["Buyers"], "key_points": "Getting around: trains and the commute"}


# ----------------------------------------------------------------------------- A1: whose tax bills the card used

def _radnor_like():
    """Twelve homes for sale: three cheap ones near the entry price (one tax bill between them), nine expensive ones.
    Eight bills are for tax year 2026, three for 2025."""
    rows = [(300_000, 3_600, "CONDO", "Unit/Flat/Apartment", "2026"), (310_000, None, "CONDO", "Unit/Flat/Apartment", None),
            (320_000, None, "TOWNHOUSE", "Interior Row/Townhouse", None)]
    rows += [(900_000 + i * 10_000, 10_800, "SINGLE_FAMILY", "Detached", "2026") for i in range(7)]
    rows += [(1_500_000, 18_000, "SINGLE_FAMILY", "Detached", "2025"), (1_600_000, 19_000, "SINGLE_FAMILY", "Detached", "2025")]
    return rows + [(310_000, 3_500, "CONDO", "Unit/Flat/Apartment", "2025")]


def test_one_tax_year_rule_is_shared_by_the_towns_tax_fact_and_the_payment_card():
    assert ms.bill_year(Counter({"2026": 8, "2025": 3}), 11) == "2026"
    assert ms.bill_year(Counter({"2027": 2, "2026": 30}), 32) == "2026"            # two early 2027 bills are not "the" year
    assert ms.bill_year(Counter({"2026": 3}), 3) is None                           # too few to pick a year: every bill counts
    assert "bill_year(years, len(taxes))" in inspect.getsource(ms.get_area_cost_profile)
    assert "bill_year(years" in inspect.getsource(ms.entry_profile)
    assert "return entry_profile(" in inspect.getsource(ms.entry_price_profile)


def test_with_too_few_bills_near_the_price_the_card_says_what_it_really_used():
    entry = ms.entry_profile(_radnor_like())
    assert entry["homes"] == 13 and entry["tax_scope"] == "all"
    assert entry["tax_homes"] == 8                                                  # the 2026 bills only - the same 8 the article's fact counts
    ex = pipe.payment_example(entry, RATE, ["Radnor"], basis="entry")
    assert ex["tax_basis"] == "tax bills of the 8 homes for sale that list one, scaled to this price" and "similar-priced" not in ex["tax_basis"]
    # ... and "similar-priced" stays where it is true
    near = [(300_000 + i * 5_000, 3_600 + i * 50, "CONDO", "Unit/Flat/Apartment", "2026") for i in range(12)]
    entry2 = ms.entry_profile(near)
    assert entry2["tax_scope"] == "near" and entry2["tax_homes"] <= 12
    assert pipe.payment_example(entry2, RATE, ["Wayne"], basis="entry")["tax_basis"] == f"tax bills of the {entry2['tax_homes']} similar-priced homes for sale, scaled to this price"
    assert pipe.tax_basis_words("house", 6, "near") == "tax bills of the 6 similar-priced single-family houses for sale, scaled to this price"
    assert pipe.tax_basis_words("attached", 14, "all") == "tax bills of the 14 condos and townhomes for sale that list one, scaled to this price"
    assert pipe.tax_basis_words("entry", None, None) == "tax bills of similar-priced homes for sale, scaled to this price"         # an entry made before this change


def test_a_bill_counts_in_the_card_exactly_when_it_counts_in_the_towns_fact():
    rows = [(400_000 + i * 1_000, 4_800, "CONDO", "Unit/Flat/Apartment", "2026") for i in range(10)]
    rows += [(405_000, 50, "CONDO", "Unit/Flat/Apartment", "2026"),                 # a $50 "bill" is a typo, not a bill
             (406_000, 4_800, "CONDO", "Unit/Flat/Apartment", "2025"),              # last year's bill
             (407_000, 4_800, "CONDO", "Unit/Flat/Apartment", None)]                # no tax year: left out once a year is chosen
    entry = ms.entry_profile(rows)
    assert entry["homes"] == 13 and entry["tax_homes"] == 10 and entry["tax_scope"] == "near"
    # a manufactured home is not a single-family house here either
    houses = [(500_000 + i * 1_000, 6_000, "SINGLE_FAMILY", "Detached", "2026") for i in range(10)] + [(90_000, 900, "SINGLE_FAMILY", "Manufactured", "2026")]
    assert ms.entry_profile(houses)["houses"] == 10
    assert ms.entry_profile(rows[:5]) == {}                                         # under ten homes for sale: no example


# ----------------------------------------------------------------------------- B1, B2: the association fee on an entry-price example

WAYNE = {"p25": 295_000, "homes": 46, "tax_pct_near_p25": 1.19, "tax_homes": 10, "tax_scope": "near", "near_homes": 11, "near_condos": 9, "near_townhomes": 1}
WAYNE_COST = {"hoa": {"condo": {"homes": 12, "median_monthly": 352}, "townhouse": {"homes": 6}}}


def test_an_entry_price_example_among_condos_adds_their_fee():
    fees = pipe.example_fees(WAYNE_COST)
    assert fees == {"condo": {"monthly": 352, "homes": 12, "label": "condos"}}       # six townhome fees are not a typical figure
    assert pipe.example_fee(WAYNE_COST) == fees["condo"]                              # the attached-home example: unchanged
    ex = pipe.payment_example(WAYNE, RATE, ["Wayne"], basis="entry", fees=fees)
    assert ex["fee"]["monthly"] == 352 and ex["fee"]["near_attached"] == 10 and ex["fee"]["near"] == 11
    assert all(sc["with_fee"] == sc["monthly_before_insurance"] + 352 for sc in ex["scenarios"])
    (line,) = [i["claim"] for i in pipe.payment_example_items(ex) if "association fee" in i["claim"]]
    assert line.startswith("Payment example with the association fee: 10 of the 11 homes for sale near this price are condos or townhomes, so the example adds a fee. The 12 condos")
    assert "$352/month" in line and "A home's own fee can be higher or lower." in line


def test_the_fee_is_added_only_when_it_fits_the_homes_near_the_price():
    fees = pipe.example_fees(WAYNE_COST)
    mixed = {**WAYNE, "near_condos": 4, "near_townhomes": 2}                          # 6 of 11: not "most"
    assert pipe.payment_example(mixed, RATE, ["Wayne"], basis="entry", fees=fees)["fee"] is None
    towns = {**WAYNE, "near_condos": 2, "near_townhomes": 8}                          # mostly townhomes, whose fee we can't state
    assert pipe.payment_example(towns, RATE, ["Wayne"], basis="entry", fees=fees)["fee"] is None
    assert pipe.payment_example({**WAYNE, "near_homes": 3, "near_condos": 3, "near_townhomes": 0}, RATE, ["Wayne"], basis="entry", fees=fees)["fee"] is None
    assert pipe.payment_example(WAYNE, RATE, ["Wayne"], basis="entry")["fee"] is None                       # no fee on file
    old = {k: v for k, v in WAYNE.items() if not k.startswith("near_")}               # an entry made before this change
    assert pipe.payment_example(old, RATE, ["Wayne"], basis="entry", fees=fees)["fee"] is None
    kennett = {"p25": 545_000, "homes": 77, "tax_pct_near_p25": 1.58, "tax_homes": 19, "tax_scope": "near", "near_homes": 30, "near_condos": 0, "near_townhomes": 24}
    ex = pipe.payment_example(kennett, RATE, ["Kennett Square"], basis="entry", fees=pipe.example_fees({"hoa": {"townhouse": {"homes": 39, "median_monthly": 215}}}))
    assert ex["fee"]["label"] == "townhomes" and ex["fee"]["monthly"] == 215
    # a single-family example never adds one
    house = {**WAYNE, "house_median": 1_050_000, "houses": 25, "tax_pct_near_house": 0.94, "tax_homes_house": 11}
    assert pipe.payment_example(house, RATE, ["Wayne"], basis="house", fee=fees["condo"], fees=fees)["fee"] is None
    run = inspect.getsource(pipe._run)
    assert "fees=example_fees(dossiers[0].get(\"cost\")) if len(dossiers) == 1 else None" in run          # one town only: a fee is a town's own fact


# ----------------------------------------------------------------------------- A2: clock times stay on the timetable

TRAIN = ("Radnor and Bryn Mawr both have stations on SEPTA's Paoli/Thorndale Line (Regional Rail). Weekday mornings, trains leave Radnor for "
         "Center City about every 20-40 minutes. The 7:43 AM reaches Suburban Station at 8:28, a 45-minute ride.\n"
         "- From Bryn Mawr, the 7:48 AM weekday train reaches Suburban Station at 8:28, about 40 minutes.\n"
         "- On weekends and major holidays, Center City-bound trains leave Radnor about once an hour, at :51 past the hour. They reach "
         "Suburban Station at :30, about 39 minutes later.")


def test_a_train_clock_time_is_found_and_everything_else_is_left_alone():
    found = pipe.clock_time_claims(TRAIN, BUYER)
    assert found == ["The 7:43 AM reaches Suburban Station at 8:28, a 45-minute ride.",
                     "From Bryn Mawr, the 7:48 AM weekday train reaches Suburban Station at 8:28, about 40 minutes.",
                     "On weekends and major holidays, Center City-bound trains leave Radnor about once an hour, at :51 past the hour.",
                     "They reach Suburban Station at :30, about 39 minutes later."]
    for fine in ("Radnor and Bryn Mawr are stops on SEPTA's Paoli/Thorndale Line, about 40-45 minutes to Suburban Station on weekday morning trains.",
                 "Weekday mornings, trains leave Radnor for Center City about every 20-40 minutes.",
                 "The timetable effective October 18, 2026 has the times (https://schedules.septa.org/current/PAO.pdf).",
                 "At 7.11% on a 30-year loan the payment is about $2,940 a month, and 3 in 10 homes near the station sold above asking.",
                 "My open house runs Saturday from 1:00 to 3:00 PM.",                           # no train in the sentence
                 "The station lot has 109 spaces and a 24-hour limit."):
        assert pipe.clock_time_claims(fine, BUYER) == [], fine
    # a time the agent typed themselves is theirs
    mine = {**BUYER, "personal_take": "I take the 7:15 from Wayne most mornings."}
    assert pipe.clock_time_claims("I take the 7:15 train from Wayne most mornings.", mine) == []
    assert pipe.clock_time_claims("I take the 7:15 train from Wayne most mornings.", BUYER) != []


def test_a_clock_time_is_a_code_confirmed_problem_and_is_settled_before_saving():
    article = {"title": "Living in Delaware County", "intro": ["Pick the town first."], "sections": [{"heading": "Getting around", "blocks": [
        {"type": "paragraph", "text": "Radnor and Bryn Mawr both have stations on SEPTA's Paoli/Thorndale Line. Weekday mornings, trains leave Radnor "
                                      "for Center City about every 20-40 minutes, and the ride to Suburban Station takes about 40-45 minutes. "
                                      "From Bryn Mawr, the 7:48 AM weekday train reaches Suburban Station at 8:28, about 40 minutes."}]}]}
    find = lambda: pipe.code_problems(pipe._article_text(article), brief=BUYER, protections=[], ledger=[])      # noqa: E731
    (p,) = [x for x in find() if x["kind"] == "clock_time"]
    assert p["pass"] == "fine_print" and "NO clock time" in p["problem"] and "7:48" in p["quote"]
    n, fixed = pipe.settle_leftovers(article, find)
    text = article["sections"][0]["blocks"][0]["text"]
    assert n == 1 and fixed[0]["fixed"] and "7:48" not in text and "8:28" not in text
    assert "about 40-45 minutes" in text and "Paoli/Thorndale Line" in text                     # what a commuter needs is still there
    assert [x for x in find() if x["kind"] == "clock_time"] == []


def test_the_writer_and_the_research_desk_are_told_to_leave_clock_times_on_the_timetable():
    assert "never a departure or arrival clock time" in pipe._ENGINE_RULES and "one misread row sends a commuter to the wrong train" in pipe._ENGINE_RULES
    assert "not departure or arrival clock times: the article does not print them" in pipe._RESEARCH_SYSTEM
    ask = " ".join(" ".join(str(x) for x in row[1:]) for row in pipe._COVERAGE
                   if hasattr(row[0], "search") and row[0].search("Getting to Center City by train"))
    assert "do NOT report departure or arrival clock times" in ask and "Never a departure or arrival clock time, even when the ledger holds one" in ask
    assert "report one weekday morning trip (departure, arrival, minutes)" not in ask
    assert pipe._SETTLED["clock_time"] == "A train or bus clock time"


# ----------------------------------------------------------------------------- C4: Wikipedia is not an official page

def test_an_encyclopedia_anyone_can_edit_is_not_an_official_source():
    for wiki in ("https://en.wikipedia.org/wiki/Radnor_Township_School_District", "https://en.wikipedia.org/wiki/Shipley_School",
                 "https://commons.wikimedia.org/wiki/File:X.jpg", "https://septa.fandom.com/wiki/Villanova"):
        assert not pipe.official_source(wiki), wiki
    for real in ("https://www.rtsd.org/our-district/school-boundaries", "https://www.shipleyschool.org/about", "https://www.radnor.com.us/x",
                 "https://www.pa.gov/agencies/revenue", "https://www.haverford.k12.pa.us/"):
        assert pipe.official_source(real), real
    stored = [{"origin": "idx_research", "topic": "schools", "claim": "Radnor High School ...", "source_url": "https://en.wikipedia.org/wiki/Radnor_Township_School_District"},
              {"origin": "idx_research", "topic": "schools", "claim": "Radnor Township School District has ...", "source_url": "https://www.rtsd.org/our-district"},
              {"origin": "idx_research", "topic": "transit", "claim": "Villanova station ...", "source_url": "https://en.wikipedia.org/wiki/Villanova_station"}]
    kept = pipe.trusted_stored_research(stored)
    assert [i["source_url"] for i in kept] == [stored[1]["source_url"], stored[2]["source_url"]]      # a school fact needs the school's own page
    # ... and a stored fact's page is shown to the reader only when it is official
    assert '(s.get("origin") != "idx_research" or official_source(s["url"]))' in inspect.getsource(pipe._run)


# ----------------------------------------------------------------------------- also

def test_one_twin_is_a_twin_and_the_examples_price_keeps_its_own_reason():
    one = tk.attached_styles_note({"twin": {"count": 1}, "row": {"count": 25, "median_close_price": 510_000}})
    assert "1 was a twin (a house that shares one side wall with its neighbor) (too few" in one and "a twins" not in one
    six = tk.attached_styles_note({"twin": {"count": 6, "median_close_price": 460_000}, "row": {"count": 1}})
    assert "6 were twins (a house" in six and "1 was a row townhome " in six + " "
    assert "never tie the price to another ledger number that happens to equal it" in inspect.getsource(pipe._run)
