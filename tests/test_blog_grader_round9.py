"""Grader round 9 (11/15 evening: Media 90 / Cherry Hill, NJ 84 - the first round made through the site's own screens)
and the owner the same hour: "we shouldn't have info like phone number and email - just say where to get it ... it's
the quality of the actual article". What each finding turned out to be:

  owner  REAL. The Media article printed an inspector's phone number (put there by the fact-checker, from the ledger);
         the Cherry Hill one printed the township office's street address and room number.
  A1     REAL. The built-in New Jersey attorney-review wording is limited to contracts "prepared by a real estate
         agent"; the writer reworded it as "once the contract is signed" - every contract.
  B1, C3 NOT the engine: the grader read our text export. The real town card is headed "Main school districts" with
         "other districts serve some"; the real video card shows the video's own title. The export was corrected.
  B2     NOT added: New Jersey's Private Well Testing Act could not be read on the state's own page today, and a
         built-in rule is only added from the official page.
  B3     REAL (small). A partial checklist with equipment specifications ("sealed 10-year smoke alarms").
  B4     REAL as a product gap; owner 11/15: "just add the fix you think would work". One question on the writer's last
         step, only when the town is outside the agent's states: "Are you licensed in New Jersey?" No -> the article
         says so and offers an introduction to an agent who is.
  B5     On purpose (the two-readings rule).
  B6     REAL. The reader's sources list showed a date the AI gave for a web page.
  C1, C2 REAL (style). "This winter" in the title, nothing about winter in the article.
No network and no database."""
import inspect

from app.services.ai import ai_blog_pipeline_service as pipe
from app.services.ai import blog_final_checks as bfc

SELLER = {"topic": "Selling your Cherry Hill home", "audiences": ["Sellers"], "personal_take": "Price for the first weekend."}
LEDGER = [{"topic": "agent", "origin": "agent_sales", "claim": "The agent sold 112 Wilton Woods Ln, Media for $1,310,000 in March 2025."},
          {"topic": "market", "origin": "mls_live", "claim": "Cherry Hill: 196 closed sales, median $510,000, in the 90 days ending 2026-11-14."}]


# ----------------------------------------------------------------------------- contact details

def test_contact_details_are_found_and_taken_out_cleanly():
    kinds = lambda t: [k for _f, k in pipe.contact_details(t)]                                    # noqa: E731
    assert kinds("Call Linn Architects (610-566-7044) to schedule.") == ["a phone number"]
    assert kinds("Email codes@chnj.gov or call 856.488.7870 x123.") == ["a phone number", "an email address"]
    assert kinds("Apply in person at 820 Mercer Street, Room 202, or by mail.") == ["a street address"]
    # not contact details: prices, dates, counts, code sections, a link's own address
    for fine in ("The median was $646,000 on 2026-11-14 across 86 sales.", "See N.J.A.C. 5:70-2.3(a) and 68 Pa.C.S. Chapter 73.",
                 "The form is [here](https://www.marpletwp.com/DocumentCenter/View/1008/Resale-Application).", "About 38% are in Middletown Township.",
                 "352 homes sold near State Street last year."):
        assert pipe.contact_details(fine) == [], fine
    out = pipe.without_contact_details
    assert out("Once you've submitted the application with payment, you call Linn Architects (610-566-7044) to schedule the inspection.") == (
        "Once you've submitted the application with payment, you call Linn Architects to schedule the inspection.", 1)
    assert out("You can apply online, in person at 820 Mercer Street, Room 202, or by mail.") == ("You can apply online, in person, or by mail.", 1)
    assert out("Questions go to the code office at codes@chnj.gov.") == ("Questions go to the code office.", 1)
    assert out("Nothing to remove here.") == ("Nothing to remove here.", 0)
    assert out("My sale at 112 Wilton Woods Ln closed in March.", keep="112 Wilton Woods Ln, Media") == ("My sale at 112 Wilton Woods Ln closed in March.", 0)


def test_the_writer_is_never_given_a_contact_detail_from_a_page_we_read():
    src = inspect.getsource(pipe._run)
    strip, build = src.index("without_contact_details(i.get(\"claim\"))"), src.index("_build_ledger(ordered)")
    assert strip < build and 'if i.get("origin") in WEB_READ' in src                 # web-read facts only; the store keeps the full wording
    assert pipe.WEB_READ == ("web_research", "state_bank", "evidence_bank", "idx_research")
    assert "Never print a phone number, an email address, or the street address, room number or opening hours" in pipe._ENGINE_RULES
    assert "Do NOT report phone numbers, email addresses, street addresses" in pipe._RESEARCH_SYSTEM


def test_a_contact_detail_in_the_article_is_caught_and_removed_by_code():
    article = {"title": "Selling in Cherry Hill", "intro": ["What to line up first."], "sections": [{"heading": "Paperwork", "blocks": [
        {"type": "paragraph", "text": "Upper Providence Township issues the certificate after a resale inspection. Once you've applied, you call Linn "
                                      "Architects (610-566-7044) to schedule the inspection. You can apply online, in person at 820 Mercer Street, Room 202, "
                                      "or by mail. A recent sale of mine, 112 Wilton Woods Ln, sold in March 2025."}]}]}
    find = lambda: pipe.code_problems(pipe._article_text(article), brief=SELLER, protections=[], ledger=LEDGER)      # noqa: E731
    found = [p for p in find() if p["kind"] == "contact_detail"]
    assert len(found) == 2 and found[0]["pass"] == "fine_print" and "say whose page has it" in found[0]["problem"]   # the agent's own sale is not one
    n, fixed = pipe.settle_leftovers(article, find)
    text = article["sections"][0]["blocks"][0]["text"]
    assert n == 2 and all(f["fixed"] for f in fixed)
    assert "610" not in text and "Mercer" not in text and "Room 202" not in text
    assert "you call Linn Architects to schedule the inspection." in text and "You can apply online, in person, or by mail." in text
    assert "112 Wilton Woods Ln" in text
    assert [p for p in find() if p["kind"] == "contact_detail"] == []


# ----------------------------------------------------------------------------- A1: a protection keeps its limits

def test_a_protection_that_lost_its_limits_is_caught_and_put_back():
    nj = [p for p in bfc.PROTECTIONS if p["key"] == "nj_attorney_review"]
    wide = ("Once the contract is signed, New Jersey's attorney review period lets either side's attorney review it, ask for changes or cancel, "
            "so a signed contract isn't final right away.")
    assert [s for s, _p in pipe.broadened_protections(wide, nj)] == [wide]
    for kept in (nj[0]["says"],
                 "In New Jersey, most home sale contracts go through attorney review, when either side's attorney can ask for changes or cancel.",
                 "A broker-prepared contract in New Jersey carries an attorney review period before it is final.",
                 wide + " That attorney review applies to a contract prepared by a real estate agent, which most are."):      # one sentence that keeps it is enough
        assert pipe.broadened_protections(kept, nj) == [], kept
    assert pipe.broadened_protections("### Attorney review\n\n[Ask-me line: \"Ask me about attorney review\" + Call/Text/Email/Message]", nj) == []
    assert pipe.broadened_protections(wide, [p for p in bfc.PROTECTIONS if p["key"] == "pa_disclosure"]) == []         # no `scope`: not looked at
    article = {"title": "Selling in Cherry Hill", "intro": ["What to line up first."], "sections": [{"heading": "State paperwork", "blocks": [
        {"type": "paragraph", "text": "New Jersey sellers generally give buyers a disclosure statement. " + wide}]}]}
    find = lambda: pipe.code_problems(pipe._article_text(article), brief=SELLER, protections=nj, ledger=LEDGER)        # noqa: E731
    (p,) = [x for x in find() if x["kind"] == "protection_scope"]
    assert p["pass"] == "protections" and nj[0]["says"] in p["problem"]
    n, fixed = pipe.settle_leftovers(article, find)
    assert n == 1 and nj[0]["says"] in article["sections"][0]["blocks"][0]["text"] and "Once the contract is signed" not in pipe._article_text(article)
    assert "keeps EVERY limit of its ledger wording" in pipe._ENGINE_RULES


# ----------------------------------------------------------------------------- B3, B6, C1/C2

def test_checklists_are_examples_and_a_seasonal_title_is_served():
    r = pipe._ENGINE_RULES
    assert "never equipment specifications (which kind of alarm, battery, wiring or size)" in r and "link the full checklist" in r
    assert "THE TITLE'S PROMISE" in r and "no statistic the ledger doesn't hold" in r


def test_a_web_page_in_the_sources_list_shows_the_day_we_read_it_only():
    src = inspect.getsource(pipe._run)
    assert '"as_of": None if s.get("origin") in WEB_READ and s.get("read") else _public_date(s.get("as_of"))' in src


def test_the_last_step_settles_all_three_kinds_and_still_runs_last():
    assert set(pipe._SETTLED) == {"unconfirmed_number", "contact_detail", "protection_scope"}
    src = inspect.getsource(pipe._run)
    assert src.index("settle_leftovers(article, problems_now)") < src.index("final_left = problems_now()") < src.index('"open_issues"')
    assert pipe.settle_unconfirmed is pipe.settle_leftovers


# ----------------------------------------------------------------------------- B4: licensed there?

def test_an_agent_not_licensed_there_offers_an_introduction_not_their_own_services():
    note = pipe.license_note({"PA"}, ["NJ"])
    assert note == ("One thing to know: I'm licensed in Pennsylvania, not in New Jersey, so for a home there I'll introduce you to an agent who is "
                    "licensed in New Jersey.")
    assert pipe.state_names(["DE", "NJ"]) == "Delaware and New Jersey" and pipe.state_names(["XX"]) == "XX"
    body = "Cherry Hill sellers need two certificates before settlement. Ask me and I'll walk you through both."
    # asked and answered No: the sentence is required ...
    (p,) = [x for x in pipe.code_problems(body, brief=SELLER, protections=[], ledger=LEDGER, not_licensed=note) if x["kind"] == "missing_license_note"]
    assert p["pass"] == "next_step" and note in p["problem"] and p["missing"]
    # ... and any plain way of saying it satisfies the check
    for said in (note, "I'm not licensed in New Jersey, so I'll connect you with an agent there I trust.", "For a New Jersey sale I'd refer you to an agent licensed there."):
        assert [x for x in pipe.code_problems(body + " " + said, brief=SELLER, protections=[], ledger=LEDGER, not_licensed=note) if x["kind"] == "missing_license_note"] == []
    # never asked, or answered Yes: nothing is required
    assert [x for x in pipe.code_problems(body, brief=SELLER, protections=[], ledger=LEDGER) if x["kind"] == "missing_license_note"] == []


def test_the_licence_sentence_is_added_by_code_when_no_step_wrote_it():
    note = pipe.license_note({"PA"}, ["NJ"])
    article = {"title": "Selling in Cherry Hill", "intro": ["What to line up first."],
               "sections": [{"heading": "Paperwork", "blocks": [{"type": "paragraph", "text": "Two certificates are needed before settlement."}]}],
               "closing": {"heading": "The bottom line", "paragraphs": ["Start the paperwork early."]}}
    assert pipe.ensure_license_note(article, note) is True and article["closing"]["paragraphs"][-1] == note
    assert pipe.ensure_license_note(article, note) is False and article["closing"]["paragraphs"].count(note) == 1            # once only
    bare = {"title": "T", "sections": [{"heading": "Only section", "blocks": []}]}
    assert pipe.ensure_license_note(bare, note) is True and bare["sections"][-1]["blocks"][-1]["text"] == note


def test_the_question_is_asked_only_for_a_town_outside_the_agents_states_and_reaches_the_writer():
    import asyncio
    from types import SimpleNamespace
    from app.api import blog_routes
    from app.schemas.blogs import BlogAiBrief

    async def served(db, user, n):
        return [SimpleNamespace(state="PA"), SimpleNamespace(state="pa")]
    real = pipe._served_areas
    pipe._served_areas = served
    try:
        user = SimpleNamespace(state="PA")
        assert asyncio.run(pipe.out_of_area_states(None, user, {"NJ"})) == (["NJ"], {"PA"})
        assert asyncio.run(pipe.out_of_area_states(None, user, {"PA"}))[0] == []
        assert asyncio.run(pipe.out_of_area_states(None, user, {"PA", "NJ"}))[0] == []            # one town at home: not "outside"
        assert asyncio.run(pipe.out_of_area_states(None, user, set()))[0] == []
    finally:
        pipe._served_areas = real
    assert BlogAiBrief(topic="x").licensed_there is None and BlogAiBrief(topic="x", licensed_there=False).licensed_there is False
    run = inspect.getsource(pipe._run)
    assert 'not_licensed = out_of_area if out_of_area and brief.get("licensed_there") is False else []' in run          # Yes / never asked: as before
    assert "NOT LICENSED THERE: the agent has told us they are NOT licensed in" in run and "ensure_license_note(article, license_note(agent_states, not_licensed))" in run
    assert run.index("settle_leftovers(article, problems_now)") < run.index("ensure_license_note(article") < run.index("final_left = problems_now()")
    assert "an offer to introduce the reader to an agent who is licensed there" in pipe._GRADE_SYSTEM
    pre = inspect.getsource(blog_routes.blog_ai_preflight)
    assert '"key": "license", "level": "ask"' in pre and "Are you licensed in {where}?" in pre
    assert 'not any(c["level"] == "block" for c in checks)' in pre                                                         # a question, never a stop


# ----------------------------------------------------------------------------- an article about one county

def test_a_county_article_uses_only_the_agents_towns_inside_that_county():
    from types import SimpleNamespace as NS
    assert pipe.county_named(["Delaware County"]) == "Delaware County" and pipe.county_named(["Living in Chester County, PA"]) == "Chester County"
    assert pipe.county_named(["Philly Main Line & Suburbs"]) is None and pipe.county_named(["Delaware County", "Chester County"]) is None
    assert pipe.county_named(["Prince George's County"]) == "Prince George's County"
    geos = {"media": {"townships": [{"name": "Middletown Township", "sort": 38.6, "county": "Delaware County"}, {"name": "Media Borough", "sort": 16.5, "county": "Delaware County"},
                                    {"name": "Upper Providence Township", "sort": 22.8, "county": "Delaware County"}]},
            "wayne": {"townships": [{"name": "Radnor Township", "sort": 45, "county": "Delaware County"}, {"name": "Tredyffrin Township", "sort": 40, "county": "Chester County"},
                                    {"name": "Upper Merion Township", "sort": 15, "county": "Montgomery County"}]},
            "gladwyne": {"townships": [{"name": "Lower Merion Township", "sort": 100, "county": "Montgomery County"}]},
            "radnor": {"townships": [{"name": "Radnor Township", "sort": 98, "county": "DELAWARE-PA"}, {"name": "other municipalities", "share": "under 1", "county": None}]}}
    served = [NS(slug=k) for k in ("gladwyne", "wayne", "media", "radnor", "new-town")]
    assert pipe.county_share(geos["wayne"], "Delaware County") == 45 and pipe.county_share(None, "Delaware County") == 0
    assert [a.slug for a in pipe.towns_inside(served, geos, "Delaware County")] == ["radnor", "media"]        # Wayne is under half inside; Gladwyne is outside
    assert [a.slug for a in pipe.towns_inside(served, geos, "Montgomery County")] == ["gladwyne"]
    assert pipe.towns_inside(served, geos, "Bucks County") == []                                              # none of the agent's towns: no examples, never the wrong ones
    run = inspect.getsource(pipe._run)
    assert "areas = towns_inside(served, geos, about_county)" in run and "THIS ARTICLE IS ABOUT {about_county.upper()}" in run
    assert "never give a price, count or pace for the county as a whole" in run

