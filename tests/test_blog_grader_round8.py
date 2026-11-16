"""Grader round 8 (11/15: Media 88 / Media township checklist 76 / Malvern 88; the round-7 train-time findings were
withdrawn by the grader). What was checked against the official pages, and what it showed:

  7  (Critical) REAL. A Media article - Upper Providence Township, DELAWARE County - stated a sewer-billing rule read
     on upperprovmcpa.gov, the site of Upper Providence Township, MONTGOMERY County. A real page, about another
     municipality of the same name; it was also stored as a fact about Delaware County's.
  8, 9  REAL. Marple Township's resale application says "at least two (2) weeks prior to settlement" and "$200";
     Malvern Borough's sewer certification takes 10 business days. Research had read the page that links the form.
  10 REAL. The reader's sources list was the first 14 in ledger order; the township pages came last and fell off.
  12 REAL (small). "takes a minimum of two weeks" was written as "apply at least two weeks before".
No network and no database."""
import asyncio
from types import SimpleNamespace

from app.services.ai import ai_blog_pipeline_service as pipe
from app.services.areas import town_knowledge_service as tk

MEDIA = {"town": "Media", "counties": ["Delaware County"], "townships": [
    {"name": "Middletown Township", "share": 39, "sort": 38.6, "county": "Delaware County", "kind": "township"},
    {"name": "Upper Providence Township", "share": 23, "sort": 22.8, "county": "Delaware County", "kind": "township"},
    {"name": "Media Borough", "share": 16, "sort": 16.5, "county": "Delaware County", "kind": "place"},
    {"name": "Marple Township", "share": 8, "sort": 7.6, "county": "Delaware County", "kind": "township"}]}
MUNIS = tk.municipalities([MEDIA])
UPPER = next(m for m in MUNIS if m["name"] == "Upper Providence Township")
WRONG = {"town": "Upper Providence Township", "topic": "regulation", "source_url": "https://www.upperprovmcpa.gov/401/Sewer-Services",
         "claim": "Upper Providence Township's Sewer Services page says sewer bills are pro-rated at settlement by the title company, using a Sewer "
                  "Certification provided by the billing administrator (Berkheimer)."}


# ----------------------------------------------------------------------------- 7. whose page is it?

def test_a_same_named_township_in_another_county_is_not_this_township():
    # the page's own county, as research read it
    said = {**WRONG, "source_url": "https://www.example.gov/sewer", "jurisdiction": "Upper Providence Township, Montgomery County, PA"}
    assert tk.jurisdiction_problem(said, UPPER) == "Montgomery County, not Delaware County"
    # the web address alone gives it away, even if research said nothing ("mcpa" = Montgomery County, PA)
    assert tk.jurisdiction_problem(WRONG, UPPER) == "Montgomery County, not Delaware County"
    # a county named in the claim
    named = {**WRONG, "source_url": "https://www.example.gov/x", "claim": "Upper Providence Township, Montgomery County, bills sewer quarterly."}
    assert "Montgomery" in tk.jurisdiction_problem(named, UPPER)
    # the right county's pages pass
    assert tk.jurisdiction_problem({**WRONG, "source_url": "https://www.upperprovidence.org/161/Sewer-Authority"}, UPPER) is None
    assert tk.jurisdiction_problem({"source_url": "https://middletowndelcopa.gov/x", "claim": "Middletown Township requires..."}, MUNIS[0]) is None
    assert tk.jurisdiction_problem({**said, "jurisdiction": "Upper Providence Township, Delaware County, PA"}, UPPER) is None


def test_a_municipal_fact_is_used_only_when_tied_to_this_county():
    held = [{"municipality": "Upper Providence Township", "source_url": "https://www.upperprovidence.org/231/FAQs", "claim": "x"},
            {"municipality": "Marple Township", "source_url": "https://ecode360.com/10773544", "claim": "y"}]
    items = [
        WRONG,                                                                                                         # another county's: out
        {"town": "Upper Providence Township", "source_url": "https://www.upperprovidence.org/161/Sewer-Authority", "jurisdiction": "n/a",
         "claim": "Upper Providence Township Sewer Authority lists a sewer certification fee."},                        # a site we already hold: in
        {"town": "Marple Township", "source_url": "https://www.marpletwp.com/DocumentCenter/View/1008/Resale-Application",
         "jurisdiction": "Marple Township, Delaware County, PA", "claim": "Marple Township's resale application must be submitted two weeks before settlement."},   # the page's own county: in
        {"town": "Marple Township", "source_url": "https://ecode360.com/99999", "jurisdiction": "n/a", "claim": "Marple Township Code requires an inspection."},   # a code site that hosts every town, no county: out
        {"town": "Media Borough", "source_url": "https://somewhere.example.org/co", "claim": "Media Borough requires a certificate."},    # nothing ties it: out
        {"town": "general", "source_url": "https://www.pa.gov/x", "claim": "Pennsylvania's realty transfer tax is 1% to the state."},      # not municipal: untouched
    ]
    kept, notes = tk.only_this_jurisdiction(items, MUNIS, held)
    assert [i["source_url"].split("/")[2] for i in kept] == ["www.upperprovidence.org", "www.marpletwp.com", "www.pa.gov"]
    assert kept[0]["municipality"] == "Upper Providence Township" and "municipality" not in kept[2]
    assert len(notes) == 3 and "A page for a different Upper Providence Township was found and NOT used (Montgomery County, not Delaware County)" in notes[0]
    assert "Could not confirm that this page belongs to Marple Township, Delaware County" in notes[1] and "Media Borough" in notes[2]
    assert tk.only_this_jurisdiction(items, [], held) == (items, [])             # an article with no municipalities: nothing to judge


class _Db:
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


def test_another_countys_page_is_never_stored_and_never_handed_over():
    db = _Db()
    good = {"topic": "regulation", "source_url": "https://www.upperprovidence.org/183/Permits", "jurisdiction": "Upper Providence Township, Delaware County, PA",
            "claim": "Upper Providence Township asks for the U&O application three weeks before settlement."}
    assert asyncio.run(tk.save_muni_items(db, "PA", UPPER, [WRONG, good])) == 1                 # the Montgomery County page is refused
    held = asyncio.run(tk.muni_items(db, "PA", MUNIS))
    assert [i["source_url"] for i in held] == [good["source_url"]] and held[0]["jurisdiction"].endswith("Delaware County, PA")
    # one that got in before the check existed (as it did on 11/15) is filtered on the way out
    db.rows[tk.muni_scope("PA", UPPER)].items.append({**WRONG, "id": "old", "saved_at": "2026-11-15T16:07:00+00:00"})
    assert [i["source_url"] for i in asyncio.run(tk.muni_items(db, "PA", MUNIS))] == [good["source_url"]]


def test_research_is_asked_for_the_county_and_given_it():
    props = pipe._EVIDENCE_TOOL["input_schema"]["properties"]["items"]["items"]
    assert "jurisdiction" in props["required"] and "COUNTY" in props["properties"]["jurisdiction"]["description"]
    rules = pipe._RESEARCH_SYSTEM
    assert "WHOSE PAGE IS IT?" in rules and "Put the COUNTY in every search for a township, borough, city or authority" in rules   # "city" added 11/16 (round 12, Wilmington DE)
    assert "do NOT report it" in rules
    import inspect
    src = inspect.getsource(pipe._run)
    assert "tk.only_this_jurisdiction(research_items, munis, muni_known)" in src            # before anything is used or stored
    assert src.index("tk.only_this_jurisdiction(") < src.index("tk.save_muni_items(db, state, m, mine)")
    assert "m['county']" in src                                                              # the look-up list names each municipality's county


# ----------------------------------------------------------------------------- 8, 9. the form was never opened

def _held(muni, claim, url="https://example.gov/x"):
    return {"municipality": muni, "claim": claim, "source_url": url}


def test_a_requirement_with_no_lead_time_is_sent_back_to_open_the_form():
    items = [
        _held("Marple Township", "Marple Township Code: all dwellings must be inspected and approved prior to the sale, transfer or settlement of any dwelling; "
                                 "the department inspects resale properties for sidewalks and lists a Resale Application form (resale inspection)."),
        _held("Upper Providence Township", "Upper Providence Township requires a Use & Occupancy certificate for the re-sale of a home."),
        _held("Upper Providence Township", "Upper Providence Township asks that the U&O application be submitted a minimum of 3 weeks prior to settlement."),
        _held("Middletown Township", "Middletown Township's FAQ says the township does not require a Certificate of Occupancy on existing residential buildings that are sold."),
        _held("Middletown Township", "Middletown Township Sewer Authority: all properties served by public sewers must have the building sewer inspected before a "
                                     "Sanitary Sewer Certification is issued; inspection and certification require a minimum of two weeks."),
        _held("Media Borough", "Media Borough requires a sewer certification for real estate transactions."),
    ]
    gaps = tk.lead_time_gaps(MUNIS, items)
    assert len(gaps) == 2
    assert gaps[0].startswith("Media Borough, Delaware County: how far before settlement its sewer certification or sewer lateral inspection must be applied for")
    assert gaps[1].startswith("Marple Township, Delaware County: how far before settlement its resale / use-and-occupancy certificate must be applied for")
    assert all("open the application form or instruction sheet itself (a page read)" in g and "not checked" in g for g in gaps)
    # Upper Providence has its three weeks, Middletown needs no occupancy certificate and has its two weeks for the sewer one
    assert not any("Upper Providence" in g or "Middletown" in g for g in gaps)
    assert tk.lead_time_gaps(MUNIS, []) == [] and tk.lead_time_gaps([], items) == []
    # those gaps earn a second pass even when research itself reported nothing as unchecked, with one search left
    lookups = [f"web_search: q{i}" for i in range(7)]
    again = pipe.research_follow_up(["Official millage page not located"], lookups, 8, 3, extra=gaps)
    assert again and again["gaps"] == gaps and again["searches"] == 1 and again["fetches"] == 3
    assert pipe.research_follow_up([], [f"web_search: q{i}" for i in range(8)], 8, 3, extra=gaps) is None      # nothing left to look with
    assert pipe.research_follow_up(["Official millage page not located"], lookups, 8, 3) is None               # unchanged without them
    rules = pipe._RESEARCH_SYSTEM
    assert "are usually on the APPLICATION FORM or instruction sheet" in rules and "open the form (a page read)" in rules


# ----------------------------------------------------------------------------- 12. what kind of time

def test_a_processing_time_is_not_written_as_a_filing_deadline():
    assert "Say what KIND of time a page gives" in pipe._RESEARCH_SYSTEM and "Never turn one kind into another" in pipe._RESEARCH_SYSTEM
    seller = pipe._coverage({"topic": "Selling your Media home", "audiences": ["Sellers"]}, 2)
    assert "never write a processing time as a filing deadline" in seller and "state EVERY one that item gives" in seller


# ----------------------------------------------------------------------------- 10. the sources the reader sees

def test_the_reader_sees_every_source_the_article_leans_on():
    """A seller article cited 28 sources and showed the first 14 - the township pages, last in the ledger, were cut."""
    import inspect
    assert pipe.SOURCES_SHOWN >= 30
    src = inspect.getsource(pipe._run)
    assert "public_sources[:SOURCES_SHOWN]" in src and "public_sources[:14]" not in src


# ----------------------------------------------------------------------------- only what we are sure of is a number

def test_a_number_from_a_township_page_needs_two_separate_readings():
    """Owner 11/15: "be more broad about it - mentioning that it exists, not exact numbers, if we aren't for sure"."""
    url = "https://www.mediaborough.com/DocumentCenter/View/139"
    first = {"municipality": "Media Borough", "source_url": url, "saved_at": "2026-11-14T20:00:00+00:00",
             "claim": "Media Borough's procedure lists a fee of $100 plus $10 per unit and says to schedule the inspection at least 15 days prior to settlement."}
    second = {"municipality": "Media Borough", "source_url": url, "saved_at": "2026-11-15T14:24:00+00:00",
              "claim": "Media Borough: mail the application and check at least 20 days before settlement; fee $100 plus $10 per unit; call at least 15 days before settlement."}
    assert tk.specifics(second["claim"]) == {"20 day", "15 day", "$100", "$10"}
    assert tk.specifics("THIS FORM MUST BE SUBMITTED AT LEAST TWO (2) WEEKS PRIOR TO SETTLEMENT; ten (10) business days; $200.00") == {"2 week", "10 day", "$200"}
    assert tk.specifics("Sold within the past five years; the 2026 budget.") == set()             # not something a seller acts on
    assert tk.confirmed_specifics([first]) == set()                                                # one reading: nothing is sure
    sure = tk.confirmed_specifics([first, second])
    assert sure == {"15 day", "$100", "$10"}                                                       # both readings gave these; "20 days" only one
    told, held = tk.mask_unconfirmed(second["claim"], sure)
    assert held == 1 and "20 days" not in told and "15 days" in told and "not yet confirmed: do NOT state a number" in told
    # a reading made in THIS run counts as the second one
    fresh = {"municipality": "Media Borough", "source_url": url, "claim": "Media Borough: submit the application at least 20 days before settlement."}
    assert "20 day" in tk.confirmed_specifics([second, fresh])
    # two items from one run are one reading; the same number on a DIFFERENT page is not a second reading of this one
    same_run = {**second, "saved_at": "2026-11-15T14:30:00+00:00", "claim": "Media Borough asks for the application 20 days before settlement."}
    assert "20 day" not in tk.confirmed_specifics([second, same_run])
    assert "20 day" not in tk.confirmed_specifics([second, {**fresh, "source_url": "https://other.example.gov/x"}])
    # the page is worth one more read until its numbers are confirmed
    assert tk.unconfirmed_pages([first, second]) == [{"municipality": "Media Borough", "url": url}]
    assert tk.unconfirmed_pages([first, {**first, "saved_at": "2026-11-15T14:24:00+00:00"}]) == []


def test_reading_the_same_words_again_is_remembered_as_a_second_reading():
    db = _Db()
    marple = next(m for m in MUNIS if m["name"] == "Marple Township")
    form = {"topic": "regulation", "source_url": "https://www.marpletwp.com/DocumentCenter/View/1008/Resale-Application",
            "claim": "Marple Township's resale application must be submitted at least two (2) weeks prior to settlement; the fee is $200."}
    assert asyncio.run(tk.save_muni_items(db, "PA", marple, [form])) == 1
    row = db.rows[tk.muni_scope("PA", marple)]
    row.items[0]["saved_at"] = "2026-11-10T12:00:00+00:00"                                       # read five days ago
    assert tk.confirmed_specifics(asyncio.run(tk.muni_items(db, "PA", MUNIS))) == set()
    assert asyncio.run(tk.save_muni_items(db, "PA", marple, [form])) == 0                        # the same words, read again today
    held = asyncio.run(tk.muni_items(db, "PA", MUNIS))
    assert len(held) == 1 and len(held[0]["reads"]) == 1
    assert tk.confirmed_specifics(held) == {"2 week", "$200"}


def test_the_writer_never_sees_an_unconfirmed_number():
    import inspect
    src = inspect.getsource(pipe._run)
    mask, bank, build = src.index("tk.mask_unconfirmed("), src.index("tk.save_muni_items(db, state, m, mine)"), src.index("_build_ledger(ordered)")
    assert bank < mask < build                                # stored in full, masked before the writer's ledger is built
    assert "tk.unconfirmed_pages(muni_known)" in src          # and each such page is put up for one more read
    seller = pipe._coverage({"topic": "Selling your Media home", "audiences": ["Sellers"]}, 2)
    assert "give NO number for it" in seller and "a number we have read only once is not stated" in seller


# ----------------------------------------------------------------------------- the site's own checker knows the rule

HELD = [{"topic": "regulation", "origin": "state_bank", "municipality": "Marple Township", "source_url": "https://www.marpletwp.com/1008/Resale-Application",
         "claim": "Marple Township's resale application must be submitted at least " + tk._NOT_CONFIRMED + " prior to settlement; the fee is " + tk._NOT_CONFIRMED + "."},
        {"topic": "regulation", "origin": "state_bank", "municipality": "Upper Providence Township", "source_url": "https://www.upperprovidence.org/resale",
         "claim": "Upper Providence Township asks for the resale certificate application at least 3 weeks before settlement."},
        {"topic": "market", "origin": "mls_live", "claim": "Media: homes sold in a median of 12 days over the last 90 days; the median price was $585,000."}]
SELLER = {"topic": "Selling your Media home", "audiences": ["Sellers"], "personal_take": "Price for the first weekend, not for the third month."}


def test_a_number_we_read_only_once_is_caught_in_the_article():
    bad = ("Marple Township requires a resale certificate, and its application is due at least two weeks before settlement. "
           "The township charges $200 for the resale certificate. "
           "Upper Providence Township asks for its resale certificate application at least three weeks before settlement.")
    found = pipe.unconfirmed_number_claims(bad, HELD, SELLER)
    assert [n for _s, n in found] == ["two weeks", "$200"]                 # Upper Providence's three weeks is confirmed: left alone
    # as real articles word it (round 8, Media): no certificate named in the sentence at all
    real = ("It currently asks you to mail the application and check at least 20 days before settlement. "
            "It asks that the application be filed at least 30 days before the initial date of occupancy. "
            "The Borough currently asks for inspection requests at least 14 days before settlement. "
            "It currently asks that the application go in at least three weeks before settlement, as its permits page explains.")
    assert [n for _s, n in pipe.unconfirmed_number_claims(real, HELD, SELLER)] == ["20 days", "30 days", "14 days"]
    problems = [p for p in pipe.code_problems(bad, brief=SELLER, protections=[], ledger=HELD) if p["kind"] == "unconfirmed_number"]
    assert len(problems) == 2 and problems[0]["pass"] == "fine_print" and not problems[0]["cut"]
    assert "NO number" in problems[0]["problem"] and "check it on the township's page" in problems[0]["note"]


def test_the_broad_wording_and_every_other_number_pass():
    fine = ("Marple Township requires a resale certificate before settlement, so start the application well ahead and check the township's "
            "page for the current timing and fee. "
            "Upper Providence Township asks for its resale certificate application at least three weeks before settlement. "
            "Media homes sold in a median of 12 days, so have the certificate under way before you list. "                 # a market number
            "A sewer lateral repair can run $8,000 or more, so ask early. "                                             # a cost, not a fee
            "Buyers usually schedule their own home inspection within 10 days of signing.")                              # not a municipal step
    assert pipe.unconfirmed_number_claims(fine, HELD, SELLER) == []
    # nothing held back in the ledger: the check stays out of the way entirely
    confirmed_only = [HELD[1], HELD[2]]
    assert pipe.unconfirmed_number_claims("The resale certificate takes 45 days.", confirmed_only, SELLER) == []
    assert pipe.unconfirmed_number_claims("The resale certificate takes 45 days.", [], None) == []


def test_the_final_grader_does_not_fail_an_article_for_leaving_the_number_out():
    g = pipe._GRADE_SYSTEM
    assert 'says a lead time or fee is "not yet confirmed"' in g and "never fail any item because that number is missing" in g
    assert "a number the article gives for it anyway is a fine_print failure" in g
    assert "unconfirmed_number_claims(text, ledger or [], brief)" in __import__("inspect").getsource(pipe.code_problems)


# ----------------------------------------------------------------------------- nothing is handed to the agent to fix

def _article(*sentences):
    return {"title": "Selling your Media home", "intro": ["Here is what to line up before you list."],
            "sections": [{"heading": "The township paperwork", "blocks": [{"type": "paragraph", "text": " ".join(sentences)}]}]}


def _problems(article):
    return lambda: pipe.code_problems(pipe._article_text(article), brief=SELLER, protections=[], ledger=HELD)


def test_a_leftover_number_is_reworded_by_code_not_handed_to_the_agent():
    assert pipe.soften_unconfirmed("It currently asks for the application and fee at least two weeks before the scheduled settlement date.") == (
        "It currently asks for the application and fee well before the scheduled settlement date.")
    assert pipe.soften_unconfirmed("File it ten (10) business days prior to settlement.") == "File it well before settlement."
    assert pipe.soften_unconfirmed("The township charges $200 for the resale certificate.") is None          # a fee: nothing to soften
    a = _article("Marple Township requires a [resale certificate](https://www.marpletwp.com/1008/Resale-Application) before settlement.",
                 "It currently asks for the application and fee at least two weeks before the scheduled settlement date.",
                 "The township charges $200 for the resale certificate.",
                 "Upper Providence Township asks for its resale certificate application at least three weeks before settlement.")
    n, fixed = pipe.settle_unconfirmed(a, _problems(a))
    text = a["sections"][0]["blocks"][0]["text"]
    assert n == 2 and len(fixed) == 2 and all(f["fixed"] for f in fixed)
    assert "well before the scheduled settlement date" in text and "two weeks" not in text                   # reworded
    assert "$200" not in text                                                                                  # a fee can only go
    assert "[resale certificate](https://www.marpletwp.com/1008/Resale-Application)" in text                 # the requirement and its link stay
    assert "at least three weeks before settlement" in text                                                    # a confirmed number is untouched
    assert [p for p in _problems(a)() if p["kind"] == "unconfirmed_number"] == []
    assert "  " not in text and " ." not in text


def test_settling_runs_last_and_before_the_agent_is_told_anything():
    import inspect
    src = inspect.getsource(pipe._run)
    assert src.index("settle_leftovers(article, problems_now)") < src.index("final_left = problems_now()") < src.index('"open_issues"')

