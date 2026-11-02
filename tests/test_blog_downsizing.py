"""11/01 downsizing article grade: what kinds of homes are for sale, one tax year per town, a video
that says what it is, and what a downsizing reader expects."""
from app.services.ai import ai_blog_pipeline_service as pipe
from app.services.ai import ai_blog_writer_service as w
from app.services.areas import market_stats_service as ms


def _home(price, kind="house", levels=("2",), senior="false"):
    return {"price": price, "attached": kind == "condo", "house": kind == "house", "manufactured": kind == "manufactured",
            "one_level": ms._one_level(list(levels)) and kind != "manufactured",
            "senior": None if senior is None else senior == "true"}


def test_home_mix_explains_a_low_quarter_price():
    homes = [_home(250_000 + i * 10_000, "condo", ("1",)) for i in range(4)] + [_home(900_000 + i * 50_000) for i in range(8)]
    mix = ms._home_mix(homes)
    assert mix["attached"] == {"count": 4, "median": None}
    assert mix["house"]["count"] == 8 and mix["house"]["median"] == 1_075_000
    assert mix["low_quarter"] == {"count": 3, "attached": 3}
    assert mix["one_level"]["count"] == 4
    assert mix["senior"] == {"count": 0, "median": None}


def test_home_mix_skips_55_plus_when_listings_leave_it_blank():
    homes = [_home(500_000 + i, senior=None) for i in range(10)]
    assert "senior" not in ms._home_mix(homes)
    assert ms._home_mix(homes[:5]) == {}


def test_one_level_reads_brights_levels_field():
    assert ms._one_level(["1"]) and ms._one_level("1.0")
    assert not ms._one_level(["1.5"]) and not ms._one_level(None)


def _blocks(*sections):
    out = [{"id": "i", "type": "paragraph", "data": {"html": "Intro."}}]
    for head, body in sections:
        out += [{"id": head, "type": "heading", "data": {"html": head}}, {"id": head + "p", "type": "paragraph", "data": {"html": body}}]
    return out


def test_video_gets_a_lead_in_when_its_section_never_mentions_it():
    brief = {"video": {"url": "https://youtu.be/x", "title": "Should You Sell Your House Now, Or Wait | Philly Suburbs"}}
    out = w.place_video(_blocks(("What your home could sell for now", "Prices held."), ("Costs", "Transfer tax."), ("Talk to me", "Call.")), brief)
    i = next(n for n, b in enumerate(out) if b["type"] == "youtube")
    assert "watch <em>Should You Sell Your House Now, Or Wait</em> just below" in out[i - 1]["data"]["html"]


def test_video_has_no_extra_line_when_the_writer_introduced_it():
    brief = {"video": {"url": "https://youtu.be/x", "title": "Selling in Wayne"}}
    out = w.place_video(_blocks(("Selling in Wayne", "Watch my video below."), ("Talk to me", "Call.")), brief)
    i = next(n for n, b in enumerate(out) if b["type"] == "youtube")
    assert out[i - 1]["data"]["html"] == "Watch my video below."


def test_downsizing_brief_gets_its_checklist_and_others_do_not():
    brief = {"topic": "Downsizing on the Main Line: what to know before you sell and buy smaller", "audiences": ["Sellers"]}
    assert "Property Tax/Rent" in pipe._coverage(brief, 1) and "ARTICLE'S state" in pipe._coverage(brief, 1)
    assert "rent-back" in pipe._coverage(brief, 2) and "55+" in pipe._coverage(brief, 2)
    assert pipe._coverage({"topic": "Main Line home prices for buyers", "audiences": ["Buyers"]}, 2) == ""


# --- 11/01 out-of-state test articles: geography for towns outside PA ---------------------------
import asyncio
from types import SimpleNamespace
from app.services.areas import town_knowledge_service as tk


def _profile(monkeypatch, state, census_munis, census_districts, mls_towns, mls_districts):
    async def fake_mix(db, scope):
        return {"townships": [{"name": n, "share": s, "homes": 50, "county": None} for n, s in mls_towns],
                "school_districts": [{"name": n, "share": s, "homes": 50} for n, s in mls_districts], "counties": [], "mailing_towns": []}
    monkeypatch.setattr(ms, "area_geography_mix", fake_mix)
    from app.services.areas import geography
    monkeypatch.setattr(geography, "scope_for_market_area", lambda a: {})
    area = SimpleNamespace(name="Town", slug="town", state=state, municipality_type=None, area_type="town",
                           facts={"boundaries": {"municipalities": [{"name": n, "share": s} for n, s in census_munis],
                                                 "school_districts": [{"name": n, "share": s} for n, s in census_districts]}})
    return asyncio.run(tk.geography_profile(None, area))


def test_maryland_election_district_is_not_a_township(monkeypatch):
    g = _profile(monkeypatch, "MD", [("Frederick City", 59), ("Unincorporated Frederick County", 41)], [], [("FREDERICK", 46)], [])
    assert [t["name"] for t in g["townships"]] == ["Frederick City", "Unincorporated Frederick County"]


def test_delaware_hundred_is_not_a_township(monkeypatch):
    g = _profile(monkeypatch, "DE", [("Rehoboth Beach City", 14)], [], [("LEWES REHOBOTH HUNDRED", 99)], [])
    assert "Hundred" not in g["items"][0]["claim"]


def test_pa_township_from_the_mls_still_joins(monkeypatch):
    g = _profile(monkeypatch, "PA", [("Radnor Township", 46)], [], [("TREDYFFRIN", 30)], [])
    assert "Tredyffrin Township" in [t["name"] for t in g["townships"]]


def test_one_district_under_two_names_is_listed_once(monkeypatch):
    g = _profile(monkeypatch, "NJ", [], [("Princeton Public Schools", 48), ("West Windsor-Plainsboro Regional School District", 26)],
                 [], [("PRINCETON REGIONAL SCHOOLS", 50), ("WEST WINDSOR-PLAINSBORO REGIONAL", 28), ("UPPER DARBY", 5)])
    names = [d["name"] for d in g["school_districts"]]
    assert names.count("Princeton Public Schools") == 1 and not any("Princeton Regional" in n for n in names)
    assert any("Upper Darby" in n for n in names)


def test_unincorporated_county_is_not_a_wrong_township():
    prof = {"town": "Frederick", "kind": "postal town", "townships": [{"name": "Frederick City"}, {"name": "Unincorporated Frederick County"}],
            "school_districts": [], "norm": {"townships": {"frederick city", "unincorporated frederick"}, "counties": {"frederick"},
                                              "districts": set(), "mailing": set()}}
    text = "Those homes sit in Frederick City (about 59% of them) and Unincorporated Frederick County (the other 41%)."
    assert tk.geography_audit("Frederick homes sit in Frederick City and Unincorporated Frederick County.", [prof], {"townships": {"unincorporated"}}) == []
    assert tk.geography_audit("Frederick homes sit in Ballenger Township.", [prof], {"townships": {"ballenger"}})


def test_payment_card_follows_the_section_that_points_to_it():
    blocks = _blocks(("What starter homes cost right now", "Prices."), ("Down payment and help", "The payment example below shows a $390,000 home."),
                     ("How fast homes sell", "Fast."), ("Talk to me", "Call."))
    out = pipe.place_payment_example(blocks, {"price": 390000})
    i = next(n for n, b in enumerate(out) if b["type"] == "payment_example")
    assert out[i - 1]["data"]["html"].startswith("The payment example below") and out[i + 1]["data"]["html"] == "How fast homes sell"


def test_a_county_in_a_list_ending_in_counties_is_a_county():
    prof = {"town": "Princeton", "kind": "postal town", "townships": [{"name": "Princeton"}, {"name": "Plainsboro Township"}], "school_districts": [],
            "norm": {"townships": {"princeton", "plainsboro"}, "counties": {"mercer", "middlesex", "somerset"}, "districts": set(), "mailing": set()}}
    known = {"townships": {"middlesex"}, "counties": {"mercer", "middlesex", "somerset"}}
    ok = "A Princeton mailing address covers homes in eight municipalities across Mercer, Middlesex and Somerset counties."
    assert tk.geography_audit(ok, [prof], known) == []
    assert tk.geography_audit("Princeton homes sit in Middlesex Borough.", [prof], known)


# --- 11/01 fact policy: rules named not spelled out; "Ask [agent]" lines; data safeguards -------------------
def test_manufactured_homes_are_counted_apart_from_one_level_homes():
    homes = [_home(150_000 + i, "manufactured", ("1",)) for i in range(5)] + [_home(400_000 + i, "house", ("1",)) for i in range(5)]
    mix = ms._home_mix(homes)
    assert mix["manufactured"]["count"] == 5 and mix["one_level"]["count"] == 5 and mix["one_level"]["median"] >= 400_000


def _p(text):
    return {"id": text[:5], "type": "paragraph", "data": {"html": text}}


def test_ask_agent_line_follows_a_sentence_that_sends_the_reader_to_the_agent():
    blocks = [{"type": "heading", "data": {"html": "Help for buyers"}},
              _p("Frederick County has down payment help for first-time buyers; conditions apply. Ask me whether you'd qualify."),
              _p("Rates moved."), {"type": "heading", "data": {"html": "Taxes"}}, _p("I can walk you through the credit.")]
    out = w.ask_agent_rows(blocks)
    assert [b["type"] for b in out] == ["heading", "paragraph", "ask_agent", "paragraph", "heading", "paragraph", "ask_agent"]


def test_ask_agent_lines_are_capped_and_never_doubled():
    ask = {"type": "ask_agent", "data": {"topic": "x"}}
    blocks = []
    for k in range(5):
        blocks += [{"type": "heading", "data": {"html": f"S{k}"}}, _p("Ask me about it."), dict(ask), dict(ask)]
    out = w.ask_agent_rows(blocks)
    assert sum(b["type"] == "ask_agent" for b in out) == 3
    assert all(not (a["type"] == b["type"] == "ask_agent") for a, b in zip(out, out[1:]))


def test_writer_ask_agent_block_becomes_an_ask_agent_row_with_its_topic():
    a = w._normalize_article({"title": "T", "intro": ["Intro."], "at_a_glance": [], "closing": {"heading": "Talk", "paragraphs": ["Call."], "button_label": "Go"}, "sections": [{"heading": "Help", "blocks": [{"type": "paragraph", "text": "There is down payment help. Conditions apply."},
                                                 {"type": "ask_agent", "text": "down payment help."}]}]})
    blocks = w.article_to_blocks(a)
    row = next(b for b in blocks if b["type"] == "ask_agent")
    assert row["data"] == {"topic": "down payment help"}
    assert "down payment help" not in w.blocks_to_text([row])


def test_rules_are_named_never_spelled_out_in_both_writers():
    assert "NAME THEM, NEVER SPELL THEM OUT" in pipe._ENGINE_RULES and "ask_agent" in pipe._ENGINE_RULES
    assert "never their amounts" in w._SYSTEM
    assert "rule_detail" in str(pipe._REVIEW_TOOL) and "rule_detail (" in pipe._REVIEW_SYSTEM


# --- 11/01 grader round 3: applicability, first person, situation exceptions, data coverage ----------------
def test_engine_never_decides_whether_a_rule_applies_and_speaks_in_first_person():
    assert "WHETHER A RULE APPLIES TO THE READER" in pipe._ENGINE_RULES and "Never refer to yourself in the third person" in pipe._ENGINE_RULES
    assert "applicability" in str(pipe._REVIEW_TOOL) and "applicability (" in pipe._REVIEW_SYSTEM and '"ask Peter"' in pipe._REVIEW_SYSTEM
    assert "exceptions that fit the article's situation" in pipe._RESEARCH_SYSTEM and "last two years" in pipe._RESEARCH_SYSTEM


def test_inherited_home_brief_gets_its_checklist():
    brief = {"topic": "Selling an inherited home in Baltimore: what heirs should know", "audiences": ["Sellers"]}
    assert "personal representative" in pipe._coverage(brief, 1) and "tax basis" in pipe._coverage(brief, 2)
    assert "INHERITED" not in pipe._coverage({"topic": "Condos in Arlington"}, 1)


def test_sources_about_another_county_are_dropped():
    prof = [{"town": "Baltimore", "counties": ["Baltimore City"]}]
    items = [{"claim": "Recording fees in Frederick County are ...", "source_name": "Maryland Courts - Frederick County Clerk"},
             {"claim": "Baltimore City's property tax rate is ...", "source_name": "Baltimore City"},
             {"claim": "Maryland's transfer tax is ...", "source_name": "Maryland Comptroller"}]
    kept = tk.drop_other_county_items(items, prof)
    assert [i["source_name"] for i in kept] == ["Baltimore City", "Maryland Comptroller"]


def test_one_town_geography_is_a_statement_about_our_records(monkeypatch):
    g = _profile(monkeypatch, "NJ", [("Cherry Hill Township", 100)], [], [], [])
    claim = g["items"][0]["claim"]
    assert "in our Bright MLS records" in claim and "confirm both for a specific home" in claim and "No Cherry Hill" not in claim


def test_financing_mix_always_says_how_many_sales_recorded_it():
    area = SimpleNamespace(name="Cherry Hill", slug="cherry-hill-nj", state="NJ")
    cost = {"as_of": "2026-11-01", "financing": {"closed_sales": 140, "recorded": 124, "since": "2025-11-01",
                                                 "mix": [{"type": "Conventional", "pct": 66}, {"type": "Cash", "pct": 23}]}}
    claim = next(i["claim"] for i in tk.cost_items(area, cost) if "how buyers paid" in i["claim"])
    assert "124 of 140 closed sales" in claim


def test_move_up_articles_price_the_example_at_a_typical_house():
    entry = {"homes": 169, "p25": 350_000, "median": 420_000, "tax_pct_near_p25": 2.0, "houses": 90, "house_median": 552_000, "tax_pct_near_house": 2.2}
    rate = {"rate_30": 7.24, "as_of": "2026-10-29"}
    brief = {"topic": "Moving up in Cherry Hill: sell your starter home and buy a bigger one"}
    assert pipe.payment_basis(brief) == "house" and pipe.payment_basis({"topic": "First-time buyers in Frederick"}) == "entry"
    ex = pipe.payment_example(entry, rate, ["Cherry Hill"], basis="house")
    assert ex["price"] >= 540_000 and "single-family houses" in ex["price_basis"]
    assert pipe.payment_example(entry, rate, ["Cherry Hill"])["price"] == 350_000


# Round 9 grader (Frederick single-family article, $390,000 example from mostly condos/townhomes).
def test_payment_example_matches_the_articles_home_type():
    entry = {"homes": 400, "p25": 390_000, "median": 480_000, "tax_pct_near_p25": 1.1,
             "houses": 210, "house_median": 600_000, "tax_pct_near_house": 1.2,
             "attached": 150, "attached_median": 345_000, "tax_pct_near_attached": 1.0}
    rate = {"rate_30": 7.24, "as_of": "2026-10-29"}
    sf = {"topic": "Buying a single-family home in Frederick: city vs. county"}
    assert pipe.payment_basis(sf) == "house"
    ex = pipe.payment_example(entry, rate, ["Frederick"], basis=pipe.payment_basis(sf))
    assert ex["price"] == 600_000 and "single-family" in ex["tax_basis"] and "%" not in ex["tax_basis"]
    th = {"topic": "Is a townhome right for you in Frederick?"}
    assert pipe.payment_basis(th) == "attached"
    assert pipe.payment_example(entry, rate, ["Frederick"], basis="attached")["price"] == 345_000
    assert pipe.payment_basis({"topic": "House hunting tips for first-time buyers"}) == "entry"
    assert pipe.payment_basis({"topic": "Townhouse vs. condo"}) == "attached"


def test_flagged_bullets_are_cut_and_an_emptied_list_goes():
    # 11/01 Lancaster: seven "Lancaster City: 41.3186 mills" bullets were flagged but never cut.
    art = {"title": "T", "intro": [], "at_a_glance": [], "sections": [{"heading": "Taxes", "blocks": [
        {"type": "paragraph", "text": "Each layer sets its own rate."},
        {"type": "bulleted_list", "items": ["Lancaster City: 41.3186 mills", "Lancaster Township: 30.5886 mills"]}]}]}
    hits = __import__("app.services.ai.blog_final_checks", fromlist=["x"]).final_lint(pipe._article_text(art), {})
    assert [h["sentence"] for h in hits] == ["- Lancaster City: 41.3186 mills", "- Lancaster Township: 30.5886 mills"]
    assert pipe._cut_target(art, hits[0]["sentence"]) == "Lancaster City: 41.3186 mills"
    assert pipe._cut_target(art, "Lancaster City") is None  # never part of an item
    pipe._apply_fixes(art, [{"find": pipe._cut_target(art, h["sentence"]), "replace": ""} for h in hits])
    assert art["sections"][0]["blocks"] == [{"type": "paragraph", "text": "Each layer sets its own rate."}]


def test_text_shares_never_crash_the_research_step():
    # 11/01 Annapolis: grouped small towns carry share "under 1" - the research step compared it to 5.
    assert pipe.share_at_least({"name": "other incorporated towns (Highland Beach)", "share": "under 1", "sort": 0.4}, 5) is False
    assert pipe.share_at_least({"name": "Annapolis City", "share": "42", "sort": 42.3}, 5) is True
    assert pipe.share_at_least({"name": "Hopewell Township", "share": 12}, 5) is True
    assert pipe.share_at_least({"name": "x", "share": None}, 5) is False


def test_tax_items_carry_no_percent_of_price():
    from types import SimpleNamespace
    area = SimpleNamespace(name="Princeton", municipality_type=None, area_type="city", state="NJ", id="a1")
    cost = {"as_of": "2026-11-01", "tax": {"homes": 120, "median_annual": 16234, "tax_year": 2025, "median_pct_of_list": 1.65, "placed": 110,
                                            "by_township": [{"township": "Hopewell Township", "homes": 30, "median_pct_of_list": 1.52},
                                                            {"township": "Princeton", "homes": 50, "median_pct_of_list": 1.54}]}}
    claims = [i["claim"] for i in tk.cost_items(area, cost)]
    assert claims and not any("%" in c for c in claims)
    assert any("$16,234" in c for c in claims) and any("substantially different tax bills" in c for c in claims)


# --- 11/01 grader round 4: everything fits this reader; closing taxes named only; places never complete ---
def test_investment_articles_get_no_payment_card_and_no_occupant_only_programs():
    rental = {"topic": "Buying your first rental property in Wilmington, DE", "audiences": ["Investors", "Buyers"]}
    home = {"topic": "Buying a condo in Arlington, VA", "audiences": ["First-time buyers", "Buyers", "Investors"]}
    assert pipe.investment_article(rental) and not pipe.investment_article(home)
    assert not pipe.wants_payment_example(rental) and pipe.wants_payment_example(home)
    items = [{"claim": "Delaware's first-time homebuyer transfer tax reduction requires the buyer to occupy the home as a principal residence.", "origin": "web_research"},
             {"claim": "Wilmington requires rental properties to be licensed and inspected.", "origin": "web_research"},
             {"claim": "Owner-occupied share of homes of Wilmington city: 45%", "origin": "idx_research", "topic": "demographics"}]
    kept = [i["claim"][:20] for i in pipe.fit_to_reader(items, rental)]
    assert kept == ["Wilmington requires ", "Owner-occupied share"]
    assert len(pipe.fit_to_reader(items, home)) == 3


def test_closing_tax_rates_are_fine_print_and_wrong_reader_is_checked():
    assert "the usual transfer-tax percentage" not in pipe._ENGINE_RULES and "never their rates or amounts" in pipe._ENGINE_RULES
    assert "EVERYTHING MUST FIT THIS READER" in pipe._ENGINE_RULES and "wrong_reader" in str(pipe._REVIEW_TOOL) and "wrong_reader (" in pipe._REVIEW_SYSTEM


def test_places_are_never_presented_as_a_complete_list(monkeypatch):
    g = _profile(monkeypatch, "DE", [("Wilmington City", 42), ("Unincorporated New Castle County", 58)],
                 [("Red Clay Consolidated School District", 40), ("Brandywine School District", 30)], [], [])
    geo, schools = g["items"][0]["claim"], g["items"][1]["claim"]
    # Round 8: an unmeasured "Unincorporated ..." row is the leftover - no share, and it says what else it holds.
    assert "unincorporated areas or other incorporated towns" in geo and "58%" not in geo
    assert "in our records include" in schools and "Others may serve some addresses" in schools
