"""Grader round 7 (11/15, four Save usage articles: Media 80 / Doylestown 84 / Malvern 87 / Ardmore 80).

What was checked against the sources by eye, and what it showed:
  8, 9   the two "wrong train time" findings were the reviewer's misreading - SEPTA's own timetables (PAO.pdf and
         LAN.pdf, effective Oct 18, 2026) show Ardmore 6:57 -> Suburban 7:28 and Doylestown 6:09 -> 7:36 (87 min),
         exactly as the articles said. Nothing to fix; nothing here.
  6      REAL. Middletown Township's sewer certification (required before any sale) was missing from a Media seller
         article although a Media article written the day before had found it. A municipality's own rule had no home
         in the fact store, and research stopped with 6 of its 8 searches unspent.
  7      REAL. Media Borough's page gives two deadlines (application 20 days before settlement, inspection call 15
         days before); the stored fact carried one.
  10     REAL. A $680,000 example beside "ask about FHA": the loan is over the county's FHA limit and nothing said so.
  11, 12 REAL. A downsizing article got a $1,080,000 single-family example; an attached-home example left out an
         association fee the town's own listings give.
No network and no database."""
import asyncio
from datetime import date
from types import SimpleNamespace

from app.services.ai import ai_blog_pipeline_service as pipe
from app.services.areas import town_knowledge_service as tk

MEDIA = {"town": "Media", "townships": [
    {"name": "Middletown Township", "share": 39, "sort": 38.6, "county": "Delaware County", "kind": "township"},
    {"name": "Upper Providence Township", "share": 23, "sort": 22.8, "county": "Delaware County", "kind": "township"},
    {"name": "Media Borough", "share": 16, "sort": 16.5, "county": "Delaware County", "kind": "place"},
    {"name": "Nether Providence Township", "share": 10, "sort": 9.6, "county": "Delaware County", "kind": "township"},
    {"name": "other municipalities (Edgmont Township, Rose Valley Borough)", "share": 1, "sort": 1.5, "county": None, "kind": "small"}]}
SEWER = {"topic": "regulation", "town": "Middletown Township", "source_url": "https://middletowndelcopa.gov/sewer",
         "claim": "Middletown Township (Delaware County): the Sewer Authority requires inspection and certification of the building sewer before the "
                  "sale, transfer or refinancing of a property; the process takes a minimum of two weeks."}


# ----------------------------------------------------------------------------- 6. a municipality's rule has a home

def test_the_articles_municipalities_are_listed_largest_first_without_the_grouped_row():
    munis = tk.municipalities([MEDIA])
    assert [m["name"] for m in munis] == ["Middletown Township", "Upper Providence Township", "Media Borough", "Nether Providence Township"]
    assert tk.muni_scope("pa", munis[0]) == "muni:PA:delaware:middletown township"
    # the same township name in another county is another place
    assert tk.muni_scope("PA", {"name": "Middletown Township", "county": "Bucks County"}) == "muni:PA:bucks:middletown township"
    assert tk.municipalities([]) == [] and tk.municipalities([{"townships": None}]) == []
    # a township and a borough of the same name are two municipalities with two sets of rules (Doylestown has both)
    both = tk.municipalities([{"townships": [{"name": "Doylestown Township", "share": 33, "county": "Bucks County", "kind": "township"},
                                              {"name": "Doylestown Borough", "share": 12, "county": "Bucks County", "kind": "place"}]}])
    assert [m["name"] for m in both] == ["Doylestown Township", "Doylestown Borough"] and tk.muni_scope("PA", both[0]) != tk.muni_scope("PA", both[1])


def test_a_fact_is_filed_under_the_one_municipality_it_is_about():
    munis = tk.municipalities([MEDIA])
    assert tk.muni_of(SEWER, munis)["name"] == "Middletown Township"                          # by its town
    by_claim = {"town": "general", "claim": "Media Borough's Certificate of Occupancy procedure says to schedule the inspection 15 days before settlement."}
    assert tk.muni_of(by_claim, munis)["name"] == "Media Borough"                             # by the only one its claim names
    assert tk.muni_of({"town": "general", "claim": "Upper Providence Township requires a U&O certificate."}, munis)["name"] == "Upper Providence Township"
    # the mailing town is not the borough of the same name; two municipalities in one claim is not one municipality's fact
    assert tk.muni_of({"town": "Media", "claim": "Media has a walkable downtown along State Street."}, munis) is None
    assert tk.muni_of({"town": "general", "claim": "Middletown Township and Media Borough both adopted budgets."}, munis) is None
    assert tk.muni_of({"town": "general", "claim": "Pennsylvania's realty transfer tax is 1% to the state."}, munis) is None
    assert tk.muni_of(SEWER, []) is None


def test_another_towns_township_rule_stops_reaching_every_article_in_the_state():
    stored = [{"claim": "Pennsylvania's realty transfer tax: 1% to the state plus a local share.", "topic": "taxes"},
              {"claim": "Upper Providence Township requires a Use & Occupancy (U&O) certificate for the re-sale of a home.", "topic": "regulation"},
              {"claim": "Lower Merion Township requires a certificate before settlement.", "topic": "regulation"}]
    general, local = tk.split_state_items(stored, tk.municipalities([MEDIA]))
    assert [i["topic"] for i in general] == ["taxes"]
    assert [(i["municipality"], i["claim"][:16]) for i in local] == [("Upper Providence Township", "Upper Providence")]
    # an article about somewhere else (or about a whole region) gets neither township's rule
    general, local = tk.split_state_items(stored, [])
    assert len(general) == 1 and local == []
    assert [i["claim"] for i in tk.merge_items([{"claim": "A  fact."}], [{"claim": "a fact."}, {"claim": "Another."}])] == ["A  fact.", "Another."]


class _Db:
    """shared_knowledge, in memory: rows by scope."""
    def __init__(self):
        self.rows = {}

    async def execute(self, stmt):
        wanted = next(iter(stmt.compile().params.values()), None)
        wanted = list(wanted) if isinstance(wanted, (list, tuple, set)) else [wanted]
        rows = [self.rows[s] for s in wanted if s in self.rows]
        return SimpleNamespace(scalar_one_or_none=lambda: rows[0] if rows else None, scalars=lambda: SimpleNamespace(all=lambda: rows))

    def add(self, row):
        self.rows[row.scope] = row

    async def commit(self):
        pass


def test_what_research_finds_about_a_township_is_there_for_the_next_article():
    """The 11/14 Media article found Middletown's sewer certification; the 11/15 one did not have it."""
    db, munis = _Db(), tk.municipalities([MEDIA])
    middletown = tk.muni_of(SEWER, munis)
    assert asyncio.run(tk.save_muni_items(db, "PA", middletown, [SEWER])) == 1
    assert asyncio.run(tk.save_muni_items(db, "PA", middletown, [SEWER])) == 0                 # once
    assert list(db.rows) == ["muni:PA:delaware:middletown township"]                                   # a county in the claim no longer loses it
    held = asyncio.run(tk.muni_items(db, "PA", munis))
    assert len(held) == 1 and held[0]["municipality"] == "Middletown Township" and "Sewer Authority" in held[0]["claim"]
    assert held[0]["origin"] == "state_bank" and held[0]["town"] == "general"                  # read downstream like any stored official fact
    # an article about another town's Middletown (Bucks County), or with no municipalities, does not get it
    assert asyncio.run(tk.muni_items(db, "PA", [{"name": "Middletown Township", "county": "Bucks County"}])) == []
    assert asyncio.run(tk.muni_items(db, "PA", [])) == []
    assert asyncio.run(tk.save_muni_items(db, "PA", middletown, [{"claim": "no source"}])) == 0


# ----------------------------------------------------------------------------- 6. research that stops early

def test_research_that_stopped_early_gets_its_unspent_look_ups_for_what_it_left_unchecked():
    not_found = ["Middletown Township: no resale certificate requirement for existing homes; smoke/CO or sewer lateral resale certificates not checked beyond FAQ",
                 "resale requirements not checked for Marple Township", "Official tax-rate pages for the districts and townships not located"]
    lookups = ["web_search: Middletown Township Delaware County resale certificate", "web_search: Nether Providence Township use and occupancy"]
    again = pipe.research_follow_up(not_found, lookups, 8, 3)
    assert again == {"gaps": not_found[:2], "searches": 6, "fetches": 3}                       # the article's total budget is unchanged
    msg = pipe.follow_up_message(again, [{"claim": "Nether Providence requires a U&O certificate."}])
    assert "SECOND PASS" in msg and "6 searches and 3 page reads unused" in msg and "not checked for Marple Township" in msg
    assert "ALREADY FOUND IN THE FIRST PASS" in msg and "Nether Providence requires" in msg and "not located" not in msg
    # no second pass when the budget was used, or when nothing was left unchecked
    used = [f"web_search: q{i}" for i in range(7)] + ["web_fetch: https://x"]
    assert pipe.research_follow_up(not_found, used, 8, 3) is None
    assert pipe.research_follow_up(["Official tax-rate pages not located"], lookups, 8, 3) is None
    assert pipe.research_follow_up([], [], 8, 3) is None and pipe.research_follow_up(not_found, None, 1, 0) is None


def test_research_is_told_to_finish_the_job_and_to_look_past_the_occupancy_certificate():
    rules = pipe._RESEARCH_SYSTEM
    assert "Report EVERY deadline, lead time and fee that page gives" in rules                 # 7: 20 days AND 15 days
    assert "Never stop while a REQUIRED LOOK-UP is still unchecked" in rules
    seller = pipe._coverage({"topic": "Selling your Media home this winter", "audiences": ["Sellers"]}, 1)
    assert "sewer or water authority" in seller and "never report a municipality as having no resale requirement on the strength of one FAQ line" in seller


# ----------------------------------------------------------------------------- 12. the example fits the reader

ENTRY = {"p25": 705000, "homes": 84, "attached_median": 690000, "attached": 41, "house_median": 1250000, "houses": 38,
         "tax_pct_near_attached": 1.1, "tax_homes_attached": 12, "tax_pct_near_p25": 1.2, "tax_homes": 20, "tax_pct_near_house": 1.0, "tax_homes_house": 9}
RATE = {"rate_30": 7.11, "as_of": "2026-11-12"}


def test_a_downsizer_is_shown_the_smaller_home_not_the_big_house_they_are_leaving():
    malvern = {"topic": "Downsizing in Malvern: Smaller Homes, 55+ Communities and How to Avoid Moving Twice",
               "goal": "Understand the smaller-home choices in Malvern and how to time selling the big house with buying the next one",
               "audiences": ["Sellers", "Homeowners"]}
    assert pipe.payment_basis(malvern) == "attached"
    assert pipe.payment_basis({"topic": "Empty nesters: right-sizing in Wayne"}) == "attached"
    # unchanged for everyone else
    assert pipe.payment_basis({"topic": "Moving up to a single-family house in Wayne"}) == "house"
    assert pipe.payment_basis({"topic": "First-time buyer guide to Ardmore", "audiences": ["First-time buyers"]}) == "entry"
    ex = pipe.payment_example(ENTRY, RATE, ["Malvern"], basis=pipe.payment_basis(malvern), sold_house={"price": 1080000, "count": 27, "window": "w"})
    assert ex["price"] == 690000 and "41 condos and townhomes" in ex["price_basis"]


# ----------------------------------------------------------------------------- 11. the association fee

def test_an_attached_home_example_adds_the_towns_median_association_fee():
    cost = {"hoa": {"townhouse": {"homes": 40, "of": 40, "median_monthly": 318}, "condo": {"homes": 12, "of": 14, "median_monthly": 410}}}
    fee = pipe.example_fee(cost)
    assert fee == {"monthly": 318, "homes": 40, "label": "townhomes"}                         # the type the town has more of
    assert pipe.example_fee({"hoa": {"condo": {"homes": 1, "median_monthly": 382}}}) is None    # one listing is not a typical fee
    assert pipe.example_fee({"hoa_few": {"townhouse": {"homes": 3}}}) is None and pipe.example_fee(None) is None
    ex = pipe.payment_example(ENTRY, RATE, ["Malvern"], basis="attached", fee=fee)
    assert ex["fee"] == fee and all(sc["with_fee"] == sc["monthly_before_insurance"] + 318 for sc in ex["scenarios"])
    (line,) = [i["claim"] for i in pipe.payment_example_items(ex) if "association fee" in i["claim"]]
    assert "the 40 townhomes for sale that list a monthly fee show a median of $318/month" in line and "still before homeowners insurance" in line
    assert f"${ex['scenarios'][0]['with_fee']:,}/month at 5% down" in line
    # a house example, or the cheapest-quarter example (a mix of types), is not handed a townhome's fee
    for basis in ("house", "entry"):
        other = pipe.payment_example(ENTRY, RATE, ["Malvern"], basis=basis, fee=fee)
        assert other["fee"] is None and "with_fee" not in other["scenarios"][0]
        assert not any("association fee" in i["claim"] for i in pipe.payment_example_items(other))


# ----------------------------------------------------------------------------- 10. programs at the example's price

def test_the_example_says_when_its_price_is_past_fhas_reach():
    fha = tk.fha_limits(date(2026, 11, 15))
    assert (fha["floor"], fha["ceiling"]) == (541_287, 1_249_125) and "hud.gov" in fha["source_url"]
    assert tk.fha_limits(date(2027, 1, 2)) is None                                             # last year's limits are never used
    doylestown = {**ENTRY, "p25": 680000}
    ex = pipe.payment_example(doylestown, RATE, ["Doylestown"], basis="entry", fha=fha)
    assert ex["fha"]["loan"] == 656_200 and ex["fha"]["over"] == "some"
    (line,) = [i for i in pipe.payment_example_items(ex) if "Loan programs at the payment example's price" in i["claim"]]
    assert "needs a loan of about $656,200" in line["claim"] and "FHA may not be available at this price here" in line["claim"]
    assert "State housing-agency programs also have purchase-price and income limits" in line["claim"] and line["source_url"] == fha["source_url"]
    assert "541,287" not in line["claim"] and "630,200" not in line["claim"]                   # no limit we do not hold for this county is stated
    # a price FHA covers in every county: nothing is added
    cheap = pipe.payment_example({**ENTRY, "p25": 520000}, RATE, ["Ardmore"], basis="entry", fha=fha)
    assert cheap["fha"]["over"] is None and not any("Loan programs" in i["claim"] for i in pipe.payment_example_items(cheap))
    # a price past FHA's highest limit anywhere
    high = pipe.payment_example({**ENTRY, "p25": 1_400_000}, RATE, ["Gladwyne"], basis="entry", fha=fha)
    assert high["fha"]["over"] == "everywhere"
    assert any("not an option at this price" in i["claim"] for i in pipe.payment_example_items(high))
    # no limits on file (after their year): the example is as before
    plain = pipe.payment_example(doylestown, RATE, ["Doylestown"], basis="entry")
    assert plain["fha"] is None and not any("Loan programs" in i["claim"] for i in pipe.payment_example_items(plain))
    buyer = pipe._coverage({"topic": "Buying your first home in Doylestown", "audiences": ["First-time buyers"]}, 2)
    assert "may not be available at the payment example's price" in buyer


# ----------------------------------------------------------------------------- 7. a stored fact with one of two deadlines

def test_a_procedure_page_read_under_the_old_rule_is_read_once_more():
    old = {"municipality": "Media Borough", "saved_at": "2026-11-14T20:00:00+00:00", "source_url": "https://www.mediaborough.com/DocumentCenter/View/139",
           "claim": "Media Borough's procedure says to contact the Borough to schedule the inspection at least 15 days prior to settlement."}
    no_deadline = {**old, "source_url": "https://www.mediaborough.com/165", "claim": "Media Borough requires a Certificate of Occupancy at each change of ownership."}
    new = {**old, "saved_at": "2026-11-15T12:00:00+00:00", "source_url": "https://example.gov/new"}
    assert tk.recheck_pages([old, no_deadline, new, dict(old)]) == [{"municipality": "Media Borough", "url": old["source_url"]}]
    assert tk.recheck_pages([{**old, "rechecked_at": "2026-11-15T13:00:00+00:00"}]) == [] and tk.recheck_pages([]) == []
    # reading the page again marks what we held from it, so it is not read a third time
    db, munis = _Db(), tk.municipalities([MEDIA])
    borough = next(m for m in munis if m["name"] == "Media Borough")
    assert asyncio.run(tk.save_muni_items(db, "PA", borough, [{**old, "topic": "regulation"}])) == 1
    row = db.rows[tk.muni_scope("PA", borough)]
    row.items[0]["saved_at"] = "2026-11-14T20:00:00+00:00"                                   # as if it had been stored under the old rule
    assert len(tk.recheck_pages(asyncio.run(tk.muni_items(db, "PA", munis)))) == 1
    both = {"topic": "regulation", "source_url": old["source_url"], "claim": "Media Borough: mail the application and check at least 20 days before "
            "settlement, and call to schedule the inspection at least 15 days before settlement."}
    assert asyncio.run(tk.save_muni_items(db, "PA", borough, [both])) == 1
    held = asyncio.run(tk.muni_items(db, "PA", munis))
    assert len(held) == 2 and tk.recheck_pages(held) == [] and any("20 days" in i["claim"] for i in held)


# ----------------------------------------------------------------------------- what the first real run showed (11/15)

def test_the_article_keeps_one_list_of_its_municipalities_with_their_counties():
    """The Media proof article stored every township fact it found under "muni:PA::middletown township" - no county -
    because a seller article re-used the name `munis` for its short look-up list. Nothing read that key."""
    import inspect
    src = inspect.getsource(pipe._run)
    assert src.count("munis = ") == 1 and "munis = [] if regional else tk.municipalities(profiles)" in src
    assert "tk.muni_of(i, munis)" in src and "for m in to_check" in src
    for m in tk.municipalities([MEDIA]):
        assert tk.muni_scope("PA", m).split(":")[2] == "delaware"


def test_moving_an_old_fact_is_not_a_fresh_read_of_its_page():
    db, munis = _Db(), tk.municipalities([MEDIA])
    nether = next(m for m in munis if m["name"] == "Nether Providence Township")
    url = "https://ecode360.com/11541899"
    first = {"topic": "regulation", "source_url": url, "saved_at": "2026-11-14T20:00:00+00:00",
             "claim": "Nether Providence Township Code Chapter 103 requires the application at least 30 days before occupancy."}
    moved = {"topic": "regulation", "source_url": url, "saved_at": "2026-11-15T07:20:00+00:00",
             "claim": "Nether Providence Township Code Chapter 103 makes the certificate the owner's duty."}
    asyncio.run(tk.save_muni_items(db, "PA", nether, [first]))
    asyncio.run(tk.save_muni_items(db, "PA", nether, [moved]))
    held = asyncio.run(tk.muni_items(db, "PA", munis))
    assert len(held) == 2 and not any(i.get("rechecked_at") for i in held)                  # still due its one more read
    assert [i["saved_at"][:10] for i in held] == ["2026-11-14", "2026-11-15"]                # each keeps the day it was read
    fresh = {"topic": "regulation", "source_url": url, "claim": "Nether Providence Township: file at least 30 days before occupancy; the fee is set by resolution."}
    asyncio.run(tk.save_muni_items(db, "PA", nether, [fresh]))
    assert tk.recheck_pages(asyncio.run(tk.muni_items(db, "PA", munis))) == []                # read again today: done


def test_the_writer_may_give_every_lead_time_the_municipality_gives():
    seller = pipe._coverage({"topic": "Selling your Media home this winter", "audiences": ["Sellers"]}, 2)
    assert "state EVERY one that item gives" in seller and "20 days before settlement AND call to schedule the inspection 15 days" in seller
    assert "The ONE timing you may state" not in seller and "never a lead time the ledger doesn't give" in seller
