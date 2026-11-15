"""Engine audit, 11/14 - a read of the whole blog engine end to end (not a reaction to one article).
What was wrong in the STEPS, and the rule each fix now holds:

1. "Who is this article for?" was answered by five different word matches that disagreed.
   -> one answer (bfc.reader_sides): the audience the agent picked decides, the words are the fallback.
2. The newest code checks (local sale rules, abatement facts, data age) ran once on the first draft and never again.
   -> one list of code-confirmed problems (pipe.code_problems), re-run after every step that edits the article.
3. The fact-check's and the repair's edits were applied blind; only the grader's were guarded.
   -> every edit goes through _apply_guarded: one that breaks a check or a sentence is undone.
4. A claim the fact-check marked "unsupported" with no fix stayed in the article under a green Verified badge.
   -> it goes to the grader for a second opinion; two agreeing judges get it rewritten or flagged.
5. "Did it answer what the agent asked" was one clause in a long instruction.
   -> the fact-check lists every point of the brief with a status; open ones are graded (pass item "asked").
6. Open flags were listed BEFORE the last fixing step, so fixed problems were still reported.
   -> what code can confirm is reported from the final text.
7. The Verified badge never changed after the agent rewrote the text by hand.
8. Research was never told its budget and spent look-ups on facts we already hold."""
import asyncio
import copy
import sys
from pathlib import Path

root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

import app.main  # noqa: F401,E402
from app.api import blog_routes  # noqa: E402
from app.services.ai import ai_blog_pipeline_service as pipe  # noqa: E402
from app.services.ai import ai_blog_writer_service as writer  # noqa: E402
from app.services.ai import blog_final_checks as bfc  # noqa: E402
from app.services.areas import town_knowledge_service as tk  # noqa: E402


# ------------------------------------------------------------------ 1. one answer to "who is it for"
def test_the_audience_the_agent_picked_decides():
    seller = {"topic": "Selling your Wayne home this winter", "audiences": ["Sellers"],
              "key_points": "What buyers are paying, how long buyers take to decide, whether buyers still need a mortgage contingency"}
    assert bfc.reader_sides(seller) == {"seller"}
    assert not pipe.wants_payment_example(seller)                       # 11/13: "a random $350,000 example in a seller article"
    assert "buyer" not in bfc.article_readers(seller) and "first_time" not in bfc.article_readers(
        {**seller, "key_points": "many of the buyers here are first-time buyers"})

    buyer = {"topic": "Buying a Fishtown rowhome - how quickly homes are selling and what sellers accept", "audiences": ["First-time buyers"]}
    assert bfc.reader_sides(buyer) == {"buyer"}
    assert pipe.wants_payment_example(buyer) and not pipe._for_sellers(buyer)
    assert pipe.local_sale_requirement_checks("Nothing about certificates.", [], buyer) == []
    # a New Jersey BUYING article is no longer told to name the seller's Realty Transfer Fee
    keys = {p["key"] for p in bfc.protections_for({"NJ"}, {**buyer, "topic": "Buying in Cherry Hill - how fast homes are selling"})}
    assert "nj_rtf" not in keys and "nj_attorney_review" in keys


def test_the_headline_adds_the_other_side_only_when_the_reader_does_it_themselves():
    both = {"topic": "Selling a longtime family home in Wayne and buying smaller", "audiences": ["Sellers", "Homeowners"]}
    assert bfc.reader_sides(both) == {"seller", "buyer"}
    assert bfc.reader_sides({"topic": "Should you sell before you buy in Media?", "audiences": ["Buyers"]}) == {"seller", "buyer"}
    assert bfc.reader_sides({"topic": "Best parks in Wayne", "audiences": ["Homeowners"]}) == set()


def test_with_no_audience_the_words_decide_and_market_talk_is_not_a_seller():
    assert bfc.reader_sides({"topic": "Selling in Cherry Hill this winter"}) == {"seller"}
    assert bfc.reader_sides({"topic": "Is selling your home this winter a good idea?"}) == {"seller"}
    assert bfc.reader_sides({"topic": "How to help your home sell faster"}) == {"seller"}
    assert bfc.reader_sides({"topic": "Fishtown market update: how quickly homes are selling"}) == set()
    assert bfc.reader_sides({"topic": "What homes sell for in Kennett Square"}) == set()
    assert bfc.reader_sides({"topic": "Main Line first-time homebuyer guide"}) == {"buyer"}
    assert bfc.reader_sides({"topic": "What Wayne sellers should know about today's buyers"}) == {"seller"}
    assert pipe._SELLER_ARTICLE.search("thinking about selling a Main Line home") and not pipe._SELLER_ARTICLE.search("how fast homes are selling")


# ------------------------------------------------------------------ 2. one list of code checks, re-run every time
PHILLY = {"town": "Fishtown", "counties": ["Philadelphia County"], "norm": {"counties": {"philadelphia"}, "townships": {"philadelphia city"}, "districts": set()}}
ABATE_BRIEF = {"areas": ["Fishtown"], "topic": "Buying a Fishtown rowhome - how the 10-year tax abatement works", "audiences": ["Buyers"]}
GOOD_ABATEMENT = ("On the new-construction abatement there are two schedules. For abatements applied for after December 31, 2021, the untaxed share "
                  "gets smaller every year, so the tax bill goes up a step each year. One applied for on or before that date stays in full for the whole ten years "
                  "and then ends all at once, so ask which abatement the home has. A home with an abatement isn't eligible for the Homestead "
                  "Exemption. The City runs separate abatement programs for new construction and for rehab, so confirm which program a listing has.")


def _problems(text, **kw):
    ledger = tk.philadelphia_abatement_items("PA", [PHILLY], ABATE_BRIEF, today="2026-11-14")
    return pipe.code_problems(text, brief=ABATE_BRIEF, protections=[], ledger=ledger, **kw)


def test_the_abatement_check_is_part_of_the_one_list_and_sees_a_later_deletion():
    assert [p["kind"] for p in _problems(GOOD_ABATEMENT)] == []
    # the fact-check deletes the older-schedule sentence: the SAME check, run again, now reports it
    later = GOOD_ABATEMENT.replace(" One applied for on or before that date stays in full for the whole ten years "
                                   "and then ends all at once, so ask which abatement the home has.", "")
    assert later != GOOD_ABATEMENT
    left = _problems(later)
    assert [p["kind"] for p in left] == ["missing_abatement_fact"]
    p = left[0]
    assert p["missing"] and p["pass"] == "protections" and p["severity"] == "high" and p["quote"] == ""
    assert "TWO schedules" in p["problem"] and p["note"].startswith("The article talks about the abatement but never says")
    assert pipe._problem_line(p).startswith("missing_abatement_fact: the article talks about")


def test_the_one_list_carries_every_kind_of_code_check():
    brief = {"topic": "Selling your Cherry Hill home", "audiences": ["Sellers"]}
    protections = bfc.protections_for({"NJ"}, brief)
    text = ("Right now 41 homes are for sale. Borrowers who put down less than 20% must get mortgage insurance. "
            "The transfer tax is 2.5% of the price. Ask your agent about it. Send [insert your number] today.")
    stale = {"fresh_wording": False, "current_through_label": "Oct 19", "age_days": 26}
    found = pipe.code_problems(text, brief=brief, protections=protections, agent_name="Peter Example", ledger=[], freshness=stale,
                               geo_check=lambda t: [{"sentence": "Right now 41 homes are for sale.", "town": "Cherry Hill", "place": "Burlington County",
                                                     "kind": "county", "truth": "Cherry Hill is in Camden County"}])
    kinds = {p["kind"] for p in found}
    assert kinds >= {"wrong_geography", "overgeneralization", "rule_detail", "missing_protection", "reader_voice", "placeholder",
                     "missing_local_requirement", "stale_data"}
    by = {p["kind"]: p for p in found}
    assert by["rule_detail"]["cut"] and by["wrong_geography"]["cut_final"] and not by["wrong_geography"]["cut"]
    assert not by["overgeneralization"]["cut_final"] and by["stale_data"]["quote"] == "Right now 41 homes are for sale."
    assert by["missing_protection"]["note"].startswith("Not mentioned: ") and by["missing_local_requirement"]["quote"] == ""


# ------------------------------------------------------------------ 3. every edit is guarded
def _article(*paragraphs):
    return {"title": "T", "subtitle": "S", "intro": [], "at_a_glance": [], "closing": {},
            "sections": [{"heading": "Costs", "blocks": [{"type": "paragraph", "text": p} for p in paragraphs]}]}


def _counts(article, brief=None):
    brief = brief or {"topic": "Buying in Wayne", "audiences": ["Buyers"]}
    return lambda: pipe._guard_counts(pipe._article_text(article), pipe.code_problems(pipe._article_text(article), brief=brief, protections=[]), missing=False)


def test_a_deletion_that_leaves_a_broken_sentence_is_undone():
    a = _article("Homes here sold fast, so you may face competition. Plan a second visit early.")
    before = copy.deepcopy(a)
    n, done = pipe._apply_guarded(a, [{"find": "Homes here sold fast", "replace": "", "kind": "unsupported_claim", "severity": "high"}], _counts(a))
    assert n == 0 and a == before and done[0]["fixed"] is False and done[0]["undone"] and done[0]["was_there"]
    # a whole sentence can go
    n, done = pipe._apply_guarded(a, [{"find": "Plan a second visit early.", "replace": "", "kind": "repetition", "severity": "low"}], _counts(a))
    assert n == 1 and done[0]["fixed"] and "second visit" not in pipe._article_text(a)


def test_an_edit_that_breaks_a_rule_that_passed_is_undone_and_a_clean_one_is_kept():
    a = _article("Closing costs include the transfer tax. Ask the settlement company for an itemized estimate.")
    before = copy.deepcopy(a)
    bad = {"find": "Closing costs include the transfer tax.", "replace": "Closing costs include the 2.5% transfer tax.", "kind": "other", "severity": "medium"}
    n, done = pipe._apply_guarded(a, [bad], _counts(a))
    assert n == 0 and a == before and done[0]["undone"]
    good = {"find": "Closing costs include the transfer tax.", "replace": "Closing costs include the realty transfer tax.", "kind": "other", "severity": "medium"}
    never = {"find": "a sentence that is not in the article", "replace": "x", "kind": "other", "severity": "medium"}
    n, done = pipe._apply_guarded(a, [good, never], _counts(a))
    assert n == 1 and [d["fixed"] for d in done] == [True, False] and done[1]["was_there"] is False


def test_broken_sentence_marks():
    count = lambda t: len(pipe._FRAGMENT.findall(t))
    assert count("It sold fast. , so you may face competition.") == 1 and count("Prices rose, .") >= 1
    assert count("The home sold and. Next one.") == 1 and count("See the card ( ) below.") == 1
    assert count("Open 9 a.m. to 5 p.m. daily. A clean sentence, with a comma; and a clause.") == 0


# ------------------------------------------------------------------ 4 + 5 + 6. the grader's second opinion
class _Meter:
    async def record(self, *a, **k):
        return None


def _run_grader(monkeypatch, article, answers, **kw):
    calls = []

    async def fake(*, tool_name, user_msg, **_):
        calls.append((tool_name, user_msg))
        return answers.pop(0)
    monkeypatch.setattr(pipe, "_run_tool_call", fake)
    brief = {"topic": "Buying in Wayne", "audiences": ["Buyers"], "key_points": "the commute to Center City"}
    problems = lambda: pipe.code_problems(pipe._article_text(article), brief=brief, protections=[])
    out = asyncio.run(pipe._grade_and_fix(article=article, ledger_text="[E1] (market) 41 homes for sale.", brief=brief, brief_msg="Topic: Buying in Wayne",
                                          inserted=[], spec={"check_model": "m"}, meter=_Meter(), find_problems=problems, **kw))
    return out, calls


def test_an_unsupported_claim_goes_to_the_grader_and_is_rewritten_when_it_agrees(monkeypatch):
    a = _article("Winter buyers here are always more serious than spring buyers. There were 41 homes for sale.")
    doubt = "Winter buyers here are always more serious than spring buyers."
    (pass_list, _nice, issues, applied), calls = _run_grader(monkeypatch, a, [
        {"items": [{"id": "facts", "status": "fail", "problems": [{"quote": doubt, "why": "no ledger item supports it", "fix": "Say some buyers shop in winter."}]}], "nice_to_have": []},
        {"fixes": [{"find": doubt, "replace": "Some buyers keep looking through the winter.", "problem": "unsupported"}]},
        {"items": [{"id": "facts", "status": "pass"}], "nice_to_have": []},
    ], second_opinion=[doubt])
    assert "THE FACT-CHECKER FOUND NO SUPPORT FOR THESE SENTENCES" in calls[0][1] and doubt in calls[0][1]
    assert applied == 1 and "Some buyers keep looking through the winter." in pipe._article_text(a)
    assert next(p for p in pass_list if p["id"] == "facts")["status"] == "fixed" and all(i["fixed"] for i in issues)


def test_when_the_grader_finds_the_claim_supported_nothing_is_touched_or_flagged(monkeypatch):
    a = _article("There were 41 homes for sale.")
    before = copy.deepcopy(a)
    (pass_list, _nice, issues, applied), calls = _run_grader(monkeypatch, a, [{"items": [{"id": i, "status": "pass"} for i in bfc.PASS_IDS], "nice_to_have": []}],
                                                           second_opinion=["There were 41 homes for sale."])
    assert a == before and applied == 0 and issues == [] and len(calls) == 1
    assert all(p["status"] == "pass" for p in pass_list)


def test_a_point_from_the_brief_left_open_is_graded_and_reported(monkeypatch):
    a = _article("There were 41 homes for sale.")
    open_point = [{"point": "the commute to Center City", "status": "not_answered", "note": "the ledger has nothing on it"}]
    still = {"items": [{"id": "asked", "status": "fail", "problems": [{"quote": "", "why": "the commute is never mentioned", "fix": "Say plainly we don't have drive times."}]}], "nice_to_have": []}
    (pass_list, _nice, issues, _applied), calls = _run_grader(monkeypatch, a, [copy.deepcopy(still), {"fixes": []}, copy.deepcopy(still)], unanswered=open_point)
    assert "NOT ANSWERED" in calls[0][1] and "the commute to Center City (not_answered; the ledger has nothing on it)" in calls[0][1]
    assert "NOT ANSWERED" in calls[2][1]                                      # the re-grade is asked again
    assert next(p for p in pass_list if p["id"] == "asked")["status"] == "fail"
    assert [(i["kind"], i["fixed"]) for i in issues] == [("pass_asked", False)]


def test_code_confirmed_problems_are_left_for_the_final_report_not_listed_twice(monkeypatch):
    a = _article("Ask your agent about the next step. There were 41 homes for sale.")
    answers = [{"items": [], "nice_to_have": []}, {"fixes": []}, {"items": [], "nice_to_have": []}]
    (pass_list, _nice, issues, _applied), _calls = _run_grader(monkeypatch, a, answers)
    assert next(p for p in pass_list if p["id"] == "voice")["status"] == "fail" and issues == []


def test_the_fact_check_is_asked_for_every_point_of_the_brief():
    schema = pipe._REVIEW_TOOL["input_schema"]
    assert "brief_points" in schema["required"] and schema["properties"]["brief_points"]["items"]["properties"]["status"]["enum"] == [
        "answered", "partly", "said_no_data", "not_answered"]
    assert "EACH key point" in pipe._REVIEW_SYSTEM and "the risks should be understood first is allowed and stays" in pipe._REVIEW_SYSTEM
    assert "how buyers or sellers behave" in pipe._writer_system("PA") and "how buyers or sellers behave" in pipe._REVIEW_SYSTEM


# ------------------------------------------------------------------ 7. the Verified badge after a hand edit
def test_the_checked_text_fingerprint_changes_with_the_words_not_the_layout():
    blocks = [{"id": "a", "type": "paragraph", "data": {"html": "Homes sold in a <strong>median 31 days</strong>."}},
              {"id": "b", "type": "bulleted_list", "data": {"items": ["One", "Two"]}},
              {"id": "c", "type": "payment_example", "data": {"price": 345000}}]
    h = blog_routes._checked_text_hash(blocks)
    moved = [blocks[2], {**blocks[0], "data": {"html": "Homes sold in a <b>median 31 days</b>.&nbsp;<br>"}}, blocks[1]]
    assert blog_routes._checked_text_hash(moved) == h
    edited = [{**blocks[0], "data": {"html": "Homes sold in a median 12 days."}}] + blocks[1:]
    assert blog_routes._checked_text_hash(edited) != h


# ------------------------------------------------------------------ the number audit read "11.7 miles" as 11.7 million
def test_miles_are_not_millions():
    assert writer.number_audit("Wilmington is 14.2 miles away and the home is $1.2 mil.", "14.2 miles") == ["$1.2 mil"]


# ------------------------------------------------------------------ smaller fixes found on the way
def test_a_sentence_repeated_word_for_word_is_removed_where_it_comes_back():
    a = _article("Homes that sold this fall took a median of 31 days to find a buyer. That is slower than last year.",
                 "Prices held steady. Homes that sold this fall took a median of 31 days to find a buyer.")
    a["at_a_glance"] = ["Homes that sold this fall took a median of 31 days to find a buyer."]
    a["closing"] = {"paragraphs": ["Reach out and I will walk you through it.", "Homes that sold this fall took a median of 31 days to find a buyer."]}
    removed = pipe.drop_repeated_sentences(a)
    assert removed == ["Homes that sold this fall took a median of 31 days to find a buyer."] * 2
    blocks = a["sections"][0]["blocks"]
    assert blocks[0]["text"].startswith("Homes that sold this fall") and blocks[1]["text"] == "Prices held steady."
    assert a["closing"]["paragraphs"] == ["Reach out and I will walk you through it."] and len(a["at_a_glance"]) == 1   # the box may repeat a key number
    assert pipe.drop_repeated_sentences(_article("Short one. Short one.")) == []


def test_a_transfer_tax_rate_we_do_not_hold_is_called_that_not_a_property_tax_percent():
    brief = {"topic": "Buying in Fishtown", "audiences": ["Buyers"]}
    hit = bfc.final_lint("The transfer tax is 9.9% of the price.", brief, tax_rates=[4.578, 3.578, 1.0])
    assert len(hit) == 1 and "transfer-tax rate we don't hold" in hit[0]["fix"] and "4.578%" in hit[0]["fix"]
    still = bfc.final_lint("Property taxes run about 1.4% of the price each year.", brief, tax_rates=[4.578])
    assert len(still) == 1 and "property tax as a percent" in still[0]["fix"]


def test_no_fee_reported_is_not_worded_as_no_fee():
    class Area:
        name, state, slug = "Kennett Square", "PA", "kennett-square-pa"
    cost = {"as_of": "2026-11-14", "townhome_fees": {"of": 40, "condo_fee": 3, "hoa_only": 30, "no_fee": 7}}
    claim = next(i["claim"] for i in tk.cost_items(Area(), cost) if "building style" in i["claim"])
    assert "7 show no fee on the listing - which means none was REPORTED, not that the home has none" in claim


# ------------------------------------------------------------------ found grading the 11/14 round-C articles (Fishtown again, Media seller)
def test_an_ask_line_takes_the_words_after_a_label_colon():
    blocks = [{"type": "subheading", "data": {"html": "Negotiated: commission and seller assist"}},
              {"type": "paragraph", "data": {"html": "Commissions are negotiable. Reach out to me and we can go over yours."}},
              {"type": "ask_agent", "data": {}},
              {"type": "heading", "data": {"html": "Bottom line: what to do first"}},
              {"type": "ask_agent", "data": {}},
              {"type": "heading", "data": {"html": "What to inspect on an older rowhome"}},
              {"type": "ask_agent", "data": {}}]
    topics = [b["data"].get("topic") for b in writer._name_ask_topics(blocks) if b["type"] == "ask_agent"]
    assert topics == ["commission and seller assist", None, "what to inspect on an older rowhome"]     # it read "Ask me about Negotiated"


def test_in_my_experience_in_front_of_a_paraphrase_of_the_agents_take_is_theirs():
    brief = {"topic": "Selling in Media", "audiences": ["Sellers"],
             "personal_take": "Most of my downsizing sellers in Media wait a year too long because the house feels like too big a project to get ready - "
                              "it almost never needs as much as they think."}
    ok = "In my experience, getting the house ready usually takes less work than sellers expect."
    assert bfc.final_lint(ok, brief) == []
    invented = "In my experience, winter buyers are more serious than spring buyers."
    assert [h["kind"] for h in bfc.final_lint(invented, brief)] == ["invented_experience"]
    story = "I've helped dozens of Media sellers get a house ready in a weekend."        # a story still has to be the agent's own words
    assert [h["kind"] for h in bfc.final_lint(story, brief)] == ["invented_experience"]


def test_how_buyers_paid_adds_up_to_100():
    class Area:
        name, state, slug = "Fishtown", "PA", "fishtown-pa"
    mix = [{"type": "Conventional", "count": 46, "pct": 65}, {"type": "Cash", "count": 16, "pct": 23}, {"type": "VA", "count": 5, "pct": 7},
           {"type": "PHFA", "count": 2, "pct": 3}, {"type": "FHA", "count": 1, "pct": 1}, {"type": "Other", "count": 1, "pct": 1}]
    cost = {"as_of": "2026-11-14", "financing": {"closed_sales": 71, "recorded": 71, "days": 90, "through": "2026-11-13", "since": "2026-08-15", "mix": mix}}
    claim = next(i["claim"] for i in tk.cost_items(Area(), cost) if "how buyers paid" in i["claim"])
    shares = [int(x) for x in __import__("re").findall(r"(\d+)%", claim)]
    assert sum(shares) == 100 and "other" in claim, claim


def test_the_grader_gets_a_second_try_at_a_problem_whose_fix_was_thrown_out(monkeypatch):
    flat = "Selling here usually means a quick sale at close to your asking price."
    a = _article(flat + " There were 41 homes for sale.")
    (pass_list, _nice, issues, applied), calls = _run_grader(monkeypatch, a, [
        {"items": [{"id": "facts", "status": "fail", "problems": [{"quote": flat, "why": "stated for every home", "fix": "Say many homes sold close to asking."}]}], "nice_to_have": []},
        {"fixes": [{"find": flat, "replace": "Many homes here sold close to their asking price.", "problem": "x"}]},
        {"items": [{"id": "facts", "status": "pass"}], "nice_to_have": []},
    ], unapplied=[{"quote": flat, "problem": "A median across mixed home types stated as what usually happens."}])
    assert "ITS OWN FIX COULD NOT BE USED" in calls[0][1] and flat in calls[0][1]
    assert applied == 1 and "Many homes here sold close to their asking price." in pipe._article_text(a)


def test_research_is_told_its_budget_its_order_and_what_not_to_look_up():
    import inspect
    src = inspect.getsource(pipe._run)
    assert "ONE look-up at a time" in src and "Do NOT spend a look-up on: home prices" in src
    assert "MUNICIPALITIES TO CHECK for resale requirements, in THIS order" in src
    assert 'usage.setdefault("lookups", [])' in inspect.getsource(pipe._run_tool_call)


def test_a_mailing_towns_named_places_come_before_the_grouped_rest():
    class Area:
        name, municipality_type, area_type = "Media", None, "city"
    geo = {"townships": [{"name": "Middletown Township", "share": 39}, {"name": "other municipalities (Edgmont Township, Rose Valley Borough)", "share": 3},
                         {"name": "Chester Heights Borough", "share": 1}]}
    text = tk.market_definition(Area(), geo)
    assert "Middletown Township, Chester Heights Borough, other municipalities (Edgmont Township, Rose Valley Borough);" in text



# ------------------------------------------------------------------ outside grader, second round (Fishtown 89, Media 81)
# Root causes, not article patches:
#   a hand-verified legal fact used a looser word than the governing text ("permitted" for "applied for")  -> see test_blog_grader_1114
#   an estimate's floor sat on one cost alone (closing costs = the transfer-tax half)                       -> see test_blog_grader_1114
#   a partial list was shown as if complete (school districts on the town card)
#   "who is it for" was stripped from a program along with its numbers (Property Tax/Rent Rebate)
#   a municipal requirement was stated as applying to every home
def test_a_protection_says_who_it_is_for_in_general_words_and_the_article_must_keep_that():
    brief = {"topic": "Selling the family home in Media and downsizing", "audiences": ["Sellers", "Homeowners"]}
    ptrr = next(x for x in bfc.protections_for({"PA"}, brief) if x["key"] == "pa_ptrr")
    assert "older adults, widows and widowers, and people with disabilities" in ptrr["says"] and "household income is under the program's limit" in ptrr["says"]
    assert not any(ch.isdigit() for ch in ptrr["says"]) and "$" not in ptrr["says"]            # who it is for - never the ages or the dollar limit
    thin = ("Once you're settled, look into Pennsylvania's Property Tax/Rent Rebate Program, which can return part of the property taxes or rent "
            "paid by eligible Pennsylvanians. Conditions apply, so check whether you qualify.")
    assert [x["key"] for x in bfc.missing_protections(thin, [ptrr])] == ["pa_ptrr"]            # the 11/14 Media wording is sent back
    full = ("Pennsylvania's Property Tax/Rent Rebate Program can return part of the property taxes paid by older adults, widows and widowers, "
            "and people with disabilities whose household income is under the program's limit.")
    assert bfc.missing_protections(full, [ptrr]) == []
    assert bfc.final_lint(full, brief) == []                                                   # and it is not fine print


def test_a_seller_is_told_a_municipal_rule_has_exceptions_and_whose_rule_it_is():
    seller = {"topic": "Selling your home in Media", "audiences": ["Sellers"]}
    told = pipe._coverage(seller, 2)
    assert "its code has exceptions" in told and "never as applying to every home" in told
    assert "name the municipality each requirement belongs to" in told and "never \"I don't have a figure\"" in told


def test_the_town_card_marks_a_school_list_that_is_not_the_whole_list():
    import inspect
    assert '"districts_complete": len(districts) == 1' in inspect.getsource(tk.geography_profile)
    assert 'c["school_districts_partial"]' in inspect.getsource(pipe._run)
    card = (root_dir.parent / "frontend" / "components" / "studio" / "blogs" / "AreaSpotlightSection.tsx").read_text(encoding="utf-8")
    assert "Main school districts" in card and "other districts serve some" in card and "Transit nearby" in card


def test_a_towns_own_application_lead_time_may_be_stated_and_nothing_else():
    # owner 11/14 ("do all fixes you think necessary"), grader finding 8: a timing article left out "apply at least three weeks before"
    seller = {"topic": "Selling your home in Media", "audiences": ["Sellers"]}
    assert "how far ahead of settlement the municipality says to apply" in pipe._coverage(seller, 1)
    told = pipe._coverage(seller, 2)
    # 11/15: every lead time the municipality's own item gives, not just one (grader round 7: Media Borough has two)
    assert "The ONLY timings you may state are the municipality's own lead times" in told and "never a lead time the ledger doesn't give" in told
    assert "lead time for applying for a resale or occupancy certificate" in pipe._GRADE_SYSTEM
    assert "lead time for applying for a resale or occupancy certificate" in pipe._REVIEW_SYSTEM and "lead time for applying" in pipe._writer_system("PA")
    ok = "Upper Providence currently asks for the application at least three weeks before settlement."
    assert bfc.final_lint(ok, seller) == []
    assert [h["kind"] for h in bfc.final_lint("You can cancel the contract within 5 days after you receive the resale certificate.", seller)] == ["rule_detail"]


# ------------------------------------------------------------------ what the look-up log showed (11/14, Ardmore on the owner's account)
def test_research_uses_the_direct_web_tools_and_falls_back_only_when_they_are_refused(monkeypatch):
    import anthropic
    import httpx
    tools = pipe.research_tools(8, 3, "PA")
    assert [t.get("type") for t in tools[:2]] == ["web_search_20250305", "web_fetch_20250910"] and tools[0]["max_uses"] == 8
    assert tools[0]["user_location"]["region"] == "Pennsylvania" and tools[-1]["name"] == "submit_evidence"
    assert [t.get("type") for t in pipe.research_tools(8, 0, None)] == ["web_search_20250305", None]         # no page reads, no location
    seen = []

    async def refuse_direct(*, tools, usage, **_):
        seen.append(tools[0]["type"])
        if tools[0]["type"] == "web_search_20250305":
            raise anthropic.BadRequestError("tool type not supported", response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None)
        return {"items": [], "not_found": []}
    monkeypatch.setattr(pipe, "_run_tool_call", refuse_direct)
    usage = pipe._new_usage()
    out = asyncio.run(pipe._research_call(model="m", user_msg="x", searches=8, fetches=3, state="PA", max_tokens=100, effort=None, usage=usage))
    assert out == {"items": [], "not_found": []} and seen == ["web_search_20250305", "web_search_20260209"]
    assert usage["lookups"] == ["(direct web tools refused - newer versions used)"]


def test_a_list_wrapped_in_a_one_item_list_is_unwrapped():
    wrapped = ['["SEPTA fare not read", "Condo fees not researched"]']
    assert pipe.research_list(wrapped) == ["SEPTA fare not read", "Condo fees not researched"]
    assert pipe.research_list(["[not json at all"]) == ["[not json at all"] and pipe.research_list(["one plain note"]) == ["one plain note"]


# ------------------------------------------------------------------ found on the owner's own account (11/14: Ardmore buyer, Bryn Mawr seller)
def test_a_sale_the_agent_picked_has_to_appear_in_the_article():
    sales = [{"id": "804186847890", "address": "401 Thornbrook Ave", "city": "Bryn Mawr", "price": 1535000, "sold_date": "2025-10-17"},
             {"id": "111", "address": "9 Other Rd", "city": "Wayne", "price": 800000, "sold_date": "2026-01-02"}]
    brief = {"topic": "Selling your Bryn Mawr home", "audiences": ["Sellers"], "include_recent_sales": True, "sale_ids": ["804186847890"]}
    chosen = pipe.picked_sales(brief, sales)
    assert [(c["address"], c["needle"], c["sold"]) for c in chosen] == [("401 Thornbrook Ave", "401 Thornbrook", "2025-10")]
    assert pipe.picked_sales({"include_recent_sales": True}, sales) == []           # the automatic set is offered, not demanded
    without = pipe.code_problems("Homes sold fast this fall.", brief=brief, protections=[], must_mention=chosen)
    hit = [p for p in without if p["kind"] == "missing_sale"]
    assert len(hit) == 1 and hit[0]["pass"] == "asked" and hit[0]["missing"] and "401 Thornbrook Ave" in hit[0]["problem"]
    assert hit[0]["note"] == "The sale you picked (401 Thornbrook Ave) isn't mentioned in the article."
    with_it = pipe.code_problems("A recent sale of mine at 401 Thornbrook Avenue closed in October 2025.", brief=brief, protections=[], must_mention=chosen)
    assert [p for p in with_it if p["kind"] == "missing_sale"] == []


def test_readers_are_not_sent_to_another_agents_website():
    assert pipe.official_source("https://www.rtsd.org/our-district") and pipe.official_source("https://www.phila.gov/x")
    assert pipe.official_source("https://www.haverford.k12.pa.us/") and not pipe.official_source("https://saiidzamani.com/towns/bryn-mawr/schools")
    assert not pipe.official_source(None) and not pipe.official_source("not a url")
    stored = [{"origin": "idx_research", "topic": "schools", "claim": "a", "source_url": "https://saiidzamani.com/towns/bryn-mawr/schools"},
              {"origin": "idx_research", "topic": "schools", "claim": "b", "source_url": "https://www.lmsd.org/schools"},
              {"origin": "idx_research", "topic": "dining", "claim": "c", "source_url": "https://www.suburbansquare.com/"},
              {"origin": "evidence_bank", "topic": "regulation", "claim": "d", "source_url": "https://www.radnor.com/real-estate-transfers"}]
    assert [i["claim"] for i in pipe.trusted_stored_research(stored)] == ["b", "c", "d"]      # a township's own .com page, read this year, stays


def test_home_type_counts_say_what_is_left_over():
    class Area:
        name, state, slug = "Ardmore", "PA", "ardmore-pa"
    cost = {"as_of": "2026-11-14", "mix": {"homes": 19, "attached": {"count": 14, "median": 520_000}, "house": {"count": 4, "median": 900_000},
                                           "low_quarter": {"count": 5, "attached": 4}, "one_level": {"count": 0}}}
    claim = next(i["claim"] for i in tk.cost_items(Area(), cost) if "homes for sale on" in i["claim"] and "single-family" in i["claim"])
    assert "of its 19 homes" in claim and "the other 1 is another type (multi-family, for example)" in claim
    cost["mix"]["homes"] = 18
    assert "another type" not in next(i["claim"] for i in tk.cost_items(Area(), cost) if "single-family" in i["claim"])
