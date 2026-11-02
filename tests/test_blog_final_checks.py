"""Round-5 blog safeguards (11/01): the code-level checks every article gets in its one run."""
from app.services.ai import blog_final_checks as bfc
from app.services.ai import ai_blog_writer_service as writer
from app.services.areas import town_knowledge_service as tk


def kinds(text, brief=None):
    return [h["kind"] for h in bfc.final_lint(text, brief or {})]


# --- fine print -------------------------------------------------------------------------------
def test_per_100_tax_rate_is_flagged():
    assert kinds("The city's rate is $0.7055 per $100 of assessed value.") == ["rule_detail"]
    assert kinds("Homes listed at $390,000 per the payment example.") == []


def test_millage_rate_is_flagged():
    assert kinds("Manheim Township School District's tax rate for 2026-27 is 18.1421 mills.") == ["rule_detail"]


def test_transfer_tax_rate_is_flagged():
    assert kinds("Maryland's state transfer tax is 0.5% of the price.") == ["rule_detail"]


# Round 9 grader (Princeton): a tax bill as a percent of price reads like an official tax rate.
def test_property_tax_as_a_percent_is_flagged():
    princeton = ("Among homes for sale on November 1, 2026 that list a tax bill, the median was $16,234 for tax year 2025 as entered on the listings. "
                 "That's about 1.65% of list price. By municipality, the median ran about 1.52% of list price in Hopewell Township, 1.54% in Princeton "
                 "and 1.87% in Plainsboro Township.")
    hits = bfc.final_lint(princeton, {})
    assert [h["sentence"][:12] for h in hits] == ["That's about", "By municipal"]
    assert "very different tax bills" in hits[0]["fix"]
    assert kinds("Median annual tax is about 1.6% of the sale price in Radnor.") == ["rule_detail"]
    assert kinds("Property taxes in Lawrence Township ran about 1.59% a year.") == ["rule_detail"]


def test_tax_bill_in_dollars_sale_to_list_and_loan_percents_are_fine():
    assert kinds("The 151 homes for sale showed a median tax bill of $6,789 for tax year 2025.") == []
    assert kinds("Property taxes vary by municipality. Homes sold for a median of 101% of list price.") == []
    assert kinds("At 7.24% with 10% down, the payment including property tax is about $11,587 a month.") == []
    assert kinds("Tax bills vary. About 40% of the homes for sale are condos.") == []


# Round 9 grader (Frederick): the Treasurer, not the clerk, collects recordation tax - leave procedure out.
def test_which_office_collects_closing_taxes_is_flagged():
    hits = bfc.final_lint("When the deed is recorded, Maryland's state transfer tax and Frederick County's recordation tax are due, "
                          "and the county clerk collects recording fees and taxes at that point.", {})
    assert [h["kind"] for h in hits] == ["rule_detail"] and "itemized estimate" in hits[0]["fix"]
    assert kinds("Recordation tax is paid to the county treasurer before recording.") == ["rule_detail"]
    assert kinds("Maryland transfer taxes, recordation taxes and recording fees can affect your closing costs. "
                 "Ask your settlement company or attorney for an itemized estimate based on the specific property.") == []


def test_program_limits_and_deadlines_are_flagged():
    assert kinds("The program has an income limit of $145,000 for a household of two.") == ["rule_detail"]
    assert kinds("You can get up to $10,000 in down payment assistance.") == ["rule_detail"]
    assert kinds("The buyer can cancel within 7 days after receiving the resale documents.") == ["rule_detail"]
    # Days on market isn't a legal deadline.
    assert kinds("Half of those homes found a buyer within 17 days.") == []


# --- statistics -------------------------------------------------------------------------------
def test_half_above_half_below_reading_of_a_median_is_flagged():
    assert "stat_logic" in kinds("The median sale-to-list ratio was 100%, so half of homes sold above asking and half below.")
    assert kinds("The median sale-to-list ratio was 100%, meaning the midpoint of sales was right at asking price.") == []


# --- condo vs townhome ------------------------------------------------------------------------
def test_condo_or_townhome_inspection_line_is_flagged():
    assert "condo_townhome" in kinds("With a condo or townhome, the inspection only covers part of what you're buying.")
    assert kinds("With a condo, the inspection only covers your unit.") == []


# --- the agent's own experience ---------------------------------------------------------------
def test_story_the_agent_never_gave_is_flagged_and_their_own_is_kept():
    assert kinds("I've seen buyers lose a house over a late inspection.") == ["invented_experience"]
    brief = {"personal_take": "Check the district for the exact address - I've seen buyers get that wrong."}
    assert kinds("Check the district first - I've seen buyers get that wrong.", brief) == []


# --- the agent's own closing ------------------------------------------------------------------
def test_agents_own_closing_is_never_flagged_in_any_state():
    # Owner 11/01: out-of-area articles keep "I'll start your search" (a referral agent may take it).
    line = "Send me your price range and I'll start your home search."
    assert kinds(line, {"areas": ["Annapolis, MD"]}) == []
    assert bfc.code_checks(line, {}, [], "Peter Stefanatos") == {}
    assert writer.cta_block_args({"cta": "buying"}) == ("I'm Buying", "buying")
    assert writer.cta_block_args({"cta": "showing"}) == ("Schedule a Showing", "showing")


# --- reader protections -----------------------------------------------------------------------
def test_maryland_first_time_condo_buyer_gets_the_right_protections():
    brief = {"topic": "A first-time buyer's guide to Annapolis", "audiences": ["First-time buyers", "Buyers"],
             "key_points": "What condos, townhomes and houses cost"}
    keys = {p["key"] for p in bfc.protections_for({"MD"}, brief)}
    assert {"md_first_time_taxes", "md_condo_resale", "md_disclosure"} <= keys
    assert "md_tax_credits" not in keys  # not an older-owner article


def test_investment_article_gets_no_live_in_programs():
    brief = {"topic": "Buying your first rental property", "audiences": ["Investors", "Buyers"]}
    keys = {p["key"] for p in bfc.protections_for({"PA"}, brief, investment=True)}
    assert "pa_phfa" not in keys and "pa_disclosure" in keys


def test_missing_protection_is_detected():
    md = [p for p in bfc.PROTECTIONS if p["key"] == "md_first_time_taxes"]
    round4 = ("Buyer and seller usually split those taxes evenly when no exemptions apply. Maryland also has a lower state "
              "transfer tax for first-time Maryland homebuyers, with conditions on who qualifies.")
    assert bfc.missing_protections("Closing costs typically run 2-5% of the price.", md) == md
    # Round 4 named the lower tax but not that the seller pays - still missing.
    assert bfc.missing_protections(round4, md) == md
    assert bfc.missing_protections(md[0]["says"], md) == []
    # Round 7: a law link inside the sentence (dots in the web address) hid it from the check.
    linked = ("Qualifying first-time Maryland homebuyers get [special rules](https://mgaleg.maryland.gov/mgawebsite/Laws/StatuteText?article=gtp&section=13-203) "
              "that lower the state transfer tax and shift which side pays certain closing taxes.")
    assert bfc.missing_protections(linked, md) == []


def test_protections_say_no_numbers():
    import re
    for p in bfc.PROTECTIONS:
        assert not re.search(r"\d", re.sub(r"\b(?:19|20)\d\d\b", "", p["says"])), p["key"]  # a year that scopes the rule ("before 1978") is not fine print
        assert bfc.final_lint(p["says"], {}) == [], p["key"]


# --- pass list (built-in grader) ----------------------------------------------------------------
def test_pass_list_is_fixed_and_cut_items_exist():
    assert bfc.PASS_IDS[:3] == ["facts", "place", "dates"] and len(bfc.PASS_IDS) == 11
    assert bfc.CUT_IF_STILL_FAILING <= set(bfc.PASS_IDS)


def test_code_checks_map_to_pass_items():
    text = ("The district's rate is 18.1421 mills. Half of homes sold above asking. I've seen buyers lose out. "
            "Ask your agent about it. Call Peter today. Price: $___.")
    found = bfc.code_checks(text, {}, [], "Peter Stefanatos")
    assert {"fine_print", "math", "stories", "voice", "complete"} <= set(found)
    assert len(found["voice"]) == 2
    assert bfc.code_checks("Prepared by a real estate agent, the contract has an attorney review period.", {}, [], "Peter Stefanatos") == {}


def test_grader_only_deletes_whole_sentences():
    from app.services.ai import ai_blog_pipeline_service as p
    art = {"title": "T", "intro": ["Homes sell fast. Some buyers shorten inspection timelines in a competitive market, so you may face competition."]}
    assert p._whole_sentences(art, "Homes sell fast.")
    assert p._whole_sentences(art, "Some buyers shorten inspection timelines in a competitive market, so you may face competition.")
    # Round 6 left ". , so you may face competition" - a partial deletion is refused.
    assert not p._whole_sentences(art, "Some buyers shorten inspection timelines in a competitive market")
    assert not p._whole_sentences(art, "inspection timelines in a competitive market, so you may face competition.")
    p._tidy_spaces(art := {"intro": ["$855,000.  To buy here"]})
    assert art["intro"] == ["$855,000. To buy here"]


# --- Round 7 grader: three specific claims -------------------------------------------------------
def test_round7_appraisal_absolute_is_caught_and_the_safe_wording_passes():
    round7 = ("If you're financing, the lender's appraisal (an independent opinion of the home's value) needs to support the price.")
    assert kinds(round7) == ["overstatement"]
    assert "facts" in bfc.code_checks(round7, {}, [], "Peter Stefanatos")
    grader = ("Your lender may require an appraisal to help determine the loan amount. If the appraisal comes in below the purchase price, "
              "you may need to negotiate with the seller, bring additional cash or review your options with your lender.")
    assert kinds(grader) == []


def test_round7_appraisal_fact_is_in_the_evidence():
    facts = [i["claim"] for i in tk.federal_items()]
    hit = [c for c in facts if "appraisal" in c and "below the purchase price" in c]
    assert hit and kinds(hit[0]) == []


def test_round7_md_first_time_wording():
    round7 = ("Qualifying first-time Maryland homebuyers buying a home they'll live in get special rules that lower the state transfer tax "
              "and shift which side pays certain closing taxes - by default toward the seller, unless the contract says otherwise - "
              "so ask the settlement company to calculate your actual share before you make an offer.")
    assert kinds(round7) == ["overstatement"]
    md = [p for p in bfc.PROTECTIONS if p["key"] == "md_first_time_taxes"][0]
    assert "by default" not in md["says"] and "unless the contract" not in md["says"]
    assert kinds(md["says"]) == [] and bfc.missing_protections(md["says"], [md]) == []
    grader = ("Qualifying first-time Maryland homebuyers may receive special state transfer-tax treatment, and the law also affects which "
              "party pays certain closing taxes. Some costs can be negotiated, while others are assigned by law.")
    assert kinds(grader) == [] and bfc.missing_protections(grader, [md]) == []


def test_round7_townhome_maintenance_line():
    from types import SimpleNamespace
    area = SimpleNamespace(name="Annapolis", state="MD", slug="annapolis-md")
    cost = {"as_of": "2026-11-01", "townhome_fees": {"of": 63, "condo_fee": 13, "hoa_only": 38, "no_fee": 12}}
    line = next(i["claim"] for i in tk.cost_items(area, cost) if "townhome" in i["claim"])
    assert "governing documents" in line and "decides what" not in line


# --- Fix A: parts never bigger than their total ------------------------------------------------
def test_parts_fit_rejects_breakdowns_bigger_than_the_total():
    from app.services.areas.market_stats_service import parts_fit
    assert parts_fit(195, [49, 23, 14])
    assert not parts_fit(195, [49, 146, 23])  # the Lancaster round-6 numbers
    assert not parts_fit(None, [1])


def test_article_part_bigger_than_whole_is_flagged():
    assert kinds("Of the homes for sale, 218 of the 195 tax bills were in three townships.") == ["count_mismatch"]
    assert kinds("49 of the 195 listings with a 2026 tax bill are in Manheim Township.") == []
    assert kinds("103 of the 272 sold above asking, 82 at asking and 87 below.") == []
    # Money and percentages aren't counts.
    assert kinds("You'd put $5,000 of the $500 deposit down.") == []
    assert kinds("About 1,200 of 20% of the homes.") == []
    assert "math" in bfc.code_checks("Records show 250 of the 195 homes.", {}, [], "Peter Stefanatos")


# --- Fix B: place by map location, never Bright's town name -------------------------------------
def test_place_comes_from_map_location():
    from app.services.areas.market_stats_service import _place_of
    from app.services.areas import boundary_service as bs
    city = [[[-76.32, 40.02], [-76.28, 40.02], [-76.28, 40.06], [-76.32, 40.06], [-76.32, 40.02]]]
    twp = [[[-76.36, 40.0], [-76.32, 40.0], [-76.32, 40.06], [-76.36, 40.06], [-76.36, 40.0]]]
    munis = [{"name": "Lancaster City", "rings": city, "box": bs.bbox(city)},
             {"name": "Lancaster Township", "rings": twp, "box": bs.bbox(twp)}]
    assert _place_of(40.04, -76.30, munis) == "Lancaster City"
    assert _place_of(40.03, -76.34, munis) == "Lancaster Township"
    # No location, or outside every outline: not guessed - left to the area-wide figure.
    assert _place_of(None, None, munis) is None
    assert _place_of(40.5, -75.0, munis) is None


# --- Round 8: every town measured on the map, never a leftover "unincorporated" share --------------
def _profile(monkeypatch, measured, state="DE", stored=None):
    import asyncio
    from types import SimpleNamespace
    from app.services.areas import town_map_service, market_stats_service, geography

    async def fake_measure(db, area):
        return measured

    async def fake_mix(db, scope):
        return {}
    monkeypatch.setattr(town_map_service, "all_municipalities", fake_measure)
    monkeypatch.setattr(market_stats_service, "area_geography_mix", fake_mix)
    monkeypatch.setattr(geography, "scope_for_market_area", lambda a: {})
    area = SimpleNamespace(name="Wilmington", state=state, slug="wilmington-de", facts={"boundaries": {"municipalities": stored or []}},
                           municipality_type=None, area_type="city", county="New Castle")
    return asyncio.run(tk.geography_profile(None, area))


def _row(name, kind, homes, total=1000, same_state=True, county="New Castle County"):
    return {"name": name, "kind": kind, "homes": homes, "share": round(100 * homes / total, 1), "rings": [[[0, 0], [1, 0], [1, 1], [0, 0]]],
            "same_state": same_state, "county": county}


def test_wilmington_small_towns_are_measured_not_hidden(monkeypatch):
    rows = [_row("Unincorporated New Castle County", "county", 550), _row("Wilmington City", "place", 420),
            _row("Elsmere Town", "place", 20), _row("Bellefonte Town", "place", 6), _row("Newport Town", "place", 3),
            _row("Paradise Township", "township", 1, same_state=False, county="Lancaster County"),
            _row("Unincorporated Montgomery County", "county", 1, county="Montgomery County"),
            _row("Rockville City", "place", 3, county="Montgomery County")]  # Frederick 11/01: stray points
    geo = _profile(monkeypatch, {"homes": 1000, "placed": 1000, "rows": rows})
    line = next(i["claim"] for i in geo["items"] if "mailing address" in i["claim"])
    assert "Unincorporated New Castle County (55%" in line and "Wilmington City (42%" in line
    assert "Bellefonte Town" in line and "Elsmere Town (2%" in line
    assert "Paradise" not in line and "Montgomery" not in line and "Rockville" not in line  # stray map points
    assert "bellefonte town" in {n for n in geo["norm"]["townships"]} or any("bellefonte" in n for n in geo["norm"]["townships"])


def test_same_name_borough_and_township_both_stay(monkeypatch):
    # 11/01 round 11 Doylestown: "Doylestown Township" (34%) and "Doylestown Borough" both normalize to
    # "doylestown" - the borough overwrote the township and the biggest place vanished from the article.
    rows = [_row("Doylestown Township", "township", 340, county="Bucks County"), _row("Buckingham Township", "township", 220, county="Bucks County"),
            _row("Doylestown Borough", "place", 160, county="Bucks County"), _row("New Britain Borough", "place", 65, county="Bucks County"),
            _row("New Britain Township", "township", 9, county="Bucks County")]
    geo = _profile(monkeypatch, {"homes": 1000, "placed": 1000, "rows": rows}, state="PA")
    line = next(i["claim"] for i in geo["items"] if "mailing address" in i["claim"])
    assert "Doylestown Township (34%" in line and "Doylestown Borough (16%" in line
    assert [t["name"] for t in geo["townships"]][:3] == ["Doylestown Township", "Buckingham Township", "Doylestown Borough"]
    assert "doylestown" in geo["norm"]["townships"] and "new britain" in geo["norm"]["townships"]


def test_unmeasured_remainder_never_gets_a_share(monkeypatch):
    stored = [{"name": "Wilmington City", "share": 42, "kind": "place", "rings": []},
              {"name": "Unincorporated New Castle County", "share": 58, "kind": "county", "rings": []}]
    geo = _profile(monkeypatch, None, stored=stored)
    line = next(i["claim"] for i in geo["items"] if "mailing address" in i["claim"])
    assert "58%" not in line and "Wilmington City (42%" in line
    assert "unincorporated areas or other incorporated towns" in line
    assert "Unincorporated New Castle County" not in tk.market_definition(SimpleNamespace_area(), geo)


def SimpleNamespace_area():
    from types import SimpleNamespace
    return SimpleNamespace(name="Wilmington", municipality_type=None, area_type="city")


def test_nj_protection_links_to_the_working_page():
    nj = [p for p in bfc.PROTECTIONS if p["key"] == "nj_rtf"][0]
    assert nj["source_url"] == "https://www.nj.gov/treasury/taxation/realty.shtml"


# --- other-county sources ---------------------------------------------------------------------
def test_other_county_source_dropped_even_when_only_the_address_names_it():
    profiles = [{"town": "Annapolis", "counties": ["Anne Arundel County"]}]
    items = [
        {"name": "Maryland Courts recording fees", "url": "https://www.courts.state.md.us/clerks/frederick/recordingfees"},
        {"name": "Howard County transfer tax", "url": "https://www.howardcountymd.gov/x"},
        {"name": "Anne Arundel County transfer tax", "url": "https://www.aacounty.org/finance"},
        {"name": "Anne Arundel finance office", "url": "https://www.aacounty.org/finance"},
        {"name": "Smart Settlements FAQ", "url": "https://www.smartsettlements.com/x"},
    ]
    kept = [i["name"] for i in tk.drop_other_county_items(items, profiles)]
    assert kept == ["Anne Arundel County transfer tax", "Anne Arundel finance office", "Smart Settlements FAQ"]


# --- 11/01 round-10 grader: source-list housekeeping ------------------------------------------
def test_school_year_dates_stay_whole():
    from app.services.ai import ai_blog_pipeline_service as pipe
    assert pipe._public_date("2025-2026") == "2025-2026"
    assert pipe._public_date("2026-2027 school year (page read 2026-11-01)") == "2026-2027"
    assert pipe._public_date("2025-26") == "2025-26"
    assert pipe._public_date("FY 2026-27") == "FY 2026-27"
    assert pipe._public_date("2026-10 (page read 2026-10-02)") == "2026-10"
    assert pipe._public_date("2026-10-29") == "2026-10-29"
    assert pipe._public_date("2026") == "2026" and pipe._public_date("undated") is None


def test_sources_about_another_township_are_dropped():
    profiles = [{"town": "Lancaster", "counties": ["Lancaster County"], "norm": {"townships": {"lancaster", "manheim", "east hempfield"}}}]
    items = [
        {"name": "Ephrata Township", "url": "https://www.ephratatownship.org/subpage.php?link=tax"},
        {"name": "Tax office", "url": "https://www.ephratatownship.org/tax"},
        {"name": "Manheim Township finance", "url": "https://www.manheimtownship.org/taxes"},
        {"name": "Lancaster County Tax Collection Bureau", "url": "https://www.lctcb.org/?page_id=354"},
        {"name": "PA DCED Homestead Exclusion", "url": "https://dced.pa.gov/x"},
    ]
    kept = [i["name"] for i in tk.drop_other_county_items(items, profiles)]
    assert kept == ["Manheim Township finance", "Lancaster County Tax Collection Bureau", "PA DCED Homestead Exclusion"]


def test_last_years_edition_is_dropped():
    from datetime import date
    nov = date(2026, 11, 1)
    old = {"source_name": "Lancaster County millage", "source_url": "https://co.lancaster.pa.us/DocumentCenter/View/19105/2026-County-Municipal-and-2025-2026-School"}
    new = {"source_name": "Lancaster County millage", "source_url": "https://co.lancaster.pa.us/DocumentCenter/View/2/2026-Municipal-and-2026-2027-School"}
    assert tk.stale_edition(old, nov) and not tk.stale_edition(new, nov)
    assert not tk.stale_edition(old, date(2026, 5, 1))  # still the current school year in May
    assert not tk.stale_edition({"source_name": "Rates 2025-26", "source_url": None}, date(2026, 3, 1))
    assert tk.stale_edition({"source_name": "Rates 2025-26", "source_url": None}, nov)
    assert not tk.stale_edition({"source_name": "Freddie Mac PMMS 2026-10-29", "source_url": "https://www.freddiemac.com/pmms"}, nov)
    mls = {**old, "origin": "mls_live"}
    assert tk.drop_stale_editions([old, new, mls], nov) == [new, mls]


def test_tax_increase_as_a_percent_is_flagged():
    # 11/01 round 11 Doylestown, from a news report.
    assert kinds("The Bucks County Herald reported that it includes a 5.7% school tax increase, the sixth straight yearly increase.") == ["rule_detail"]
    assert kinds("School taxes rose 4.2% this year.") == ["rule_detail"]
    assert kinds("The school board raised taxes for 2026-27, the sixth straight yearly increase.") == []
    assert kinds("Prices rose 5% from a year earlier.") == []


def test_another_citys_source_is_dropped_but_federal_and_state_sites_stay():
    # 11/01 round 11 Ashburn (Loudoun County): a City of Alexandria recordation-tax page in its sources.
    profiles = [{"town": "Ashburn", "counties": ["Loudoun County"], "norm": {"townships": {"loudoun"}}}]
    items = [
        {"name": "City of Alexandria – Recordation Tax", "url": "https://www.alexandriava.gov/real-estate/recordation-tax"},
        {"name": "Recordation tax", "url": "https://www.alexandriava.gov/x"},
        {"name": "Loudoun County Commissioner of the Revenue", "url": "https://www.loudoun.gov/revenue"},
        {"name": "CFPB closing costs", "url": "https://www.consumerfinance.gov/owning-a-home/"},
        {"name": "FHFA conforming loan limits", "url": "https://www.fhfa.gov/news"},
        {"name": "Code of Virginia 58.1-802", "url": "https://law.lis.virginia.gov/vacode/title58.1/chapter8/section58.1-802/"},
        {"name": "Montgomery County transfer tax", "url": "https://www.montgomerycountymd.gov/x"},
    ]
    kept = [i["name"] for i in tk.drop_other_county_items(items, profiles)]
    assert kept == ["Loudoun County Commissioner of the Revenue", "CFPB closing costs", "FHFA conforming loan limits", "Code of Virginia 58.1-802"]
    # The town's own city site stays in its own article.
    alex = [{"town": "Alexandria", "counties": [], "norm": {"townships": {"alexandria"}}}]
    assert len(tk.drop_other_county_items(items[:2], alex)) == 2


# --- 11/01 round-11 grader: jumbo status by the county's own limit -----------------------------
def test_flat_jumbo_claims_are_flagged_but_hedges_and_definitions_pass():
    assert kinds("Even at 20% down, the loan is still a jumbo loan.") == ["overstatement"]
    assert kinds("A loan on a house near Ashburn's $1.34 million median list price would likely be a jumbo loan (one above that limit) with its own rules and rates, so ask a lender for a jumbo quote.") == ["overstatement"]
    assert kinds("Any loan above a set size counts as a jumbo loan, which has its own rules and rates.") == []
    assert kinds("Depending on how much you borrow, a $1.34 million house may need a jumbo loan.") == []
    assert kinds("Get a full pre-approval, including a jumbo quote if you're shopping near the median house price.") == []


def test_county_limit_decides_jumbo_on_the_payment_card():
    from datetime import date
    from app.services.ai import ai_blog_pipeline_service as pipe
    nov = date(2026, 11, 1)
    loudoun = tk.county_conforming_limit("VA", ["Loudoun County"], nov)
    assert loudoun["limit"] == 1_249_125 and "fhfa.gov" in loudoun["source_url"]
    assert tk.county_conforming_limit("PA", ["Bucks County"], nov)["limit"] == 832_750
    assert tk.county_conforming_limit("VA", ["Alexandria County"], nov)["limit"] == 1_249_125  # Alexandria city, as Bright labels it
    assert tk.county_conforming_limit("MD", ["Frederick County", "Carroll County"], nov) is None  # limits differ -> it depends
    assert tk.county_conforming_limit("VA", ["Nowhere County"], nov) is None
    assert tk.county_conforming_limit("VA", ["Loudoun County"], date(2031, 1, 5)) is None  # no list on file for that year

    entry = {"homes": 261, "p25": 600_000, "median": 720_000, "houses": 52, "house_median": 1_340_000, "tax_pct_near_house": 0.9}
    rate = {"rate_30": 7.24, "as_of": "2026-10-29"}
    ex = pipe.payment_example(entry, rate, ["Ashburn"], conforming_limit=loudoun["limit"], limit_area="Loudoun County", basis="house")
    assert [s["jumbo"] for s in ex["scenarios"]] == [True, False, False]  # $1,273,000 / $1,206,000 / $1,072,000
    claims = [i["claim"] for i in pipe.payment_example_items(ex)]
    assert "above Loudoun County's 2026 conforming loan limit" in claims[1] and "within Loudoun County's 2026 conforming loan limit" in claims[3]
    unknown = pipe.payment_example(entry, rate, ["Ashburn"], basis="house")
    assert not any(s["jumbo"] for s in unknown["scenarios"]) and unknown["limit_known"] is False
    assert any("depends on the amount borrowed and the conforming loan limit" in i["claim"] for i in pipe.payment_example_items(unknown))


def test_bare_ask_lines_take_their_sections_topic():
    # 11/01 round-11 grader nice-to-have: a bare "Ask me" in Doylestown's financing section.
    h = lambda t, x: {"type": t, "data": {"html": x}}
    blocks = [h("heading", "Closing costs, first-time buyer help and getting pre-approved"),
              h("subheading", "Loan options and first-time buyer help"), h("paragraph", "Ask me and a lender what would fit your loan."),
              {"type": "ask_agent", "data": {}},
              h("heading", "Pennsylvania transfer tax"), h("paragraph", "Ask me about it."), {"type": "ask_agent", "data": {}},
              h("heading", "The bottom line"), {"type": "ask_agent", "data": {}},
              h("heading", "HOA fees"), {"type": "ask_agent", "data": {"topic": "the resale packet"}}]
    out = writer._name_ask_topics(blocks)
    topics = [b["data"].get("topic") for b in out if b["type"] == "ask_agent"]
    assert topics == ["loan options and first-time buyer help", "Pennsylvania transfer tax", None, "the resale packet"]


def test_spotlight_district_suffix_shown_once():
    import app.api  # noqa: F401 - load the app first (content_adapters has a circular import on its own)
    from app.services.website.content_adapters import _public_school_districts
    facts = {"public_schools": {"districts": [{"name": "Cherry Hill Township Public Schools School District"}, {"name": "Hempfield School District"},
                                              {"name": "Loudoun County Public Schools"}]}}
    assert _public_school_districts(facts, []) == ["Cherry Hill Township Public Schools", "Hempfield School District", "Loudoun County Public Schools"]


# --- 11/01 round-12 grader: program eligibility is never paraphrased ---------------------------
def test_program_eligibility_paraphrase_is_flagged():
    hits = bfc.final_lint("Its programs include a first-time homebuyer mortgage, down payment help for first-time buyers and extra help for "
                          "buyers who are also the first in their family to buy a home.", {})
    assert [h["kind"] for h in hits] == ["rule_detail"] and "participating lender" in hits[0]["fix"]
    assert kinds("The First Generation program is for buyers whose parents never owned a home.") == ["rule_detail"]
    assert kinds("You count as a first-time buyer if you haven't owned a home in three years.") == ["rule_detail"]


def test_plain_program_categories_pass():
    assert kinds("NJHMFA also offers additional assistance through its First Generation Down Payment Assistance Program for qualified buyers. "
                 "Ask a participating lender whether you meet the program's requirements.") == []
    assert kinds("The Pennsylvania Housing Finance Agency (PHFA) offers home loans and help with down payment and closing costs for buyers who qualify.") == []
    assert kinds("It's a conventional loan with a low down payment for buyers within its income limits, and you don't have to be a first-time buyer to use it.") == []
    assert kinds("VA loans for eligible service members and veterans have no mortgage insurance, though a one-time funding fee may apply.") == []


def test_short_district_names_in_a_school_sentence_are_districts():
    # 11/01 round 13 Wilmington: "Brandywine (33%)" in a districts sentence was read as a place and the true sentence was cut.
    prof = {"town": "Wilmington", "kind": "city", "townships": [{"name": "Wilmington City"}], "school_districts": [{"name": "Brandywine School District"}],
            "norm": {"townships": {"wilmington city"}, "counties": {"new castle"}, "districts": {"red clay consolidated", "brandywine", "christina"}}}
    known = {"townships": {"brandywine"}, "counties": {"new castle"}}
    ok = ("Public school districts serving Wilmington-address homes include Red Clay Consolidated (53% of them), Brandywine (33%) and "
          "Christina (13%), depending on the exact address.")
    assert tk.geography_audit(ok, [prof], known) == []
    assert tk.geography_audit("Most Wilmington homes sit in Brandywine Township.", [prof], known)


def test_one_home_town_in_another_county_is_a_stray_point(monkeypatch):
    # 11/01 round 13: one Baltimore-addressed home "in Annapolis City" (Anne Arundel County).
    rows = [_row("Baltimore City", "place", 816, county="Baltimore city"), _row("Unincorporated Baltimore County", "county", 172, county="Baltimore County"),
            _row("Unincorporated Anne Arundel County", "county", 11, county="Anne Arundel County"), _row("Annapolis City", "place", 1, county="Anne Arundel County"),
            _row("Brooklyn Park", "place", 1, county="Anne Arundel County")]
    for r in rows:
        r["isolated"] = r["name"] == "Annapolis City"  # 25 km from every other Baltimore address; Brooklyn Park sits right next to them
    geo = _profile(monkeypatch, {"homes": 1000, "placed": 1000, "rows": rows}, state="MD")
    line = next(i["claim"] for i in geo["items"] if "mailing address" in i["claim"])
    assert "Annapolis" not in line and "Unincorporated Anne Arundel County" in line
    assert "Brooklyn Park" in line  # round-13 grader: a rare but real outlier is never dropped for being rare


def test_isolation_is_by_distance_not_rarity():
    from app.services.areas import town_map_service as tms
    near = [(-76.61 + i * 0.002, 39.29) for i in range(30)]       # a cluster of homes
    pts = near + [(-76.61, 39.291), (-76.49, 38.98)]                # one next to them, one ~35 km away
    where = [0] * 30 + [1, 2]
    assert tms._isolated_rows(pts, where, [30, 1, 1]) == {2}


def test_every_one_percent_place_must_be_named():
    prof = {"town": "Baltimore", "townships": [{"name": "Baltimore City", "share": 82, "sort": 81.6, "kind": "place"},
                                               {"name": "Unincorporated Baltimore County", "share": 17, "sort": 17.2, "kind": "county"},
                                               {"name": "Unincorporated Anne Arundel County", "share": 1, "sort": 1.1, "kind": "county"},
                                               {"name": "other incorporated towns (X)", "share": "under 1", "sort": 0.3, "kind": "small"}]}
    ps = tk.place_protections([prof])
    assert [p["name"] for p in ps] == ["Baltimore City", "unincorporated Baltimore County", "unincorporated Anne Arundel County"]
    text = "A Baltimore address can be in Baltimore City or Baltimore County, so check each home."
    assert [p["name"] for p in bfc.missing_protections(text, ps)] == ["unincorporated Anne Arundel County"]
    assert bfc.missing_protections(text + " Some are in Anne Arundel County.", ps) == []
    assert bfc.missing_protections("Homes with a Baltimore address are popular.", ps[:1])  # the town's own name isn't "Baltimore City"
    assert tk.place_protections([{"town": "Cherry Hill", "townships": [{"name": "Cherry Hill Township", "share": 100, "sort": 99.7, "kind": "township"}]}]) == []


def test_ground_rent_and_lead_paint_and_md_disclaimer():
    brief = {"topic": "Buying a rowhome in Baltimore, MD", "audiences": ["Buyers"]}
    keys = {p["key"] for p in bfc.protections_for({"MD"}, brief, places={"baltimore city", "baltimore"})}
    assert {"md_ground_rent", "us_lead_paint", "md_disclosure"} <= keys
    plain = {"topic": "Buying a home in Annapolis", "audiences": ["Buyers"]}
    keys2 = {p["key"] for p in bfc.protections_for({"MD"}, plain, places={"annapolis", "anne arundel"}, older_share=0.2)}
    assert "md_ground_rent" not in keys2 and "us_lead_paint" not in keys2
    assert "us_lead_paint" in {p["key"] for p in bfc.protections_for({"PA"}, plain, older_share=0.55)}  # most listings pre-1978
    md = next(p for p in bfc.PROTECTIONS if p["key"] == "md_disclosure")
    # Round 13: "as-is" alone is missing the hidden-defect duty.
    assert bfc.missing_protections("Sellers give either a Disclosure Statement or a Disclaimer Statement, which means the home is sold as-is.", [md]) == [md]
    assert bfc.missing_protections("A seller who uses the Disclaimer Statement must still disclose known hidden (latent) defects that threaten health or safety.", [md]) == []


def test_payment_card_notes_carry_no_rates():
    # 11/02: the PA card note said "1% state plus at least 1% local" - a tax rate on every PA article.
    from app.services.ai import ai_blog_pipeline_service as pipe
    for st, note in pipe._CLOSING_NOTES.items():
        assert bfc.final_lint(note, {}) == [] and "%" not in note, st
