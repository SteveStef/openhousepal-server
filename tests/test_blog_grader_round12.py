"""Grader round 12 (11/16): the outside audit of the three out-of-state articles written on live (Bethesda MD,
Haddonfield NJ, Wilmington DE). Each test is one finding traced to its cause in the engine."""


def test_borough_of_haddonfield_is_haddonfield_borough():
    """A2: the borough's page was read ONCE and "at least 10 days before settlement" was printed. The fact named the
    "Borough of Haddonfield"; we knew the place as "Haddonfield Borough", so it was nobody's municipal fact and house
    rule 1 (a number only after two separate readings) never looked at it."""
    from app.services.areas import town_knowledge_service as tk
    munis = [{"name": "Haddonfield Borough", "county": "Camden"}, {"name": "Haddon Township", "county": "Camden"}, {"name": "Barrington Borough", "county": "Camden"}]
    fact = {"town": "Haddonfield, NJ", "claim": "The Borough of Haddonfield requires a smoke detector inspection before settlement; the fee is $56 if the "
            "application is submitted and paid 10 days prior to settlement.", "source_name": "Borough of Haddonfield - Smoke Detector & Sidewalk Inspections",
            "source_url": "https://www.haddonfieldnj.org/departments/borough_clerk/smoke_detector_and_sidewalk_inspection_requests.php"}
    assert tk.muni_of(fact, munis)["name"] == "Haddonfield Borough"
    assert tk.muni_of({**fact, "town": "Borough of Haddonfield"}, munis)["name"] == "Haddonfield Borough"
    # the neighbour is not swallowed: "Township of Haddon" is not inside "Borough of Haddonfield"
    assert tk.muni_of({"town": "", "claim": "The Township of Haddon requires a certificate of compliance.", "source_name": ""}, munis)["name"] == "Haddon Township"
    assert tk.muni_forms("Wilmington City") == ["Wilmington City", "City of Wilmington"]
    assert tk.muni_forms("Unincorporated New Castle County") == ["Unincorporated New Castle County"]
    # read once -> not confirmed -> the writer's copy carries no number
    mine = [{**fact, "municipality": "Haddonfield Borough"}]
    masked, held = tk.mask_unconfirmed(fact["claim"], tk.confirmed_specifics(mine), tk.disputed_specifics(mine))
    assert held == 2 and "10 days" not in masked and "$56" not in masked and "do NOT state a number" in masked


def test_the_page_and_the_application_form_disagree_so_no_number_is_ever_confirmed():
    """A2, the grader's point: the borough's web page ($56, "10 days prior") and its own application form ($45, "more
    than 10 business days") differ. Once both are held, neither number is confirmed however often either is read."""
    from app.services.areas import town_knowledge_service as tk
    page = {"municipality": "Haddonfield Borough", "saved_at": "2026-11-10T10:00:00+00:00", "reads": ["2026-11-16T22:50:00+00:00"],
            "claim": "Resale smoke detector certificate: the fee is $56 if the application is submitted and paid 10 days prior to settlement.",
            "source_url": "https://www.haddonfieldnj.org/departments/borough_clerk/smoke_detector_and_sidewalk_inspection_requests.php"}
    form = {"municipality": "Haddonfield Borough", "saved_at": "2026-11-16T22:50:00+00:00",
            "claim": "Resale smoke detector certificate application: the fee is $45 if the application is submitted more than 10 business days prior to settlement.",
            "source_url": "https://cms5.revize.com/revize/boroughofhaddonfield/Smoke%20Detector%20Application%20Form.pdf"}
    assert "10 day" in tk.confirmed_specifics([page])                     # the page alone, read twice: confirmed
    both = [page, form]
    assert {"$56", "$45"} <= tk.disputed_specifics(both)
    assert not ({"$56", "$45"} & tk.confirmed_specifics(both))


def test_a_local_requirement_nobody_could_place_gives_no_number():
    """Rule 1's backstop: a web-read sale requirement that is tied to none of the article's municipalities cannot be
    confirmed by a second reading, so its day counts and fees are held back."""
    from app.services.ai import ai_blog_pipeline_service as pipe
    stray = {"town": "Haddonfield, NJ", "origin": "web_research", "source_url": "https://example.gov/resale",
             "claim": "A resale certificate is required; apply at least 15 business days before settlement. The fee is $120."}
    placed = {**stray, "municipality": "Haddonfield Borough"}                 # the per-municipality rule handles this one
    state = {**stray, "town": "general"}                                      # a state-wide rule is not a municipal number
    train = {"town": "Haddonfield, NJ", "origin": "web_research", "source_url": "https://www.ridepatco.org/", "claim": "Trains take about 20 minutes and the fare is $3."}
    mls = {"town": "Haddonfield, NJ", "origin": "mls_live", "source_url": None, "claim": "Condos that list a resale certificate fee show a median of $250."}
    items = [stray, placed, state, train, mls]
    assert pipe.hold_back_unplaced(items) == 2
    assert "15 business days" not in stray["claim"] and "$120" not in stray["claim"] and "do NOT state a number" in stray["claim"]
    assert "15 business days" in placed["claim"] and "15 business days" in state["claim"] and "$3" in train["claim"] and "$250" in mls["claim"]
    assert pipe.hold_back_unplaced(items) == 0                                 # already held back: nothing more to do
    # and the final check then refuses a sentence that prints the number anyway
    bad = "The Borough asks you to apply for the resale certificate at least 15 business days before settlement."
    assert pipe.unconfirmed_number_claims(bad, [stray], {})


def test_one_home_with_no_list_price_no_longer_splits_the_counts():
    """A3: Wilmington had 483 homes for sale and one of its 240 townhomes had no list price. The article said 292 condos
    or townhomes, 240 townhomes and 53 condos with a fee (240 + 53 = 293), and the payment card said 482 homes."""
    from app.services.areas import market_stats_service as ms

    def home(price, attached=False, house=False):
        return {"price": price, "attached": attached, "house": house, "one_level": False, "manufactured": False, "senior": None}
    homes = ([home(250_000 + n, attached=True) for n in range(239)] + [home(None, attached=True)]                 # the unpriced townhome
             + [home(200_000 + n, attached=True) for n in range(53)] + [home(415_000 + n, house=True) for n in range(159)]
             + [home(300_000 + n) for n in range(31)])
    mix = ms._home_mix(homes)
    assert mix["homes"] == 483 and mix["attached"]["count"] == 293 == 240 + 53 and mix["house"]["count"] == 159
    assert mix["attached"]["median"] and mix["low_quarter"]["count"] <= 482
    # the payment card counts the same homes: 483, with the unpriced one in no price
    rows = ([(250_000 + n, 2_000, "TOWNHOUSE", None, 2025) for n in range(239)] + [(None, None, "TOWNHOUSE", None, None)]
            + [(200_000 + n, 1_800, "CONDO", None, 2025) for n in range(53)] + [(415_000 + n, 3_500, "SINGLE_FAMILY", None, 2025) for n in range(159)]
            + [(300_000 + n, 2_500, "MULTI_FAMILY", None, 2025) for n in range(31)])
    entry = ms.entry_profile(rows)
    without = ms.entry_profile(rows[:239] + rows[240:])
    assert entry["homes"] == 483 and entry["attached"] == 293 and entry["houses"] == 159
    assert without["homes"] == 482 and without["attached"] == 292          # the counts moved by exactly that one home
    assert entry["p25"] == without["p25"] and entry["median"] == without["median"]      # and no price did


def test_a_relocation_article_is_told_what_a_mover_needs():
    """B1-B3: the Wilmington article named no neighborhood, no sales-tax difference and no city wage tax. Research ran
    out of look-ups before the neighborhoods and never asked about the other two."""
    from app.services.ai import ai_blog_pipeline_service as pipe
    brief = {"topic": "Moving to Wilmington, Delaware: Neighborhoods, Taxes and Cost of Living", "audiences": ["Relocating"],
             "goal": "Know how Delaware taxes and everyday costs compare to Pennsylvania",
             "key_points": "- Which Wilmington neighborhoods are popular with newcomers and why\n- How Delaware income and property taxes work for movers"}
    research, write = pipe._coverage(brief, 1), pipe._coverage(brief, 2)
    assert "general sales tax" in research and "wage" in research and "neighborhoods" in research and "COUNTY" in research
    assert "sales tax" in write and "wage" in write and "never who lives there" in write and "not licensed" in write
    # an ordinary buyer article is not sent on these look-ups
    plain = {"topic": "Buying a Home in Wayne, PA: What It Costs and How to Get Ready", "audiences": ["Buyers"], "key_points": "- Closing costs"}
    assert "RELOCATION" not in pipe._coverage(plain, 1)
