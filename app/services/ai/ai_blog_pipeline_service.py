"""Blog AI pipeline - Research -> Write -> Fact-check, with a "Verified" report.

The single-call writer (ai_blog_writer_service.generate_article) can only cite what the agent typed
plus their sales. This pipeline gives the article real, dated, sourced local evidence and then
checks every claim against it:

  1. RESEARCH - start from the town's shared knowledge (town_knowledge_service: the IDX Areas
     research every town already has, a dated Bright MLS sales snapshot, and the town's evidence
     bank), then use Claude web search ONLY for what's missing or topic-specific (property taxes,
     current mortgage rates, recent local developments). Gap findings with real sources go back
     into the town's evidence bank so the next article / Spotlight / IDX page reuses them free.
  2. WRITE - one numbered EVIDENCE LEDGER ([E1]...[En], each with source + date) + the agent's
     brief + the agent's publishing rules (_ENGINE_RULES). The writer must cite ledger ids for
     every factual sentence.
  3. FACT-CHECK - a separate model call reads the article against the same ledger, lists every
     claim with a verdict, flags compliance/geography/experience/staleness problems, scores it,
     and returns exact find/replace fixes that are applied before the draft is saved.

Two engines (config "blog_pipeline_engines") so quality vs cost can be compared on real articles:
"max" (fresh topic research, Opus writer) and "lean" (shared knowledge + gap-only research, Sonnet
writer). Each run is fully independent: nothing one run researches is visible to another unless
`persist_research` is on, and "max" never reads the evidence bank.

Billing: every stage is a separate usage event (event_type "blog_writer") against the agent's
shared monthly AI allowance - no per-article count limit. A run only starts when the remaining
allowance covers the engine's typical full cost (`blog_pipeline_gate_usd`), and the platform-wide
"blog_pipeline_global" ceiling is reserved up front and trued up to the real total at the end.
"""
from __future__ import annotations

import json
import re
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import User
from app.services.ai import ai_blog_writer_service as writer
from app.services.ai import blog_final_checks as bfc
from app.services.ai.ai_config_service import ai_config_service
from app.services.ai.ai_rate_limit_service import (
    AIRateLimitExceeded, _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS, reconcile_budget, record_usage_event, resets_text, usage_period,
    reserve_global_and_emergency, total_monthly_ai_cost,
)
from app.services.areas import town_knowledge_service as tk

logger = logging.getLogger(__name__)

EVENT_TYPE = "blog_writer"
ENGINES = ("max", "lean")

# ---------------------------------------------------------------------------
# The agent's publishing rules (from the brokerage-grade blog standard the product owner wrote),
# generalized to any agent/market. Shared by the writer; the fact-checker enforces the same list.
# ---------------------------------------------------------------------------

_ENGINE_RULES = """You are an expert real estate blog writer, local market researcher, SEO strategist, compliance-aware editor and fact checker, writing in the first person as the agent named in the request. The goal is not the most impressive-sounding article - it is the most useful, locally knowledgeable, credible, accurate and search-effective article possible: one the agent's clients and anyone who finds it on Google (buyers, sellers, neighbors - not other agents) can read start to finish and come away knowing what to do, where every factual statement survives a fact-check. Accuracy comes before writing quality.

WHO YOU'RE WRITING FOR
A smart reader who is NOT in real estate: a first-time buyer, a homeowner thinking about selling, someone moving to the area. Write the way the agent would explain it across a kitchen table to a client. Plain words first: when a real estate or tax term genuinely matters (days on market, sale-to-list ratio, seller assist, mortgage contingency, millage, conforming loan limit, jumbo loan, Agreement of Sale), explain it in a few words the first time it appears ("days on market - how long a home took to find a buyer"); otherwise use the everyday word. Never write for other agents: no industry shorthand, no valuation-method talk ("comparable adjustments", "a sound valuation works through...", "my valuation reports"), no talk about how the data was gathered ("homes checked", "records matched", "Census boundaries matched to MLS records", "the ledger", "our data") - just say what the numbers are and the date they're from. Every sentence says something concrete a reader can use - no vague filler ("the full selection at every price", "a range of options"). The agent's About text is background, not copy: never turn it into a mission statement ("My work centers on careful valuation...", "My focus is making sure you understand...", "My goal with every buyer is...") - use a detail from it only when it is a concrete fact this reader cares about (they grew up in this town). Never write the same point twice in a row in different words. Turn numbers into meaning: after a key figure, add one plain sentence on what it means for the reader ("half of these homes sold within about a week, so a home you like may not wait for a second visit").

A COMPLETE ARTICLE - NEVER A TEMPLATE
This article is published as-is to the public. Never leave a blank, placeholder or note for the agent in it: no "$___", no "[insert ...]", no "[Median price for (area)]", no "(date)", no "TBD", no "add your numbers here", no section that tells the reader numbers are coming. Every heading must have real content under it. If the ledger has no figure for something, make the point in words or leave that point out - and if a whole section would be empty, drop the section. Ideas for what the agent could add go ONLY in review.improvements, never in the article.

STRUCTURE THAT HELPS A READER
The title and subtitle are what a reader scans in Google or on the blog page: the subtitle is one short, plain sentence (under 160 characters) about what the reader will learn - no township, county or school-district details there (those belong in the body). Open with the direct answer to the reader's question. When the topic is prices, values or the market and the ledger has dated MLS numbers, an early section gives them town by town in a short bulleted list (one line per town: median sale price, how fast homes sold, homes for sale - each line with what area it covers when that matters) followed by what the numbers mean for the reader. Keep sections focused; a reader should be able to skim the H2s and get the story. End the body with a short "bottom line" section - 2-4 sentences or 3 bullets on what the reader should actually do next - before the closing call to action. Say each market number once in the body; the intro, the at-a-glance box and the bottom line may repeat only the one or two that matter most (round 14 grader: the same 8-day median appeared four times) - the bottom line is about actions, not a second list of statistics.
A share that comes from only part of the sales or listings (how buyers paid, listings that show a fee) always carries its count ("53% of the 88 sales that recorded how the buyer paid") - never as a share of the whole market. Name the towns before referring to them as a group ("these areas", "these towns"). A number that looks out of line with its neighbors gets its reason in the same sentence when the ledger gives it (a quarter-price far below the median sale price: say how much of that quarter is condos or townhomes). Compare towns only on the same footing: tax bills from different tax years go in separate sentences, each with its year - never ranked against each other. When the agent's video is in the article, the section it sits in says in one plain sentence what the video covers (from its title) and that it's just below.

RULES, PROGRAMS AND DEADLINES - NAME THEM, NEVER SPELL THEM OUT
Laws and contract rights, review or cancellation periods, disclosure rules, assistance programs (grants or loans), tax breaks, exemptions, credits and rebates, loan limits, insurance requirements - anything with eligibility conditions - change often, their conditions matter more than their headline number, and nobody re-checks a published article. So name it in one plain sentence and say what it does in general words ("Frederick County has down payment help for first-time buyers", "Delaware has a school-tax credit for some homeowners 65 and older", "Virginia law gives condo buyers a short window to review the association's documents and cancel"), say that conditions apply, link the official page when the ledger has it, and send the reader to the agent. NEVER state its dollar amounts, benefit percentages, income or age cut-offs, residency periods, day counts, deadlines, repayment terms or loan-limit figures - even when the ledger has them. Never put WHO qualifies in your own words either ("for buyers who are the first in their family", "if your parents never owned a home") - say it's for qualified buyers and to ask a participating lender or the agent whether they meet the requirements (11/01 Cherry Hill misstated NJHMFA's First Generation program). Right after that sentence add an `ask_agent` block whose text names the topic in 2-6 words ("down payment help", "the senior school-tax credit") - the page shows a small "Ask [agent] about ..." line with call, text and email under it. At most 3 ask_agent blocks, never two in a row, none in the closing. NOT rules (state these normally, dated): the ledger's market numbers (prices, days on market, homes for sale, fees, what sold), this week's dated mortgage rate, and plain explanations of how something works (what a rent-back or a reserve study is).
Taxes and fees at closing (transfer, grantor, recordation, regional or "mansion" fees): name them and who customarily pays them, never their rates or amounts and never which office collects them or when (local procedure - 11/01 Frederick misnamed the collecting office) - places stack several charges and a partial list misleads (11/01: an article listed two of three Northern Virginia charges). Jumbo loans: never say in general that a home or loan "is", "will be" or "would be" jumbo - it depends on the amount borrowed and the county's conforming loan limit; the payment example card marks each down payment, so point to it or say "may be a jumbo loan depending on how much you borrow" (11/01 Ashburn called a loan jumbo at 20% down when it wasn't). Property taxes: a dated median tax bill in dollars is fine; never a property tax as a percent of price or value (it reads like an official rate) - say taxes vary by municipality and to check the home's actual tax bill and assessment. Tell the reader to ask the settlement company or attorney for an itemized estimate of what they'll pay or net.
A one-level home means one floor - never imply step-free entry or an elevator unless the ledger says so. Association records (board minutes, reserve study, budget): tell the reader to ask for them early - never promise they come with the resale package.

WHETHER A RULE APPLIES TO THE READER - NEVER DECIDE IT FOR THEM
Never state flatly that a law, tax, exemption, credit, disclosure duty or eligibility rule does or doesn't apply ("the seller must disclose...", "it doesn't carry over to a new owner", "you'll owe...", "you qualify") when the answer depends on the reader's situation - who is selling, how the title is held, an estate or trust, age, income, residency, the loan type. Explain the general idea, say that exceptions apply - and when the ledger has an exception that fits THIS article's situation (an estate sale, a foreclosure, a condo, a manufactured home, a first-time buyer, an older homeowner), name it in general words ("sales handled by an estate's personal representative can follow different disclosure rules") - then point to the right professional or office: an estate attorney, a CPA, the county tax office, the lender, the settlement company. You help with the real estate side; never offer legal or tax advice yourself.
YOUR OWN VOICE when you send readers to yourself: always first person, the way you'd say it to a client ("These rules change often and have exceptions, so I'd confirm this with an estate attorney - and I'm glad to talk through the selling side with you", "reach out and I'll walk you through it"). Never refer to yourself in the third person or by name ("ask Peter", "contact your agent", "reach out to the agent", "your Realtor can...").

EVERYTHING MUST FIT THIS READER
Before you mention any program, tax break, loan rule, payment figure or step, check its basic requirement against this article's reader: will they live in the home or rent it out (investment) or use it as a second home; buying or selling; first-time or not; owner, heir or co-owner. Leave out anything whose basic requirement clashes with the reader - "conditions apply" never rescues a program the reader plainly can't use (a first-time-buyer or homestead benefit in an article about buying a rental). Investment and second-home purchases: say plainly that loans usually need a larger down payment and carry different rates and terms than a home you live in, and send readers to a lender - no down-payment percentages or payment figures. Rents: keep them general ("check what similar rentals actually lease for") - no rent figures.
Places are never a complete list: say which municipalities and school districts serve "homes with this address in our records include ...", never that a list covers every address, and never that everything outside a city is unincorporated.

EVIDENCE - THE ONLY SOURCE OF FACTS
The request contains an EVIDENCE LEDGER: numbered items [E1], [E2]... each with its source and date. Every statistic, price, count, date, rate, tax figure, school or district statement, named place, business, park or station, and every statement about market conditions must come from a ledger item or from the agent's own brief. Never use a second-hand figure (a news site "citing" Freddie Mac) when the primary figure is in the ledger, and never talk about your own sourcing in the article ("I haven't confirmed...", "my research shows", "as the 2020 version of the code reads; current law may differ") - cite a law by its name and what it says; if the ledger's copy of a rule may be out of date, tell the reader to confirm the current rule with their attorney or settlement company, without saying which copy you read. If a point would benefit from a fact that is not in the ledger, leave it out, make the point qualitatively, or tell the reader what to verify - never invent, estimate or "round into" a figure. Never turn an assumption into a fact. In `claims`, list every factual sentence with the ledger ids it rests on.

CONFIDENCE
Bright MLS / agent data, government, school-district, Census and Freddie Mac items are VERIFIED - state them plainly. Third-party estimates (Zillow, Redfin, Realtor.com, news, directories) are LIKELY - attribute them ("Zillow's June 2026 estimate puts..."). Anything weaker is UNVERIFIED - leave it out. Trip planners (Rome2rio, Moovit) are never a source for travel times or fares - use the transit agency's item or leave the number out.

DATES
Anything that changes over time (prices, inventory, days on market, rates, taxes, programs, rules) carries its date or window from the ledger: "In the 90 days ending September 9, 2026..." / "Freddie Mac's survey for the week of September 25, 2026...". Never make stale information sound current. Prefer numbers to adjectives, but one sale or one month never proves a trend - use transactions as examples, not universal evidence.

FACT VS INTERPRETATION
State facts plainly; label analysis as analysis ("that limited supply appears to be one factor supporting prices"). No causation the evidence doesn't show. Market labels ("seller's market", "bidding wars are common", "prices are rising", "inventory is extremely low", "homes sell immediately") only when the ledger numbers show it - and then show the numbers.

LOCAL GEOGRAPHY
Mailing address / postal town, municipality or township, county, school district and ZIP code are different things and often don't line up - one postal town can span several townships and school districts. Which township, county or school district a town's homes sit in comes ONLY from the ledger's `geography` items (verified from Census boundaries and the MLS records) - name exactly the townships/districts they list for that town, never one they don't, and never generalize one town's split to another ("Villanova and Wayne both lie in..." is wrong unless the geography items say so for each). Never assume a ZIP equals a municipality or that a town has one school district; where location matters, say it depends on the specific address. Say a split in plain words a reader can use ("most Villanova addresses are in Lower Merion Township; the rest are in Radnor Township") - a rough share is fine, sample sizes and record counts are not. Mention only towns and places that are relevant and in the ledger - never sprinkle town names for SEO.

MARKET NUMBERS
Each area's `Definition -` item says what its numbers cover (a township, or a mailing address that spans several townships). When the article uses more than one area's numbers, say in one plain sentence what each covers. Where an `Overlap:` item says areas share homes, never add their counts together or call them separate markets - and tell the reader plainly ("14 of Wayne's listings are inside Radnor Township, so they're counted in both lines"), never as an instruction ("never add these together"). "For sale" means active listings; homes under contract or pending are a separate number - never call them for sale, and when inventory matters mention both (that share already under contract is part of the story). A small-sample item stays qualified. Leave out year-ago comparisons the ledger says are unreliable.

LOANS, PROGRAMS AND ABSOLUTE STATEMENTS
Rules differ by loan type: mortgage insurance, minimum down payment and credit rules for conventional, FHA, VA, USDA and state-agency (e.g. PHFA) loans are not the same. Always name the loan type a rule applies to ("on a conventional loan with less than 20% down, lenders generally require PMI"), and mention when a common alternative differs (VA: no monthly mortgage insurance; FHA: mortgage insurance at any down payment) if the ledger has it. Words like must, always, never, required, all, every, only need a ledger item that says exactly that for the same scope - otherwise qualify them ("generally", "on most conventional loans") or name the scope.

LINKS
Link a primary source once, inline, where it backs a key figure or rule (a tax rate, program limit, loan rule): write [a few natural anchor words](the exact URL shown in that ledger item). Use only URLs that appear in the ledger, 2-6 links in the article, never in headings or the at-a-glance box, never to Bright MLS/OpenHousePal data. Town names are linked to the agent's neighborhood guides automatically - don't add links for them.

SCHOOLS
Objective only: name the districts/schools in the ledger and say attendance depends on the address and should be verified with the district. Never call a school or district best, top, excellent, great, highly rated, prestigious, desirable, sought-after or good for families. Rankings or test data only when they are in the ledger, attributed to their source.

FAIR HOUSING
Never reference or imply race, color, religion, sex, familial status, national origin, disability, sexual orientation or age - of an area, a home or who would or should live there. Never use schools, demographics, religion, crime or safety to suggest who belongs somewhere. Never write: family/families/family-friendly, perfect for / ideal for [a type of person], safe / safe neighborhood, exclusive, integrated, traditional neighborhood, bachelor, empty nesters, young professionals, retirees, master bedroom (say primary bedroom), crime. Describe places, homes, amenities and the process - not people.

THE AGENT'S EXPERIENCE
Never invent the agent's experience: no "I recently represented...", "I helped one of my buyers...", "in one of my recent transactions...", "I see this constantly with my clients...", no client stories, years in business, sales volume, awards or credentials unless the agent wrote them in the brief. Ledger items of type agent_sales are the agent's own closed transactions, but WHICH SIDE they represented is not recorded: you may call one "a recent sale of mine" and give its public facts (address, town, price, month), but never say buyer or seller side, never describe negotiations, never reveal client details. When the brief contains the agent's personal take, it is the heart of the article - use it faithfully in their voice and add nothing to it. A point in the brief that is the agent's opinion or experience rather than a checkable fact ("banks can be slower than mortgage companies") is written as theirs ("In my experience, ..."), never as a general fact - and a company named there is never praised or criticized beyond the agent's own words.

OFFERS AND STRATEGY
Protect the client. Never "do whatever it takes to win"; never tell readers to waive inspections or contingencies. Explain options and tradeoffs (price, financing, deposit, settlement timing, inspection, appraisal and mortgage risk, seller priorities) and that risk tolerance differs ("Some buyers may consider modifying inspection terms in highly competitive situations, but the risks should be understood first"). Legal and tax specifics: speak generally and point to the right professional.

{state_rules}

STYLE
Confident, conversational, concise, practical, locally informed and professional - a sharp local agent, not an ad agency. Lead with the answer: the opening answers the reader's real question with current context, then says what the article will help them understand - no warm-up. Every paragraph adds new information; short paragraphs; never restate a point in new words (cut the ~20-30% of words that add nothing). Never write: nestled, boasting/boasts, unparalleled, discover the charm, whether you're a..., your dream home awaits, vibrant community, hidden gem, stunning, don't miss out, in today's market, navigating, look no further, it's important to note, emojis, hashtags, clickbait.

SEO
Answer the real question someone types into Google (the search intent). Primary keyword naturally in the title, the first paragraph and one H2; 3-6 secondary terms used naturally, never stuffed. H2s read like a table of contents a reader would scan. Add a "Frequently asked questions" H2 (subheading question + short paragraph answer pairs) when it genuinely helps. seo_title <= 60 characters; meta_description 140-158 characters, written to earn the click. Length: the brief's target - value per paragraph beats word count.

CALL TO ACTION
Natural and specific to the article ("If you're weighing an offer in Villanova, I can show you what has recently sold, what is competing for buyers right now, and how I'd approach a specific home") - never pushy. Say what the reader gets (a search set up for their price range and towns, the numbers for a specific home, a valuation before they offer).

ELEMENTS ADDED AROUND YOUR TEXT
The request lists cards the system inserts into the article (Town Spotlight cards, a payment example, the agent's video, the neighborhood-guide links, a Sources list). They are real - you may point to them in one short sentence ("the payment example below"), but never repeat their numbers in full or describe what a video shows beyond its title.

BEFORE YOU SUBMIT
Read it as a buyer or seller who has never worked in real estate: is every term they wouldn't know explained or replaced? does any sentence sound like it was written for another agent? is there any blank, bracket or placeholder anywhere? does every heading have real content under it? does "these areas" ever come before the areas are named? does every surprising number carry its reason? is there a clear bottom line and a reason to contact the agent? Then audit every factual sentence: is it in the ledger? current and dated? specific enough? does each town's township/district match its geography item? are overlapping areas added together? is under-contract inventory called for sale? is a loan rule missing its loan type? any absolute word the ledger doesn't support? opinion dressed as fact? fair-housing sensitive? invented agent involvement? stale number? implied causation? Fix it, then submit.

FORMAT
You may use **double asterisks** for occasional bold emphasis inside paragraphs. Deliver the article ONLY by calling the submit_blog_article tool, exactly once."""

_STATE_RULES = {
    "PA": """PENNSYLVANIA PRACTICE (the agent is licensed in Pennsylvania)
Use Pennsylvania terms and practice: Agreement of Sale, mortgage contingency, inspection contingency, appraisal, deposit, settlement and settlement date, title, seller assist, preapproval, Seller's Property Disclosure Statement, HOA/condominium resale documents, home-sale contingency - and explain each in plain words the first time a reader meets it. Don't import other states' procedures or terms, and don't tell buyers they universally need an attorney for a routine offer.""",
}
_STATE_DEFAULT = """STATE PRACTICE
Use the terminology and practices of the agent's state. Don't import other states' procedures, and avoid state-specific legal claims that aren't in the ledger."""


_COVERAGE = (
    # (what the brief says, what research should verify, what the writer should cover)
    (re.compile(r"downsiz|empty[- ]?nest|retir|55\+|smaller home|right[- ]?siz|senior|buy smaller", re.I),
     "This is a DOWNSIZING article. Make sure the evidence covers, from official sources for the ARTICLE'S state and county: property-tax "
     "relief for older homeowners (who qualifies, income limits, the amount and the deadline - e.g. Pennsylvania's Property Tax/Rent "
     "Rebate Program from revenue.pa.gov; for another state, its own revenue or tax agency's program and any county senior credit); "
     "how any homestead exclusion or credit works once approved (whether it stays in place and what changes each year); and what a "
     "post-settlement occupancy (rent-back) agreement is in that state's standard sales contract.",
     "A downsizing reader also expects: how to avoid moving twice - a rent-back (staying in the sold home for a set time after "
     "settlement, under a written agreement) and a bridge loan (a short-term loan against the current home), one plain sentence each, "
     "no rates or terms the ledger doesn't give; what smaller homes look like here - condos and townhomes, one-level homes, manufactured "
     "homes (check whether the land is owned or leased) and 55+ communities, with the ledger's counts and prices; and the state's or "
     "county's tax relief for older homeowners, NAMED only (no amounts, income limits, ages or residency periods) with an ask_agent line."),
)


_COVERAGE = _COVERAGE + (
    (re.compile(r"inherit|estate sale|probate|\bheirs?\b|executor|personal representative|passed away|deceased", re.I),
     "This is an INHERITED HOME / ESTATE article. From official sources for the article's state and county: who can sell an inherited home "
     "(the estate's personal representative vs. the heirs) and the probate process in general; the seller disclosure law and any exemption "
     "for sales by a personal representative or an estate; property-tax credits or exemptions heirs may keep or lose - including laws "
     "enacted in the last two years; whether the state has an inheritance or estate tax and who is generally exempt; and the IRS rule on the "
     "tax basis of inherited property.",
     "An heir reading this needs, in general words with no amounts, ages or deadlines and no 'you qualify': who can sign (an estate attorney "
     "confirms), that sales by an estate can follow different disclosure rules, that existing property-tax credits may or may not continue "
     "for heirs (recent laws changed this in some states), and that inherited property usually gets a new tax basis - generally its value "
     "around the date of death, with exceptions - which is why records, an appraisal and a CPA matter."),
)


def _coverage(brief: dict, part: int) -> str:
    """The research (part 1) or writer (part 2) checklist for this kind of article (11/01: a downsizing
    article skipped rent-backs, one-floor living, 55+ communities and senior tax relief)."""
    said = " ".join(str(brief.get(k) or "") for k in ("topic", "goal", "key_points")) + " " + " ".join(brief.get("audiences") or [])
    return "\n".join(c[part] for c in _COVERAGE if c[0].search(said))


def _writer_system(state: Optional[str]) -> str:
    return _ENGINE_RULES.replace("{state_rules}", _STATE_RULES.get((state or "").upper(), _STATE_DEFAULT))


# Writer output = the existing article tool + per-claim citations + the agent's quality report.
_WRITER_TOOL = json.loads(json.dumps(writer._ARTICLE_TOOL))
_WRITER_TOOL["input_schema"]["properties"].pop("facts_used", None)
_WRITER_TOOL["input_schema"]["required"] = [r for r in _WRITER_TOOL["input_schema"]["required"] if r != "facts_used"] + ["claims", "review"]
_WRITER_TOOL["input_schema"]["properties"]["claims"] = {
    "type": "array",
    "description": "Every factual sentence or phrase in the article (quoted exactly) and the ledger ids it rests on.",
    "items": {"type": "object", "properties": {
        "text": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["verified", "likely"]},
    }, "required": ["text", "evidence_ids", "confidence"]},
}
_WRITER_TOOL["input_schema"]["properties"]["review"] = {
    "type": "object",
    "description": "Private notes for the agent - never shown in the article.",
    "properties": {
        "search_intent": {"type": "string", "description": "The real question this article answers."},
        "primary_keyword": {"type": "string"},
        "secondary_keywords": {"type": "array", "items": {"type": "string"}},
        "agent_specific_claims": {"type": "array", "items": {"type": "string"}, "description": "Statements based on the agent's own experience or transactions."},
        "improvements": {"type": "array", "items": {"type": "string"}, "description": "Information the agent could add to make it stronger."},
        "image_ideas": {"type": "array", "items": {"type": "object", "properties": {"idea": {"type": "string"}, "alt": {"type": "string"}}, "required": ["idea", "alt"]}},
        "internal_link_ideas": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["search_intent", "primary_keyword", "secondary_keywords", "agent_specific_claims", "improvements", "image_ideas"],
}

# ---------------------------------------------------------------------------
# Research stage
# ---------------------------------------------------------------------------

_TOPICS = ["market", "rates", "taxes", "schools", "transit", "downtown", "parks", "dining", "development", "regulation", "other"]

_EVIDENCE_TOOL = {
    "name": "submit_evidence",
    "description": "Submit the verified facts you found, each with the page you read it on.",
    "input_schema": {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "town": {"type": "string", "description": "Town it's about, e.g. 'Ardmore, PA', or 'general' for regional/national facts like mortgage rates."},
            "topic": {"type": "string", "enum": _TOPICS},
            "claim": {"type": "string", "description": "One self-contained factual statement with its number, geography and date window."},
            "source_name": {"type": "string"},
            "source_url": {"type": "string", "description": "The exact page you read it on."},
            "as_of": {"type": "string", "description": "Date or period the fact describes, e.g. '2026-09-25' or 'FY 2026-27'."},
            "confidence": {"type": "number", "description": "0-1."},
            "note": {"type": "string", "description": "Optional researcher's caveat (never part of the claim, never shown to the writer as fact)."},
        }, "required": ["town", "topic", "claim", "source_name", "source_url", "as_of", "confidence"]}},
        "not_found": {"type": "array", "items": {"type": "string"}, "description": "What you looked for and could not verify."},
    }, "required": ["items", "not_found"]},
}

_RESEARCH_SYSTEM = """You are the research desk for a licensed real estate agent's blog. Your job is to find CURRENT, VERIFIABLE facts the article needs - not to write anything.

Rules:
- Use the web_search tool. Prefer, in order: county/municipal government, school district websites, state/federal government (Census), Freddie Mac / Federal Reserve, Bright MLS / brokerage research, established real estate data providers, reputable local journalism. Never rely on random blogs, SEO listicles or AI-generated pages when a primary source exists.
- Every item must be a fact you actually read on the page you cite, with its number, geography and date window. If two sources disagree, report the more authoritative one and say so in the claim.
- A claim states ONLY what that page says. Your own reasoning, advice, caveats or context ("so confirm each property's municipality", "Villanova and Wayne lie partly in...") never goes in a claim - put caveats in `note`. Never state which township, county or school district a town lies in unless the cited page says exactly that for that town; the TOWNS section already gives the verified split.
- Train, bus and ferry times and fares come only from the transit agency's own schedule or fare pages (NJ Transit, SEPTA, MARC, VRE, WMATA, DART, Amtrak for Amtrak trains) - never trip planners or aggregators (Rome2rio, Moovit, Wanderu); name the line and the stations.
- For every law, tax rule, credit, exemption or disclosure duty the article will lean on, also look for (a) exceptions that fit the article's situation - an estate or trust sale, a foreclosure, a condo, a manufactured home, a first-time buyer, an older homeowner - and (b) changes enacted or taking effect in the last two years. Report each exception or change as its own item from the official source (e.g. 'Maryland's seller disclosure law does not apply to sales by a personal representative administering an estate').
- For every program, credit or tax break you report, say its basic eligibility condition in the claim when the source gives it (who it's for, whether the buyer must live in the home as a principal residence) - the writer uses that to leave out programs that don't fit the reader.
- Never cite a political party, caucus or campaign site (a "House Democrats" or "Senate Republicans" page) - find the same fact on the government agency's own page or a news report.
- Laws and regulations come from the CURRENT official code or agency page (the state legislature's code site, e.g. law.lis.virginia.gov, legis.delaware.gov, mgaleg.maryland.gov, njleg.state.nj.us, legis.state.pa.us) - never an archived or dated copy (a 2020 code edition, a law firm's old summary); cite the section and say if it was renumbered or replaced.
- Keep geography exact: postal town vs township/municipality vs county vs school district vs ZIP. If a fact is for a township or county rather than the town, say which.
- Loan and program rules: keep the loan type and any conditions with the rule (e.g. "on PHFA's conventional Keystone Home Loan, ..."), never a bare "buyers must...".
- Schools: districts, boundaries, budgets, millage, official policies - never rankings or opinions unless attributed to a named source.
- Never research or report anything about the demographics, religion, ethnicity or "type" of residents, or crime.
- Don't repeat facts listed under ALREADY KNOWN - find what's missing.
- When you're done, call submit_evidence exactly once with everything you verified, and list in not_found what you couldn't verify."""

# ---------------------------------------------------------------------------
# Fact-check stage
# ---------------------------------------------------------------------------

_REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Submit the fact-check and compliance review.",
    "input_schema": {"type": "object", "properties": {
        "claims": {"type": "array", "items": {"type": "object", "properties": {
            "quote": {"type": "string"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "verdict": {"type": "string", "enum": ["supported", "partly_supported", "unsupported", "stale", "analysis"]},
        }, "required": ["quote", "evidence_ids", "verdict"]}},
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["unsupported_claim", "wrong_number", "stale_data", "wrong_geography", "market_definition", "overgeneralization",
                                                "fair_housing", "school_language", "invented_experience", "causation", "legal_tax", "generic_ai_phrasing",
                                                "repetition", "placeholder", "reader_voice", "rule_detail", "applicability", "wrong_reader", "other"]},
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "quote": {"type": "string"},
            "problem": {"type": "string"},
            "find": {"type": "string", "description": "Exact verbatim substring of the article to replace (unique, within one sentence or list item)."},
            "replace": {"type": "string", "description": "Corrected text - qualify, attribute, date, soften or remove. Empty string deletes it. No new facts."},
        }, "required": ["kind", "severity", "quote", "problem", "find", "replace"]}},
        "scores": {"type": "object", "properties": {
            k: {"type": "integer"} for k in ("accuracy", "local_specificity", "usefulness", "voice", "readability", "seo", "compliance")
        }, "required": ["accuracy", "local_specificity", "usefulness", "voice", "readability", "seo", "compliance"]},
        "summary": {"type": "string"},
        "improvements": {"type": "array", "items": {"type": "string"}},
        "rules_mentioned": {"type": "array", "description": "Every law, contract right, program, tax break, exemption, credit, loan limit or "
                            "deadline the article names (after your fixes).", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "official_source": {"type": "string", "description": "The ledger's official URL for it, if any."}},
            "required": ["name"]}},
    }, "required": ["claims", "issues", "scores", "summary", "improvements", "rules_mentioned"]},
}

_REVIEW_SYSTEM = """You are the fact-check and compliance editor for a licensed real estate agent's blog - strict, specific and fair. You receive the EVIDENCE LEDGER the writer was allowed to use, the agent's brief, and the finished ARTICLE.

1. In `claims`, list every factual claim (numbers, dates, places, businesses, schools/districts, market statements, rates, taxes, statements about the agent's experience), quoted exactly, with the ledger ids that support it and a verdict: supported (the ledger says it - same number, date window and geography), partly_supported (right idea but a detail is off, unattributed or undated), unsupported (not in the ledger or brief), stale (the ledger date is too old to state as current), analysis (clearly labeled interpretation - fine).
2. In `issues`, report every problem: unsupported_claim, wrong_number, stale_data, wrong_geography (check EVERY statement about which township, county or school district a town is in against that town's `geography` ledger items - a township or district they don't list is wrong even if another ledger item says it; ZIP vs township confusion; assuming one district; one town's split applied to another), market_definition (areas' counts added together or treated as separate when an Overlap item says they share homes; homes under contract or pending called "for sale"; an area's numbers described as something other than its Definition item; a small sample stated without its caveat; a year-ago comparison the ledger marks unreliable), overgeneralization (a rule stated for everyone that the ledger gives for one loan type, program or place - "buyers with under 20% down must get mortgage insurance" is conventional-only; must/always/never/required/all without a ledger item saying exactly that), fair_housing (protected classes, steering, "family", "safe", "perfect for..."), school_language (any subjective school/district wording), invented_experience (agent involvement, buyer/seller side, clients, years or volume not in the brief - but an opinion or observation the agent wrote in the brief, framed as theirs ("In my experience, ..."), is supported by the brief and must stay framed as theirs; flag it only when it is stated as a general fact), causation (unsupported cause-and-effect), legal_tax (definitive legal/tax advice, advising to waive contingencies), generic_ai_phrasing (filler, hype, cliches like nestled/vibrant/dream home/hidden gem/navigating), repetition, applicability (a flat statement that a law, tax, exemption, credit, disclosure duty or eligibility rule does or doesn't apply - "the seller must disclose", "it doesn't carry over", "you'll owe" - when it depends on the reader's situation, or one that ignores an exception the ledger gives for THIS article's situation: rewrite as the general idea + 'exceptions apply' (naming the one that fits, in general words) + the professional to confirm with, in the agent's first-person voice), rule_detail (a law, contract right, deadline, program, tax break, exemption, credit, rebate or loan limit stated WITH its amounts, benefit percentages, income or age cut-offs, residency periods, day counts, deadlines, repayment terms or loan-limit figures - the article may only name it and what it does in general words: replace the detail with one sentence that names it, says conditions apply and sends the reader to the agent; keep any "Ask" line under it; who customarily pays a closing tax and a dated mortgage rate are allowed, but a transfer, grantor or recordation tax rate or amount is fine print too), wrong_reader (a program, tax break, loan rule or calculation whose basic requirement doesn't fit this article's reader - a first-time-buyer or homestead benefit, or a 5% down payment, in an article for investors buying a rental they won't live in; a buyer program in a seller article: delete it with ""), placeholder (any blank, bracketed template text, "$___", "(area)", "(date)", "[insert ...]", "TBD" or note to the agent - the article is published to the public as-is: fill it from the ledger or delete it with ""), reader_voice (the article is for the agent's clients and the public, not other agents: insider jargon left unexplained - days on market, sale-to-list, seller assist, millage, conforming limit, jumbo, contingency - wording aimed at agents, valuation-method talk, or talk about how the data was gathered such as "homes checked" or "records matched", talk about which copy of a law or source was read ("as the 2020 code reads; current law may differ"), a mission statement lifted from the agent's About text ("My work centers on careful valuation"), or the agent referred to in the third person or by name ("ask Peter", "contact your agent"): rewrite in plain words a buyer or seller would use, same facts), other.
3. Give every issue a minimal fix: `find` = an exact, verbatim substring of the ARTICLE text (copy it character for character, long enough to be unique, inside one sentence or list item), `replace` = the corrected text (qualify, attribute, add the date, soften, correct it from the ledger, or "" to delete). Never add a fact that isn't in the ledger. Keep any [anchor](url) link markup intact in your replacement unless the link itself is the problem. Every item under AUTOMATED CHECKS is a confirmed problem - each needs an issue with a fix. The elements listed under INSERTED ELEMENTS are real parts of the article - a sentence pointing to them is not an unsupported claim. A figure the ledger only has second-hand (a news site "citing" the primary source) or marks as unconfirmed gets DELETED, not hedged, when the primary figure is also present. The article must never talk about its own sourcing ("I haven't confirmed...", "according to my research", "the ledger") - fix those by deleting or rewriting.
4. Score 1-10 - honestly, a 9-10 must be earned: accuracy, local_specificity (could only have been written about this place), usefulness (answers the reader's real question), voice (sounds like a knowledgeable local agent talking to a client, not AI and not agent-to-agent), readability (a buyer or seller with no real estate background can follow it), seo, compliance.
5. `summary`: 1-2 plain sentences for the agent. `improvements`: up to 4 specific things the agent could add (their own experience, a photo, a number they have) to make it stronger.
6. `rules_mentioned`: every law, contract right, deadline, program, tax break, exemption, credit, rebate or loan limit the article names (after your fixes) - the transfer tax and a first-time-buyer transfer-tax break count - each with the ledger's official URL when it has one. An empty list only when the article names none.

Respond only by calling submit_review, exactly once."""


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _new_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "web_search_requests": 0}


def _add_usage(acc: dict, resp) -> None:
    u = resp.usage
    acc["input_tokens"] += getattr(u, "input_tokens", 0) or 0
    acc["output_tokens"] += getattr(u, "output_tokens", 0) or 0
    acc["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    acc["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
    stu = getattr(u, "server_tool_use", None)
    if stu is not None:
        acc["web_search_requests"] += getattr(stu, "web_search_requests", 0) or 0


def _cost(config: dict, model: str, u: dict) -> float:
    p = (config.get("model_pricing") or {}).get(model) or {}
    inp, out = p.get("input_per_mtok", 0.0), p.get("output_per_mtok", 0.0)
    return round(
        (u["input_tokens"] * inp
         + u["cache_creation_input_tokens"] * inp * config.get("blog_pipeline_cache_write_multiplier", 1.25)
         # Cache reads are 0.1x input on most models, 0.05x on Opus 5.5 / Sonnet 5.5 (model_pricing).
         + u["cache_read_input_tokens"] * inp * p.get("cache_read_multiplier", config.get("blog_pipeline_cache_read_multiplier", 0.1))
         + u["output_tokens"] * out) / 1_000_000
        + u["web_search_requests"] * config.get("blog_pipeline_web_search_cost_usd", 0.01),
        6,
    )


def _tool_input(resp, name: str) -> Optional[dict]:
    for b in resp.content:
        if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == name and isinstance(b.input, dict):
            return b.input
    return None


async def _run_tool_call(*, model: str, system: str, user_msg: str, tools: list[dict], tool_name: str, max_tokens: int,
                         effort: Optional[str], usage: dict) -> dict:
    """One model turn that must end in `tool_name` (forced tool_choice isn't available on the 5.5
    models, so this asks, resumes server-tool pauses, and nudges once if the call didn't come)."""
    client = writer._client()
    messages: list[dict] = [{"role": "user", "content": user_msg}]
    extra = {"output_config": {"effort": effort}} if effort else None
    nudged = False
    for _ in range(6):
        kwargs: dict[str, Any] = dict(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools, tool_choice={"type": "auto"}, messages=messages,
        )
        if extra:
            kwargs["extra_body"] = extra
        async with client.messages.stream(**kwargs) as stream:
            resp = await stream.get_final_message()
        _add_usage(usage, resp)
        if resp.stop_reason == "refusal":
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI declined this request - try rewording the topic.")
        payload = _tool_input(resp, tool_name)
        if payload is not None:
            return payload
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue  # server-side tool loop paused - resend and it resumes
        if resp.stop_reason == "max_tokens":
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI ran out of room before finishing - please try again.")
        if nudged:
            break
        nudged = True
        messages.append({"role": "user", "content": f"Now call {tool_name} with your result."})
    raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI didn't return a result - please try again. Nothing was charged for this try.")


class _Meter:
    """Records each stage as its own usage event on the agent's allowance, and keeps the totals."""

    def __init__(self, db: AsyncSession, user: User, config: dict):
        self.db, self.user, self.config = db, user, config
        self.stages: list[dict] = []
        self.total = 0.0
        self.events: list = []

    async def record(self, stage: str, model: str, usage: dict, started: float, *, failed: Optional[str] = None) -> None:
        cost = _cost(self.config, model, usage)
        self.total += cost
        self.stages.append({"stage": stage, "model": model, "cost_usd": round(cost, 4), "seconds": round(time.monotonic() - started, 1),
                            "input_tokens": usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"],
                            "output_tokens": usage["output_tokens"], "web_searches": usage["web_search_requests"]})
        self.events.append(await record_usage_event(
            self.db, self.user.id, None, model, usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"],
            usage["output_tokens"], cost, 1, bool(failed), failed, tool_names=[f"blog_pipeline_{stage}"], event_type=EVENT_TYPE,
        ))

    async def refund(self) -> None:
        """A run that ends without an article costs the agent nothing (user, 10/17 - Peter's usage
        went up for blog runs that failed): every stage it charged stays in the log, marked
        "refunded_failed_run", and drops out of the monthly allowance (which only counts
        un-blocked events)."""
        from sqlalchemy import update
        from app.models.database import AIUsageEvent
        ids = [e.id for e in self.events if e is not None and not e.blocked]
        if not ids:
            return
        try:
            await self.db.rollback()
            await self.db.execute(update(AIUsageEvent).where(AIUsageEvent.id.in_(ids)).values(blocked=True, block_reason="refunded_failed_run"))
            await self.db.commit()
            self.total = 0.0
        except Exception:
            logger.warning("blog pipeline: refund of a failed run failed", exc_info=True)


async def refund_since(db: AsyncSession, owner_id: str, since_iso) -> None:
    """Refund a run that died without finishing (its job went stale): the article-pipeline stages
    this agent was charged for since that run started."""
    from datetime import datetime
    from sqlalchemy import update
    from app.models.database import AIUsageEvent
    try:
        since = datetime.fromisoformat(str(since_iso))
    except (TypeError, ValueError):
        return
    try:
        await db.execute(update(AIUsageEvent).where(
            AIUsageEvent.owner_id == owner_id, AIUsageEvent.event_type == EVENT_TYPE,
            AIUsageEvent.blocked.is_(False), AIUsageEvent.created_at >= since,
        ).values(blocked=True, block_reason="refunded_failed_run"))
        await db.commit()
    except Exception:
        logger.warning("blog pipeline: stale-run refund failed", exc_info=True)
        await db.rollback()


async def _gate(db: AsyncSession, user: User, config: dict, engine: str) -> None:
    """Only start when the agent's remaining monthly allowance covers a typical full run - the
    allowance is the only limit (no article count), and a run can't push them past it."""
    if user.id in _MONTHLY_BUDGET_UNLIMITED_OWNER_IDS:
        return
    from app.services.ai.ai_rate_limit_service import monthly_limit_usd
    limit = await monthly_limit_usd(db, user.id, config, user.plan_tier)
    if not limit:
        return
    need = float((config.get("blog_pipeline_gate_usd") or {}).get(engine, 1.0))
    if await total_monthly_ai_cost(db, user.id) + need > float(limit):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"There isn't enough AI usage left this month for a full researched article. {resets_text((await usage_period(db, user.id))[1])}",
        )


# ---------------------------------------------------------------------------
# Evidence ledger
# ---------------------------------------------------------------------------

def _sale_items(sales: list[dict]) -> list[dict]:
    out = []
    for s in sales:
        bits = [writer._money(s.get("price")) and f"closed at {writer._money(s.get('price'))}",
                s.get("sold_date") and f"on {s['sold_date']}",
                s.get("beds") and f"{s['beds']} bd", s.get("baths") and f"{s['baths']} ba",
                s.get("sqft") and f"{s['sqft']} sqft"]
        out.append({"topic": "agent_sales", "claim": f"{s.get('address')}, {s.get('city')} {', '.join(b for b in bits if b)} - one of the agent's own closed transactions (buyer/seller side not recorded).",
                    "source_name": "Bright MLS (agent's closed sales)", "source_url": None, "as_of": s.get("sold_date"), "origin": "agent_sales", "town": s.get("city")})
    return out


def _listing_items(listings: list[dict]) -> list[dict]:
    return [{"topic": "agent_listing", "claim": f"{writer._fmt_property(p)} - a listing the agent attached.", "source_name": "Bright MLS (agent's listing)",
             "source_url": None, "as_of": datetime.now(timezone.utc).date().isoformat(), "origin": "agent_listing", "town": p.get("city")} for p in listings]


def _build_ledger(items: list[dict], per_town_cap: int = 70) -> tuple[list[dict], str]:
    """Number every item [E1]..[En] and render the ledger text."""
    seen, ledger, per_town = set(), [], {}
    for it in items:
        key = (it.get("claim") or "").strip().lower()
        if not key or key in seen:
            continue
        town = it.get("town") or "general"
        if it.get("origin") in ("idx_research", "evidence_bank") and per_town.get(town, 0) >= per_town_cap:
            continue
        per_town[town] = per_town.get(town, 0) + 1
        seen.add(key)
        ledger.append({**it, "eid": f"E{len(ledger) + 1}"})
    lines = []
    for it in ledger:
        src = it.get("source_name") or "source"
        url = f" <{it['source_url']}>" if it.get("source_url") else ""
        lines.append(f"[{it['eid']}] ({it.get('topic')} | {src}{' | as of ' + str(it['as_of']) if it.get('as_of') else ''}) {it['claim']}{url}")
    return ledger, "\n".join(lines)


# ---------------------------------------------------------------------------
# Deterministic checks + the worked payment example (no model involved)
# ---------------------------------------------------------------------------

_LOAN_RULE = re.compile(r"\b(mortgage insurance|PMI|MIP|down payment|downpayment)\b", re.I)
_ABSOLUTE = re.compile(r"\b(must|required|requires|have to|has to|need to|needs to|always|never|all buyers|every buyer)\b", re.I)
_LOAN_TYPE = re.compile(r"\b(conventional|FHA|VA\b|USDA|PHFA|Keystone|HomeReady|Home Possible|jumbo|K-FIT|HFA Preferred|government)", re.I)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")


def loan_rule_lint(text: str) -> list[str]:
    """Sentences stating a mortgage-insurance / down-payment rule as absolute without naming the
    loan type (10/17: "Borrowers who put down less than 20% must also get mortgage insurance" -
    true for conventional loans, not VA)."""
    out = []
    for s in _SENTENCES.split(text or ""):
        if _LOAN_RULE.search(s) and _ABSOLUTE.search(s) and not _LOAN_TYPE.search(s):
            out.append(s.strip())
    return out


def _monthly_pi(loan: float, rate_pct: float, years: int = 30) -> float:
    r, n = rate_pct / 1200, years * 12
    return loan / n if r == 0 else loan * r / (1 - (1 + r) ** -n)


def _round_price(v: float) -> int:
    step = 10_000 if v >= 500_000 else 5_000
    return int(round(v / step) * step)


_BUYER_WORDS = re.compile(r"\b(buy|buyer|buying|first[- ]time|afford|mortgage|payment|down payment|preapprov|purchase|starter)", re.I)


def wants_payment_example(brief: dict) -> bool:
    # A primary-home example (5-20% down, the national average rate) doesn't fit a rental or second
    # home - investment loans need more down and price differently (11/01 grader).
    if investment_article(brief):
        return False
    text = " ".join([str(brief.get("topic") or ""), str(brief.get("goal") or ""), " ".join(brief.get("audiences") or []), str(brief.get("key_points") or "")])
    return bool(_BUYER_WORDS.search(text))


_INVESTMENT = re.compile(r"\b(rental property|rental properties|first rental|investment property|investment properties|investor|investing|"
                         r"landlord|buy(?:ing)? (?:a )?rental|second home|vacation home|short[- ]term rental|airbnb)", re.I)
_OCCUPANT_ONLY = re.compile(r"first[- ]time (?:home ?)?buyer|homestead|principal residence|primary residence|owner[- ]occupan|"
                            r"must (?:live|occupy|reside)|occupy the (?:home|property)", re.I)


def investment_article(brief: dict) -> bool:
    """An article about buying a home the reader won't live in (a rental, an investment, a second home) -
    by its topic or goal, or when Investors is its only audience."""
    text = " ".join([str(brief.get("topic") or ""), str(brief.get("goal") or "")])
    audiences = [a.lower() for a in brief.get("audiences") or []]
    return bool(_INVESTMENT.search(text)) or audiences == ["investors"]


def fit_to_reader(items: list[dict], brief: dict) -> list[dict]:
    """11/01 grader (Wilmington rental article): programs that require the buyer to live in the home
    (first-time-buyer transfer-tax breaks, homestead credits) never reach an investment-property
    article's ledger - "conditions apply" can't rescue a benefit the reader plainly can't use."""
    if not investment_article(brief):
        return items
    return [i for i in items if i.get("topic") == "demographics" or i.get("origin") in ("mls_live", "geo_verified")
            or not _OCCUPANT_ONLY.search(str(i.get("claim") or ""))]


_MOVE_UP = re.compile(r"move[- ]?up|moving up|bigger|larger home|upsiz|luxury|forever home|step up", re.I)


_HOUSE_TYPE = re.compile(r"single[- ]family|detached (?:home|house)|\bhouses?\b(?! hunt)", re.I)
_ATTACHED_TYPE = re.compile(r"\bcondo|townho(?:me|use)|\battached\b", re.I)


def share_at_least(row: dict, n: float) -> bool:
    """A place row's share can be text ("under 1" for grouped small towns - 11/01 Annapolis crash);
    compare its number ("sort") when it has one."""
    v = row.get("sort", row.get("share"))
    return isinstance(v, (int, float)) and v >= n


def payment_basis(brief: dict) -> str:
    """The home type the example is drawn from, by what the article is about: 'house' for a
    single-family, move-up or luxury article, 'attached' for a condo / townhome article, else
    'entry' (the cheapest quarter of everything for sale). 11/01 grader: a single-family
    Frederick article got a $390,000 example drawn mostly from condos and townhomes."""
    text = " ".join([str(brief.get("topic") or ""), str(brief.get("goal") or ""), " ".join(brief.get("audiences") or [])])
    if _MOVE_UP.search(text) or _HOUSE_TYPE.search(text):
        return "house"
    return "attached" if _ATTACHED_TYPE.search(text) else "entry"


def payment_example(entry: dict, rate: dict, area_names: list[str], *, conforming_limit: Optional[int] = None, limit_area: Optional[str] = None,
                    basis: str = "entry") -> Optional[dict]:
    """A worked example for a realistic entry price (the 25th-percentile list price of homes for
    sale across the article's towns): down payment, loan, principal & interest at Freddie Mac's
    latest 30-year average, a property-tax estimate from similar listings' tax bills, and closing
    costs at the CFPB's 2-5% range. Every number is computed here, never by the writer."""
    if not entry or not entry.get("p25") or not rate or not rate.get("rate_30"):
        return None
    kind = basis if basis in ("house", "attached") and entry.get(f"{basis}_median") else "entry"
    price = _round_price(entry[f"{kind}_median"] if kind != "entry" else entry["p25"])
    tax_pct = entry.get(f"tax_pct_near_{kind}") if kind != "entry" else entry.get("tax_pct_near_p25")
    tax_mo = round(price * tax_pct / 100 / 12) if tax_pct else None
    scenarios = []
    for pct in (5, 10, 20):
        down = round(price * pct / 100)
        loan = price - down
        pi = round(_monthly_pi(loan, rate["rate_30"]))
        scenarios.append({"down_pct": pct, "down": down, "loan": loan, "principal_interest": pi,
                          "mortgage_insurance": "none" if pct >= 20 else "lender quote", "tax": tax_mo,
                          "monthly_before_insurance": pi + (tax_mo or 0),
                          "jumbo": bool(conforming_limit and loan > conforming_limit)})
    return {
        "price": price, "price_basis": (f"About the median list price of the {entry['houses']} single-family houses for sale across {_and_list(area_names)}." if kind == "house"
                                        else f"About the median list price of the {entry['attached']} condos and townhomes for sale across {_and_list(area_names)}." if kind == "attached"
                                        else f"About a quarter of the {entry['homes']} homes for sale across {_and_list(area_names)} are listed at or below this price."),
        "rate": rate["rate_30"], "rate_as_of": rate.get("as_of"), "term_years": 30,
        # The estimate's basis in words only - a tax bill as a percent of price reads like a tax
        # rate (11/01 grader, Princeton). The percent stays internal (tax_pct) for the math.
        "tax_pct": tax_pct, "tax_basis": ({"house": "tax bills of similar-priced single-family houses for sale",
                                           "attached": "tax bills of similar-priced condos and townhomes for sale"}.get(kind, "tax bills of similar-priced homes for sale")
                                          if tax_pct else None),
        "closing_low": round(price * 0.02), "closing_high": round(price * 0.05),
        # The county limit the jumbo marks were judged by; without one, jumbo status isn't claimed.
        "limit_known": bool(conforming_limit), "limit_area": limit_area if conforming_limit else None,
        "scenarios": scenarios, "as_of": datetime.now(timezone.utc).date().isoformat(),
    }


def payment_example_items(ex: dict) -> list[dict]:
    """The example's numbers as ledger items, so the writer can point to them and the number
    audit recognizes them."""
    m = writer._money
    items = [f"Worked payment example (OpenHousePal calculation): a {m(ex['price'])} home. {ex['price_basis']} Rate used: Freddie Mac's 30-year average of {ex['rate']:.2f}% (week of {ex['rate_as_of']}); a buyer's own rate will differ."]
    area = ex.get("limit_area")
    for s in ex["scenarios"]:
        # Each scenario says which side of the county's limit it's on, so the writer never generalizes
        # one scenario to all (11/01 Ashburn: "even at 20% down, the loan is still a jumbo loan").
        if not ex.get("limit_known"):
            loan_kind = "."
        elif s.get("jumbo"):
            loan_kind = (f" - above {area}'s 2026 conforming loan limit, so a jumbo loan (Freddie Mac's average covers conforming loans; "
                         "a jumbo rate needs a lender quote).")
        else:
            loan_kind = f" - within {area}'s 2026 conforming loan limit, so a conforming loan."
        items.append(f"Payment example, {s['down_pct']}% down: {m(s['down'])} down, {m(s['loan'])} loan, about {m(s['principal_interest'])}/month principal and interest"
                     + (f" plus about {m(s['tax'])}/month property tax" if s.get("tax") else "")
                     + ("" if s["down_pct"] >= 20 else " plus mortgage insurance on a conventional loan (lender quote)") + loan_kind)
    # No county limit on file and the biggest loan tops the national baseline: say it depends.
    if not ex.get("limit_known") and max(s["loan"] for s in ex["scenarios"]) > (tk.conforming_limit() or 0):
        items.append("Payment example loan size: whether a loan this size is a jumbo loan depends on the amount borrowed and the conforming loan limit "
                     "for the property's county - not checked here. Ask a lender which loan options apply.")
    items.append(f"Payment example closing costs: {m(ex['closing_low'])}-{m(ex['closing_high'])} (the CFPB's typical 2-5% of the price), on top of the down payment.")
    out = [{"topic": "market", "claim": c, "source_name": "OpenHousePal calculation", "source_url": None, "as_of": ex["as_of"], "confidence": 0.99,
            "town": "general", "origin": "calc"} for c in items]
    if ex.get("limit_source"):
        src = ex["limit_source"]
        out.append({"topic": "regulation", "claim": f"Conforming loan limits are set county by county by FHFA; the payment example's jumbo / conforming marks use "
                                                    f"{area}'s 2026 one-unit limit from FHFA's county list. Whether a buyer's own loan is jumbo depends on the amount they borrow.",
                    "source_name": src["source_name"], "source_url": src["source_url"], "as_of": src["published"], "confidence": 0.99, "town": "general", "origin": "calc"})
    return out


_DATE_DAY = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?(?!\d)")
_DATE_RANGE = re.compile(r"^(FY\s*)?(\d{4})\s*[-–/]\s*(\d{4}|\d{2})(?!\d)", re.I)
_DATE_YEAR = re.compile(r"^(FY\s*)?(\d{4})(?!\d)", re.I)


def _public_date(as_of) -> Optional[str]:
    """A source's date as readers see it: '2026-10 (page read 2026-10-02)' -> '2026-10';
    research notes in the as-of field stay in the agent's report, not on the page. A school or
    fiscal year stays whole: '2025-2026' (11/01 round-10 grader: it came out '2025-20')."""
    s = str(as_of or "").strip()
    if m := _DATE_DAY.match(s):
        return m.group(0)
    if m := _DATE_RANGE.match(s):
        return f"{'FY ' if m.group(1) else ''}{m.group(2)}-{m.group(3)}"
    m = _DATE_YEAR.match(s)
    return f"{'FY ' if m.group(1) else ''}{m.group(2)}" if m else None


def _and_list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


# One state-specific line under the payment card's closing costs (from that state's own facts).
_CLOSING_NOTES = {
    # No rates on the card either - the same no-fine-print rule as the article (11/02: the card said
    # "1% state plus at least 1% local", the one place a tax rate still reached every PA article).
    "PA": "In Pennsylvania that includes your share of the state and local realty transfer tax - who pays what is set in the Agreement of Sale.",
}

_COST_HEADING = re.compile(r"\b(mortgage|rate|rates|afford|budget|payment|preapprov|pre-approv|cost|costs|down payment|financ|tax)", re.I)


def place_payment_example(blocks: list[dict], ex: dict) -> list[dict]:
    """The payment card goes at the end of the first section about mortgages / budgets / costs
    (never the closing section), else just before the closing heading."""
    heads = [i for i, b in enumerate(blocks) if b.get("type") == "heading"]
    card = {"id": writer._bid("payment_example"), "type": "payment_example", "data": ex}
    at = None

    def plain(b: dict) -> str:
        d = b.get("data") or {}
        return re.sub(r"<[^>]+>", " ", " ".join(str(x) for x in [d.get("html") or d.get("text") or ""] + list(d.get("items") or [])))
    # The section whose text points to the card ("the payment example below") gets it (11/01
    # Frederick: the card went after "What starter homes ... cost" while the text pointing to it
    # sat a section later); else the first section with a cost/mortgage heading.
    for n, h in enumerate(heads[:-1]):
        if any(re.search(r"payment example", plain(b), re.I) for b in blocks[h + 1:heads[n + 1]]):
            at = heads[n + 1]
            break
    for n, h in enumerate(heads[:-1]):
        if at is not None:
            break
        if _COST_HEADING.search(plain(blocks[h])):
            at = heads[n + 1]
            break
    if at is None:
        at = heads[-1] if heads else len(blocks)
    return blocks[:at] + [card] + blocks[at:]


_FIX_TOOL = {
    "name": "submit_fixes",
    "description": "Submit exact find/replace corrections.",
    "input_schema": {"type": "object", "properties": {"fixes": {"type": "array", "items": {"type": "object", "properties": {
        "find": {"type": "string", "description": "Exact verbatim substring of the article."},
        "replace": {"type": "string", "description": "Corrected text; \"\" deletes it."},
        "problem": {"type": "string"},
    }, "required": ["find", "replace", "problem"]}}}, "required": ["fixes"]},
}

_FIX_SYSTEM = """You correct specific, confirmed errors in a real estate agent's article. For each problem you get the exact sentence and the verified facts. Return a minimal fix per problem: `find` = an exact verbatim substring of the article containing the error, `replace` = the corrected wording using ONLY the verified facts given (same voice, keep any [anchor](url) links), or "" to delete when it can't be corrected cleanly. For a MISSING item, `find` = the most related existing sentence and `replace` = that same sentence followed by the new one. Never add facts beyond the ones given. Respond only by calling submit_fixes."""


_GRADE_TOOL = {
    "name": "submit_grade",
    "description": "Grade the article against the pass list.",
    "input_schema": {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string", "enum": bfc.PASS_IDS},
            "status": {"type": "string", "enum": ["pass", "fail"]},
            "problems": {"type": "array", "items": {"type": "object", "properties": {
                "quote": {"type": "string", "description": "The exact, complete sentence from the article that has the problem, copied verbatim (\"\" for something missing)."},
                "why": {"type": "string", "description": "One sentence: what is wrong."},
                "fix": {"type": "string", "description": "What the corrected sentence must say, using only ledger facts."},
            }, "required": ["quote", "why", "fix"]}},
        }, "required": ["id", "status"]}},
        "nice_to_have": {"type": "array", "items": {"type": "string"}, "description": "Style suggestions and minor notes - never blockers."},
    }, "required": ["items", "nice_to_have"]},
}

_GRADE_SYSTEM = """You are the final grader for a real estate agent's article before it is saved. Grade it against the PASS LIST only, item by item, using the evidence ledger as the truth.

Fail an item ONLY for a clear, specific error a careful outside reviewer would mark - something wrong, missing for this reader, or misleading. If, after looking, you conclude a sentence is fine, the item PASSES - never fail an item and then explain that it is fine. Minor wording, flow, repetition and "could be clearer" notes go in nice_to_have, never as failures.

For each failure quote the exact, complete sentence from the article (verbatim), say what is wrong in one sentence, and say what the corrected sentence must say using ONLY ledger facts.

Things that are NOT problems:
- Lines like [Ask-me line: ...] are real rendered elements (a small "Ask me" row with call/text/email buttons), not placeholders. The Town Spotlight card, payment example card, sources list and closing button are inserted around the text automatically - "the payment example below" is correct.
- Market numbers from the ledger are not fine print: median tax bills in dollars, fees, prices, counts, days on market, the dated mortgage rate, the CFPB 2-5% closing-cost range. Fine print means rates of a tax, a property tax given as a percent (of list price, value or anything else - readers take it for an official rate), program amounts or limits, eligibility cut-offs, legal day counts or deadlines, and which office collects or receives a closing tax or fee (local procedure).
- The agent's own closing offer ("I'll start your search", "I'll set up showings") is correct even for towns outside the agent's state. The agent's personal take from the brief is theirs to say.

Respond only by calling submit_grade."""


def _pass_list_text(ids: Optional[list[str]] = None) -> str:
    return "\n".join(f"- {pid}: {label} - {rule}" for pid, label, rule in bfc.PASS_LIST if ids is None or pid in ids)


def _whole_sentences(article: dict, find: str) -> bool:
    """True when `find` is one or more complete sentences of the article - a deletion of anything
    less leaves a fragment (11/01 round 6: ". , so you may face competition")."""
    find = (find or "").strip()
    if not find or find[-1] not in ".!?\"'”)":
        return False
    text = _article_text(article)
    at = text.find(find)
    if at < 0:
        return False
    before = text[:at].rstrip(" ")
    return not before or before[-1] in ".!?\n\"'”)" or before.endswith(("**", "- "))


def _cut_target(article: dict, quote: str) -> Optional[str]:
    """What to delete for a code-confirmed break: the quote itself when it's whole sentences, or the
    bullet's text when it's one whole list item - 11/01 Lancaster: seven "Lancaster City: 41.3186
    mills" bullets were flagged but never cut, since a bullet has no closing period."""
    quote = (quote or "").strip()
    if _whole_sentences(article, quote):
        return quote
    item = quote[2:].strip() if quote.startswith("- ") else quote
    return item if item and f"- {item}" in _article_text(article).splitlines() else None


def _tidy_spaces(node):
    """Collapse the doubled spaces a deletion leaves inside a paragraph."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                node[k] = re.sub(r"(?<=\S) {2,}(?=\S)", " ", v)
            else:
                _tidy_spaces(v)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                node[i] = re.sub(r"(?<=\S) {2,}(?=\S)", " ", v)
            else:
                _tidy_spaces(v)


async def _grade_and_fix(*, article: dict, ledger_text: str, brief: dict, brief_msg: str, inserted: list[str], protections: list[dict],
                         agent_name: str, spec: dict, meter: "_Meter", geo_check) -> tuple[list[dict], list[str], list[dict], int]:
    """The engine's own grader, its last step: code checks + an AI grade against bfc.PASS_LIST, one
    targeted rewrite of only the failing sentences, a re-check of those items. Only code-confirmed
    rule breaks are cut; anything else still failing is flagged to the agent.

    Guards (11/01 round 6 - the first version made articles worse): the AI grade never cuts on its
    own; a deletion must be whole sentences; and a rewrite that breaks a code check that passed
    (e.g. drops the "who pays" part of a reader protection) is undone.
    Returns (pass_list, nice_to_have, issues, applied)."""
    import copy

    def checks() -> dict[str, list[dict]]:
        text = _article_text(article)
        found = bfc.code_checks(text, brief, protections, agent_name)
        for f in geo_check(text):
            found.setdefault("place", []).append({"quote": f["sentence"], "why": f"puts {f['town']} in {f['place']}", "fix": f"Verified: {f['truth']}."})
        return found

    def broken(before: dict, after: dict) -> bool:
        return any(len(after.get(k, [])) > len(before.get(k, [])) for k in after)

    async def grade(ids: Optional[list[str]], code: dict, stage: str) -> dict:
        msg = (f"PASS LIST:\n{_pass_list_text(ids)}\n\nAgent's brief:\n{brief_msg}\n\nINSERTED ELEMENTS:\n" + "\n".join(f"- {x}" for x in inserted)
               + ("\n\nAUTOMATED CODE CHECKS FOUND (confirmed - include them):\n" + "\n".join(f"- {k}: \"{p['quote']}\" ({p['why']})" for k, ps in code.items() for p in ps) if code else "")
               + f"\n\n== EVIDENCE LEDGER ==\n{ledger_text}\n\n== ARTICLE ==\n{_article_text(article)}")
        started, usage = time.monotonic(), _new_usage()
        try:
            got = await _run_tool_call(model=spec["check_model"], system=_GRADE_SYSTEM, user_msg=msg, tools=[_GRADE_TOOL], tool_name="submit_grade",
                                       max_tokens=8000, effort="high", usage=usage)
            await meter.record(stage, spec["check_model"], usage, started)
            return got
        except Exception:
            await meter.record(stage, spec["check_model"], usage, started, failed=f"{stage}_failed")
            logger.warning("blog pipeline: %s failed", stage, exc_info=True)
            return {}

    def merge(code: dict, got: dict, ids: list[str]) -> dict[str, list[dict]]:
        fails: dict[str, list[dict]] = {k: [dict(p, code=True) for p in v] for k, v in code.items() if k in ids}
        for it in writer._coerce(got.get("items"), list):
            if isinstance(it, dict) and it.get("id") in ids and it.get("status") == "fail":
                have = {p["quote"] for p in fails.get(it["id"], [])}
                for p in writer._coerce(it.get("problems"), list):
                    if isinstance(p, dict) and str(p.get("quote") or "").lstrip().startswith("[Ask-me line"):
                        continue  # a real Ask line, not unfinished text
                    if isinstance(p, dict) and (p.get("why") or p.get("quote")) and p.get("quote", "") not in have:
                        fails.setdefault(it["id"], []).append({"quote": p.get("quote") or "", "why": p.get("why") or "", "fix": p.get("fix") or ""})
        return {k: v for k, v in fails.items() if v}

    code = checks()
    first = await grade(None, code, "grade")
    nice = [str(n) for n in writer._coerce(first.get("nice_to_have"), list) if n][:8]
    failed = merge(code, first, bfc.PASS_IDS)
    issues: list[dict] = []
    applied = 0
    still: dict[str, list[dict]] = {}
    if failed:
        problems = [f"- [{pid}] " + (f"\"{p['quote']}\": " if p["quote"] else "MISSING: ") + f"{p['why']}. Fix: {p['fix']}" for pid, ps in failed.items() for p in ps]
        fix_msg = ("PROBLEMS (rewrite ONLY these sentences, each as a whole sentence; for MISSING items add one sentence after the most related one):\n"
                   + "\n".join(problems)
                   + f"\n\nVERIFIED FACTS (the evidence ledger - the only facts you may use):\n{ledger_text}\n\n== ARTICLE ==\n{_article_text(article)}")
        started, usage = time.monotonic(), _new_usage()
        try:
            got = await _run_tool_call(model=spec["check_model"], system=_FIX_SYSTEM, user_msg=fix_msg, tools=[_FIX_TOOL], tool_name="submit_fixes",
                                       max_tokens=8000, effort="medium", usage=usage)
            await meter.record("grade_fix", spec["check_model"], usage, started)
            for f in writer._coerce(got.get("fixes"), list):
                if not isinstance(f, dict):
                    continue
                if (f.get("replace") or "") == "" and not _whole_sentences(article, f.get("find") or ""):
                    continue  # a partial deletion would leave a fragment
                saved, before = copy.deepcopy(article), checks()
                n, done = _apply_fixes(article, [dict(f, kind="grader", severity="high", quote=f.get("find"))])
                if n and broken(before, checks()):
                    article.clear()
                    article.update(saved)  # the rewrite broke a check that passed - undo it
                    continue
                applied += n
                issues += done
        except Exception:
            await meter.record("grade_fix", spec["check_model"], usage, started, failed="grade_fix_failed")
            logger.warning("blog pipeline: grader fix failed", exc_info=True)
        # Check the failed items again - code first, then the grader on just those items.
        code2 = checks()
        again = await grade(list(failed), code2, "regrade")
        still = merge(code2, again, list(failed))
        # Only a code-confirmed rule break is cut (whole sentences only); the AI's own judgment is flagged.
        for pid in list(still):
            if pid not in bfc.CUT_IF_STILL_FAILING:
                continue
            for p in [p for p in still[pid] if p.get("code") and p["quote"]]:
                target = _cut_target(article, p["quote"])
                if not target:
                    continue
                n, cut = _apply_fixes(article, [{"kind": f"pass_{pid}", "severity": "high", "quote": p["quote"], "find": target, "replace": "",
                                                 "problem": f"{p['why']} - removed before saving."}])
                if n:
                    applied += n
                    issues += cut
                    still[pid].remove(p)
                    failed[pid].append({"cut": True})
        still = {k: v for k, v in still.items() if v}
        issues += [{"kind": f"pass_{pid}", "severity": "high" if pid in bfc.CUT_IF_STILL_FAILING else "medium", "quote": p["quote"],
                    "problem": f"{p['why']}. {p['fix']}".strip(), "fixed": False} for pid, ps in still.items() for p in ps]
    _tidy_spaces(article)
    pass_list = []
    for pid, label, rule in bfc.PASS_LIST:
        if pid in still:
            status_ = "fail"
        elif pid in failed:
            status_ = "cut" if any(p.get("cut") for p in failed[pid]) else "fixed"
        else:
            status_ = "pass"
        notes = [p.get("why") for p in (still.get(pid) or failed.get(pid) or []) if p.get("why")][:3]
        pass_list.append({"id": pid, "label": label, "rule": rule, "status": status_, "notes": notes})
    return pass_list, nice, issues, applied


def _article_text(a: dict) -> str:
    lines = [a.get("title") or "", a.get("subtitle") or "", f"SEO title: {a.get('seo_title') or ''}", f"Meta description: {a.get('meta_description') or ''}"]
    lines += [f"- {g}" for g in a.get("at_a_glance") or [] if isinstance(g, str)]
    lines += [p for p in a.get("intro") or [] if isinstance(p, str)]
    for sec in a.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        lines.append(f"\n## {sec.get('heading') or ''}")
        for b in sec.get("blocks") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "ask_agent":
                # Shown as what it is - a bare topic line read as a stray fragment and the fact-check
                # deleted it (11/01 Alexandria: "the resale certificate").
                lines.append(f"[Ask-me line: \"Ask me about {b.get('text')}\" + Call/Text/Email/Message]" if b.get("text") else "[Ask-me line: Call/Text/Email/Message]")
            elif b.get("text"):
                lines.append(("### " if b.get("type") == "subheading" else "") + b["text"])
            lines += [f"- {i}" for i in b.get("items") or [] if isinstance(i, str)]
    closing = a.get("closing") or {}
    if closing.get("heading"):
        lines.append(f"\n## {closing['heading']}")
    lines += [p for p in closing.get("paragraphs") or [] if isinstance(p, str)]
    return "\n".join(l for l in lines if l is not None)


def _apply_fixes(article: dict, issues: list[dict]) -> tuple[int, list[dict]]:
    """Apply the fact-checker's exact find/replace fixes to every text field of the article.
    Returns (applied_count, issues annotated with `fixed`)."""
    skip = {"claims", "review", "category", "cover_image_idea"}
    out = []
    applied = 0

    def walk(node, find, repl):
        hit = False
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k in skip:
                    continue
                if isinstance(v, str) and find in v:
                    node[k] = " ".join(v.replace(find, repl).split()) if repl == "" else v.replace(find, repl)
                    hit = True
                elif isinstance(v, (dict, list)):
                    hit = walk(v, find, repl) or hit
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str) and find in v:
                    node[i] = v.replace(find, repl).strip()
                    hit = True
                elif isinstance(v, (dict, list)):
                    hit = walk(v, find, repl) or hit
        return hit

    for issue in issues:
        find = (issue.get("find") or "").strip()
        repl = issue.get("replace") if isinstance(issue.get("replace"), str) else None
        fixed = bool(find) and repl is not None and find != repl and walk(article, find, repl)
        applied += int(fixed)
        out.append({**issue, "fixed": fixed})
    # Drop list items / paragraphs a deletion emptied out.
    article["intro"] = [p for p in article.get("intro") or [] if isinstance(p, str) and p.strip()]
    article["at_a_glance"] = [p for p in article.get("at_a_glance") or [] if isinstance(p, str) and p.strip()]
    for sec in article.get("sections") or []:
        for b in sec.get("blocks") or []:
            if isinstance(b, dict) and isinstance(b.get("items"), list):
                b["items"] = [i for i in b["items"] if isinstance(i, str) and i.strip()]
        # A list whose every item was cut goes too.
        if isinstance(sec.get("blocks"), list):
            sec["blocks"] = [b for b in sec["blocks"] if not (isinstance(b, dict) and isinstance(b.get("items"), list) and not b["items"] and not (b.get("text") or "").strip())]
    return applied, out


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

Progress = Optional[Callable[[str], Awaitable[None]]]


async def precheck(db: AsyncSession, user: User, engine: Optional[str] = None) -> str:
    """Run before queueing a background job, so "not enough AI usage left" is an instant answer.
    Returns the engine that will run."""
    writer._require_key()
    config = await ai_config_service.get_config(db)
    engine = engine if engine in ENGINES else config.get("blog_pipeline_default_engine", "max")
    await _gate(db, user, config, engine)
    return engine


async def run_pipeline(db: AsyncSession, user: User, brief: dict, *, engine: Optional[str] = None, persist_research: bool = True,
                       on_stage: Progress = None) -> dict:
    """Research -> write -> fact-check. Returns generate_article's shape plus `verification`.
    `on_stage(name)` is awaited as each stage starts: "gathering", "research", "write", "fact_check", "finishing"."""
    writer._require_key()
    config = await ai_config_service.get_config(db)
    engine = engine if engine in ENGINES else config.get("blog_pipeline_default_engine", "max")
    spec = config["blog_pipeline_engines"][engine]
    await _gate(db, user, config, engine)
    try:
        reservation = await reserve_global_and_emergency(
            db, "blog_pipeline_global", float(config["blog_pipeline_reservation_estimate_usd"][engine]),
            config["blog_pipeline_global_daily_cost_ceiling_usd"], config["blog_pipeline_global_monthly_cost_ceiling_usd"], config,
        )
    except AIRateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.user_message)

    meter = _Meter(db, user, config)
    t0 = time.monotonic()
    try:
        result = await _run(db, user, brief, engine, spec, config, meter, persist_research, on_stage)
    except Exception:
        await meter.refund()
        await reconcile_budget(db, reservation, meter.total)
        raise
    await reconcile_budget(db, reservation, meter.total)
    result["verification"].update({"cost_usd": round(meter.total, 4), "stages": meter.stages, "seconds": round(time.monotonic() - t0, 1),
                                   "grader_cost_usd": round(sum(s["cost_usd"] for s in meter.stages if s["stage"] in ("grade", "grade_fix", "regrade")), 4)})
    result["usage"] = {"engine": engine, "cost_usd": round(meter.total, 4), "stages": meter.stages}
    return result


async def _stage(on_stage: Progress, name: str) -> None:
    if on_stage:
        try:
            await on_stage(name)
        except Exception:  # progress is cosmetic - never fail the article over it
            logger.warning("blog pipeline: progress update failed", exc_info=True)


async def _served_areas(db: AsyncSession, user: User, limit: int = 5):
    from sqlalchemy import select
    from app.models.database import AgentMarketArea, MarketArea
    return (await db.execute(
        select(MarketArea).join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id).where(AgentMarketArea.owner_id == user.id).limit(limit)
    )).scalars().all()


async def _run(db: AsyncSession, user: User, brief: dict, engine: str, spec: dict, config: dict, meter: _Meter, persist: bool,
               on_stage: Progress = None) -> dict:
    await _stage(on_stage, "gathering")
    hub_config = await writer._hub_config(db, user.id)
    profile = writer._agent_profile(user, hub_config)
    sales = []
    if brief.get("include_recent_sales"):
        wanted = set(brief.get("sale_ids") or [])
        sales = [s for s in writer._recent_sales(hub_config, limit=300) if not wanted or s["id"] in wanted][:10]
    listings = [p for p in (brief.get("listings") or []) if isinstance(p, dict)][:6]
    state = (user.state if hasattr(user, "state") and user.state else None) or next((s.get("state") for s in sales if s.get("state")), None) or "PA"

    # --- 1. What we already know (free) ---------------------------------------------------
    areas = await tk.find_areas(db, slugs=list(brief.get("area_slugs") or []), names=list(brief.get("areas") or []), state=state)
    areas = areas[:4]
    # The article's towns decide the state, not the agent's profile: 11/01 a PA agent writing about
    # Frederick, MD would otherwise get Pennsylvania's transfer tax, programs and research.
    town_states = {(a.state or "").upper() for a in areas if a.state}
    # Towns outside every state the agent works in (their own state + their served towns'): the
    # article never claims local experience there, but keeps the agent's chosen closing - owner
    # 11/01: "I'll start your search" stays (the agent may hand it to a referral agent).
    agent_states = {(getattr(user, "state", None) or "").upper()} | {(a.state or "").upper() for a in await _served_areas(db, user, 50)}
    agent_states.discard("")
    out_of_area = sorted(town_states - agent_states) if town_states and agent_states and not (town_states & agent_states) else []
    if len(town_states) == 1:
        state = next(iter(town_states))
    # A general article ("buying a home in the Philly suburbs", "PA closing costs") names no town:
    # the agent's own served towns become REGIONAL context - a few dated market numbers and the
    # basics per town for examples - and research focuses on state/county-level facts instead.
    region_words = ("suburb", "county", "main line", "region", "area", "metro", "valley", "shore", "greater ", "philly", "philadelphia")
    named = [str(a) for a in (brief.get("areas") or [])]
    regional = not areas and (not named or all(any(w in n.lower() for w in region_words) for n in named))
    if regional:
        areas = list(await _served_areas(db, user, 5))
    # No-MLS plans: no Bright MLS numbers anywhere in the article (snapshot, overlap counts,
    # listing-based payment example) - only public research, state/federal facts and their own brief.
    from app.utils.auth import tier_has_mls
    mls_ok = tier_has_mls(user.plan_tier)
    dossiers = [await tk.town_dossier(db, a, include_bank=bool(spec.get("reuse_bank")) or regional, mls=mls_ok) for a in areas]
    if regional:
        # The dated MLS numbers come first: 11/01 the 14-item cut kept only stored school/transit
        # research (it sits ahead of the snapshot in the dossier), so a "Main Line home prices"
        # article had no median price or days on market for any town and the writer left a
        # fill-in-the-blanks box instead.
        for d in dossiers:
            mls_items = [i for i in d["items"] if i["origin"] == "mls_live"][:16]
            d["items"] = ([i for i in d["items"] if i["origin"] == "geo_verified"] + mls_items
                          + [i for i in d["items"] if i["origin"] != "mls_live" and i["topic"] in ("schools", "transit", "downtown")][:6])
    known_items = [it for d in dossiers for it in d["items"]]
    # Geography truth for every town in the article (what the geography audit checks against),
    # and the township names on record around them (what it recognizes in a sentence).
    profiles = [d["geo"] for d in dossiers if d.get("geo")]
    try:
        known_geo = await tk.known_places(db, sorted({c for p in profiles for c in p.get("counties") or []}), state)
    except Exception:
        logger.warning("blog pipeline: known places lookup failed", exc_info=True)
        known_geo = {}
    # Areas that share homes (Wayne addresses inside Radnor Township) - so counts are never summed.
    from app.services.areas import geography as geo_scope
    from app.services.areas.market_stats_service import count_shared_for_sale, entry_price_profile, older_homes_share
    pairs = []
    for i, a in enumerate(areas if mls_ok else []):
        for b in areas[i + 1:]:
            # report from the mailing-address town into the township (or the first named)
            x, y = (b, a) if (a.municipality_type or "").lower() == "township" and (b.municipality_type or "").lower() != "township" else (a, b)
            try:
                shared = await count_shared_for_sale(db, geo_scope.scope_for_market_area(x), geo_scope.scope_for_market_area(y))
            except Exception:
                logger.warning("blog pipeline: overlap count failed", exc_info=True)
                continue
            total = next(((d.get("sales_snapshot") or {}).get("active_count") for d in dossiers if d["slug"] == x.slug), None)
            if shared and total:
                pairs.append((x, y, shared, total))
    overlap = tk.overlap_items(pairs)
    federal = tk.federal_items()
    # State-wide facts (transfer tax, buyer programs, rules, latest rates) - shared by every
    # article; Premium still re-verifies the ones the topic leans on.
    try:
        state_known = await tk.state_items(db, state)
    except Exception:
        logger.warning("blog pipeline: state facts lookup failed", exc_info=True)
        state_known = []
    known_items += state_known
    try:
        rate_items = await tk.freddie_mac_rate_items()
    except Exception:
        logger.warning("blog pipeline: Freddie Mac PMMS fetch failed", exc_info=True)
        rate_items = []
    # Buying articles get a worked payment example, computed here from real listings + the rate.
    pay_ex = None
    if mls_ok and areas and not regional and wants_payment_example(brief):
        try:
            entry = await entry_price_profile(db, [geo_scope.scope_for_market_area(a) for a in areas])
            # Jumbo or not is judged against the county's own FHFA limit, never the national baseline
            # (11/01 round-11 grader, Ashburn/Loudoun). No single known limit -> the card says it depends.
            states = {(a.state or "").upper() for a in areas}
            cll = tk.county_conforming_limit(next(iter(states)), sorted({c for p in profiles for c in p.get("counties") or []})) if len(states) == 1 else None
            pay_ex = payment_example(entry, await tk.freddie_mac_latest(), [a.name for a in areas], conforming_limit=(cll or {}).get("limit"),
                                     limit_area=_and_list((cll or {}).get("counties") or []) or None, basis=payment_basis(brief))
            if pay_ex and cll:
                pay_ex["limit_source"] = {k: cll[k] for k in ("source_name", "source_url", "published")}
        except Exception:
            logger.warning("blog pipeline: payment example failed", exc_info=True)
    calc_items = payment_example_items(pay_ex) if pay_ex else []

    # --- 1b. Research what's missing (web search) ----------------------------------------
    await _stage(on_stage, "research")
    started = time.monotonic()
    usage = _new_usage()
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    town_lines = []
    for d in dossiers:
        known = [f"  - {i['claim']}" for i in d["items"] if i["origin"] != "idx_research" or i["topic"] in ("schools", "market", "transit", "downtown")][:30]
        town_lines.append(f"{d['town']}: we hold {len(d['items'])} sourced facts (topics: {', '.join(f'{k} {v}' for k, v in d['covered'].items())}). "
                          f"Gaps: {', '.join(d['gaps']) or 'none'}.\n  ALREADY KNOWN:\n" + "\n".join(known))
    unknown_towns = [] if regional else [t for t in (brief.get("areas") or []) if not any(t.split(",")[0].strip().lower() == a.name.lower() for a in areas)]
    general = ("\n\nThis article is GENERAL (not about one town) - the towns above are only the agent's service area. Research the "
               f"state, county and regional facts it needs ({state} transfer/recording taxes and how they're split, the state's "
               "first-time-buyer programs, county-level facts, statewide rules and timelines), not town trivia."
               if regional else "")
    # Nothing stored for the region and no service towns (e.g. a new agent writing about "the Philly
    # suburbs"): the research step first pins down what the region actually is, from sources.
    region_label = ", ".join(named) or profile.get("market") or ""
    if regional and not dossiers and region_label:
        general += (f"\n\nWe hold NO stored data for '{region_label}'. First establish from authoritative sources what it commonly refers "
                    "to (which counties and major municipalities), then gather a few dated, sourced basics for it: counties and their "
                    "seats, the main school districts, regional rail/transit lines, and any official county housing or market figures.")
    rates_note = ("\n\nMortgage rates are already supplied from Freddie Mac's own published survey - do NOT research mortgage rates."
                  if rate_items else "")
    # Tax research aims at the townships/districts the towns' homes really sit in (10/17 it went
    # looking for Lower Merion's millage for Wayne, which has no Lower Merion homes).
    big = lambda x: share_at_least(x, 5)
    tax_targets = sorted({t["name"] for p in profiles for t in p.get("townships") or [] if big(t)}
                         | {d["name"] for p in profiles for d in p.get("school_districts") or [] if big(d)})
    if tax_targets and not regional:
        rates_note += ("\n\nThe verified townships and school districts for these towns are: " + "; ".join(tax_targets)
                       + ". If property taxes matter to this topic, find the official page that publishes property-tax rates for exactly these (official township, county or district pages), not for other municipalities - as a source to link, since articles never quote tax rates or millage.")
    rates_note += ("\n\nFederal loan basics (conventional/FHA/VA mortgage insurance and down payment rules, the conforming loan limit, CFPB closing-cost range) are already supplied - do NOT research them.")
    # 11/01 round-10 grader: a Lancaster article linked the 2025-2026 school millage table after the
    # 2026-2027 one was out. Last year's editions are also dropped in code (tk.drop_stale_editions).
    rates_note += (f"\n\nToday is {datetime.now(timezone.utc).date().isoformat()}. For anything published by year (tax-rate or millage tables, "
                   "school-year or fiscal-year documents, program guidelines), use the NEWEST edition - check whether this year's version is "
                   "already out (school and fiscal years start July 1) and never cite an edition whose year has ended.")
    if engine == "max":
        task = ("Research this article FRESH and thoroughly: the specific question it answers, the most recent local developments "
                "(last 12 months) that matter to it, the official page publishing property-tax rates for the relevant township(s)/school district(s) (to link - articles never quote rates), current mortgage "
                "rates (Freddie Mac PMMS latest week) if buying, selling or the market is discussed, and re-verify any already-known fact the "
                "article will lean on that may have changed.")
    else:
        task = ("Fill ONLY the gaps: the official page publishing property-tax rates for the relevant township(s)/school district(s) if missing (to link, not to quote), the current 30-year "
                "mortgage rate (Freddie Mac PMMS latest week) if buying, selling or the market is discussed, and at most one fact specific "
                "to this topic that the known facts don't cover. Keep it tight.")
    research_msg = (
        f"Today is {today}. Agent: {profile['name']} ({profile['brokerage']}), market: {profile['market'] or state}.\n"
        f"ARTICLE TOPIC: {brief.get('topic', '').strip()}\n"
        + (f"Goal: {brief['goal']}\n" if brief.get("goal") else "")
        + (f"Audience: {', '.join(brief['audiences'])}\n" if brief.get("audiences") else "")
        + (f"Key points: {brief['key_points']}\n" if brief.get("key_points") else "")
        + (f"\nREGION the article covers: {', '.join(named)}" if regional and named else "")
        + (f"\n{state} STATE-WIDE FACTS ALREADY KNOWN (don't re-research unless they may have changed):\n" + "\n".join(f"  - {i['claim']} ({i.get('as_of')})" for i in state_known[:40]) + "\n" if state_known else "")
        + "\nTOWNS:\n" + ("\n\n".join(town_lines) if town_lines else "(no towns with stored research)")
        + (f"\n\nTowns with NO stored research (research their basics: municipality/township, county, school district(s), rail/transit, downtown): {', '.join(unknown_towns)}" if unknown_towns else "")
        + f"\n\nYOUR TASK: {task}{general}{rates_note}"
        + (f"\n\n{_coverage(brief, 1)}" if _coverage(brief, 1) else "")
    )
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": int(spec["research_searches"]),
              "user_location": {"type": "approximate", "region": "Pennsylvania" if state == "PA" else state, "country": "US"}},
             _EVIDENCE_TOOL]
    if spec.get("research_fetches"):
        tools.insert(1, {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": int(spec["research_fetches"])})
    research_items: list[dict] = []
    not_found: list[str] = []
    try:
        found = await _run_tool_call(model=spec["research_model"], system=_RESEARCH_SYSTEM, user_msg=research_msg, tools=tools,
                                     tool_name="submit_evidence", max_tokens=16000, effort=spec.get("research_effort"), usage=usage)
        for it in found.get("items") or []:
            if isinstance(it, dict) and it.get("claim") and it.get("source_url"):
                research_items.append({"topic": it.get("topic") or "other", "claim": it["claim"].strip(), "source_name": it.get("source_name") or tk._domain(it.get("source_url")),
                                       "source_url": it["source_url"], "as_of": it.get("as_of"), "confidence": it.get("confidence"), "town": it.get("town") or "general",
                                       "origin": "web_research"})
        not_found = [x for x in found.get("not_found") or [] if isinstance(x, str)]
        await meter.record("research", spec["research_model"], usage, started)
    except HTTPException:
        # Research is an enrichment - the article can still be written from what we hold.
        await meter.record("research", spec["research_model"], usage, started, failed="research_incomplete")
        logger.warning("blog pipeline: research stage returned nothing", exc_info=True)
    except Exception:
        await meter.record("research", spec["research_model"], usage, started, failed="research_error")
        logger.warning("blog pipeline: research stage failed", exc_info=True)

    # Nothing that contradicts the verified geography is used - or banked for the next article.
    research_items, geo_scrubbed = tk.scrub_geography(research_items, profiles, known_geo)
    research_items = tk.drop_stale_editions(tk.drop_other_county_items(research_items, profiles))
    banked = 0
    if persist and research_items:
        town_names = {a.name.lower() for a in areas}
        state_level = [i for i in research_items if (i.get("town") or "general").split(",")[0].strip().lower() not in town_names]
        try:
            banked += await tk.save_state_items(db, state, state_level)
        except Exception:
            logger.warning("blog pipeline: state facts save failed", exc_info=True)
        for a in areas:
            mine = [i for i in research_items if (i.get("town") or "").split(",")[0].strip().lower() == a.name.lower()]
            try:
                banked += await tk.save_to_bank(db, a, mine)
            except Exception:
                logger.warning("blog pipeline: evidence bank save failed for %s", a.slug, exc_info=True)

    brief_facts = []
    if (brief.get("facts") or "").strip():
        brief_facts.append({"topic": "agent_brief", "claim": brief["facts"].strip(), "source_name": "The agent (brief)", "source_url": None,
                            "as_of": datetime.now(timezone.utc).date().isoformat(), "origin": "agent", "town": "general"})
    # Verified geography + MLS + agent data first so they get the lowest ids, then fresh research,
    # then stored research.
    # The state rules and programs this reader should hear about - a built-in, checked list
    # (blog_final_checks.PROTECTIONS), not whatever research happened to find.
    # Place-specific ones (Baltimore ground rent) need the article's towns and counties; the lead-paint
    # disclosure needs how old the listings are. Then every 1%+ local government is a must-name item.
    places = {tk.norm_place(a.name) for a in areas} | {tk.norm_place(c) for p in profiles for c in p.get("counties") or []}
    try:
        older = await older_homes_share(db, [geo_scope.scope_for_market_area(a) for a in areas]) if areas else None
    except Exception:
        logger.warning("blog pipeline: older-homes share failed", exc_info=True)
        older = None
    protections = (bfc.protections_for(town_states or {(state or "").upper()}, brief, investment_article(brief), places=places, older_share=older)
                   + ([] if regional else tk.place_protections(profiles)))
    ordered = ([i for i in known_items if i["origin"] == "geo_verified"] + [i for i in known_items if i["origin"] == "mls_live"] + overlap
               + rate_items + calc_items + federal + bfc.protection_items(protections, datetime.now(timezone.utc).date().isoformat())
               + _sale_items(sales) + _listing_items(listings) + brief_facts
               + research_items + [i for i in known_items if i["origin"] == "state_bank"]
               + [i for i in known_items if i["origin"] not in ("mls_live", "state_bank", "geo_verified")])
    ordered, scrubbed_more = tk.scrub_geography(ordered, profiles, known_geo)
    geo_scrubbed += scrubbed_more
    # Every item - saved state facts too, not just this run's research (11/01 Annapolis).
    ordered = tk.drop_stale_editions(tk.drop_other_county_items(ordered, profiles))
    ordered = fit_to_reader(ordered, brief)
    ledger, ledger_text = _build_ledger(ordered)
    link_urls = {it["source_url"] for it in ledger if str(it.get("source_url") or "").startswith("https://")}

    # --- 2. Write --------------------------------------------------------------------------
    spotlight: list[dict] = []
    if brief.get("area_slugs"):
        try:
            spotlight = await writer.spotlight_for_brief(db, user, list(brief["area_slugs"]))
        except Exception:
            logger.warning("blog pipeline: spotlight cards failed", exc_info=True)
    # The spotlight cards show the SAME numbers as the ledger (one snapshot, one moment) - they
    # used to be computed separately and read 89 homes where the text said 92.
    snaps = {d["slug"]: d.get("sales_snapshot") or {} for d in dossiers}
    for c in spotlight:
        # The town's stored AI blurb is unchecked marketing copy (10/24: "acclaimed dining",
        # "ideal setting for discerning families" - a fair-housing problem) - an article's card
        # carries only verified numbers, districts and places.
        c["summary"] = None
        snap = snaps.get(c.get("slug")) or {}
        if snap.get("active_count") is not None:
            c.update(active_count=snap["active_count"], median_price=snap.get("active_median_list_price"),
                     under_contract_count=snap.get("under_contract_count"), data_through=snap.get("data_through"))
    brief_msg, _ = writer._brief_text(brief, profile, [], [], None)
    brief_msg = brief_msg.split("== REAL DATA ==")[0].rstrip()
    inserted = []
    if spotlight:
        inserted.append("Town Spotlight cards for " + ", ".join(c["name"] for c in spotlight) + " (photo, homes for sale, median list price, price range, school districts), placed after the first section.")
    if pay_ex:
        inserted.append(f"A 'What it could cost' payment example card for a {writer._money(pay_ex['price'])} home (5%, 10% and 20% down; the numbers are the 'Payment example' ledger items), placed after the section about mortgages, budgets or costs - point to it in one sentence; don't redo its math.")
    if writer._video(brief):
        inserted.append("The agent's video, embedded in the section that mentions it.")
    inserted.append("A 'Sources & data notes' list at the end with every source and what each area's numbers cover.")
    if brief.get("area_slugs"):
        inserted.append("Links from each town's first mention to the agent's neighborhood guide for it (homes for sale, prices, schools).")
    write_msg = (
        f"{brief_msg}\n\nToday's date: {today}.\n"
        + "\nINSERTED ELEMENTS (added around your text automatically):\n" + "\n".join(f"- {x}" for x in inserted) + "\n"
        + (f"\nWHAT THIS READER EXPECTS: {_coverage(brief, 2)}\n" if _coverage(brief, 2) else "")
        + ("\nPROTECTIONS THIS READER MUST HEAR ABOUT (each is in the ledger - name every one in plain words where it fits, "
           "no rates, amounts, limits or deadlines, never say the reader qualifies, and follow it with an ask_agent line):\n"
           + "\n".join(f"- {p['name']}" for p in protections) + "\n" if protections else "")
        + (f"\nOUTSIDE THE AGENT'S AREA: the agent works in {', '.join(sorted(agent_states))}; this article's towns are in {', '.join(out_of_area)}. "
           "Never claim to live, work or have sold homes there, and never invent local experience. Keep the closing the agent chose "
           "(a showing request, starting their home search, a home value, a question) in your own first-person voice (\"I'll start your search\").\n" if out_of_area else "")
        + ("This is a REGIONAL article, not about one town. The town items in the ledger are the agent's service area - use a few as concrete, dated examples where they genuinely help the reader; never force town names in or imply one town represents the region.\n" if regional else "")
        + f"\n== EVIDENCE LEDGER ({len(ledger)} items - the only facts you may use; cite ids in `claims`) ==\n{ledger_text}"
        + (f"\n\nResearch could not verify (do not state these): {'; '.join(not_found[:8])}" if not_found else "")
    )
    await _stage(on_stage, "write")
    started = time.monotonic()
    usage = _new_usage()
    try:
        article = await _run_tool_call(model=spec["writer_model"], system=_writer_system(state), user_msg=write_msg, tools=[_WRITER_TOOL],
                                       tool_name=_WRITER_TOOL["name"], max_tokens=32000, effort=spec.get("writer_effort"), usage=usage)
    except Exception as exc:
        await meter.record("write", spec["writer_model"], usage, started, failed="write_failed")
        if isinstance(exc, HTTPException):
            raise
        logger.exception("blog pipeline: write stage failed")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI writer didn't respond - try again in a moment.")
    await meter.record("write", spec["writer_model"], usage, started)
    article = writer._normalize_article(article)
    if not article.get("title") or not article.get("sections"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI writer returned an incomplete draft - please try again.")
    writer_review = writer._coerce(article.get("review"), dict)
    writer_claims = [c for c in writer._coerce(article.get("claims"), list) if isinstance(c, dict)]

    # --- 3. Fact-check ------------------------------------------------------------------------
    draft_text = _article_text(article)
    geo_first = tk.geography_audit(draft_text, profiles, known_geo)
    loan_first = loan_rule_lint(draft_text)
    automated = ([f"wrong_geography: \"{f['sentence']}\" puts {f['town']} in {f['place']} ({f['kind']}) - verified: {f['truth']}." for f in geo_first]
                 + [f"overgeneralization: \"{s}\" states a mortgage-insurance / down-payment rule without saying which loan type it applies to." for s in loan_first]
                 + [f"{h['kind']}: \"{h['sentence']}\" - {h['fix']}" for h in bfc.final_lint(draft_text, brief)]
                 + [f"missing_protection: the article never names {p['name']} - add one plain sentence from the ledger: \"{p['says']}\""
                    for p in bfc.missing_protections(draft_text, protections)])
    check_msg = (
        f"Agent: {profile['name']} - {profile['brokerage']} - {profile['market'] or state}. Today's date: {today}.\n"
        f"Agent's brief:\n{brief_msg}\n\nINSERTED ELEMENTS (real parts of the article, added around the text):\n" + "\n".join(f"- {x}" for x in inserted)
        + ("\n\nAUTOMATED CHECKS (confirmed problems - each needs a fix):\n" + "\n".join(f"- {x}" for x in automated) if automated else "")
        + f"\n\n== EVIDENCE LEDGER ==\n{ledger_text}\n\n== ARTICLE ==\n{draft_text}"
    )
    await _stage(on_stage, "fact_check")
    started = time.monotonic()
    usage = _new_usage()
    review: dict = {}
    try:
        review = await _run_tool_call(model=spec["check_model"], system=_REVIEW_SYSTEM, user_msg=check_msg, tools=[_REVIEW_TOOL],
                                      tool_name="submit_review", max_tokens=24000, effort=spec.get("check_effort"), usage=usage)
        await meter.record("fact_check", spec["check_model"], usage, started)
    except Exception:
        await meter.record("fact_check", spec["check_model"], usage, started, failed="fact_check_failed")
        logger.warning("blog pipeline: fact-check failed - saving with the unchecked report", exc_info=True)
    await _stage(on_stage, "finishing")
    # The "[Ask-me line: ...]" marker in _article_text is a real element - never a placeholder to report
    # (11/01 round 6: the fact-check listed both of Lancaster's Ask lines as unfinished text).
    issues = [i for i in writer._coerce(review.get("issues"), list) if isinstance(i, dict) and not str(i.get("quote") or "").lstrip().startswith("[Ask-me line")]
    applied, issues = _apply_fixes(article, issues)

    # --- 3b. Re-check what can be checked without a model; one targeted repair if needed ----------
    geo_left = tk.geography_audit(_article_text(article), profiles, known_geo)
    loan_left = loan_rule_lint(_article_text(article))
    hard_left = bfc.final_lint(_article_text(article), brief)
    missing = bfc.missing_protections(_article_text(article), protections)
    repaired = 0
    if geo_left or loan_left or hard_left or missing:
        geo_facts = "\n".join(f"- {i['claim']}" for i in ledger if i.get("origin") == "geo_verified")
        loan_facts = "\n".join(f"- {i['claim']}" for i in ledger if i.get("origin") == "federal" or "mortgage insurance" in (i.get("claim") or "").lower())
        problems = ([f"- \"{f['sentence']}\": puts {f['town']} in {f['place']} ({f['kind']}), but {f['truth']}." for f in geo_left]
                    + [f"- \"{s}\": states a mortgage-insurance / down-payment rule as universal; name the loan type it applies to." for s in loan_left]
                    + [f"- \"{h['sentence']}\": {h['fix']}" for h in hard_left]
                    + [f"- MISSING: the article never names {p['name']}. Add this sentence (reworded to fit, same voice) after the most related "
                       f"sentence: \"{p['says']}\"" for p in missing])
        fix_msg = (f"PROBLEMS:\n" + "\n".join(problems) + f"\n\nVERIFIED GEOGRAPHY:\n{geo_facts}\n\nVERIFIED LOAN FACTS:\n{loan_facts}"
                   + (f"\n\nVERIFIED PROTECTIONS:\n" + "\n".join(f"- {p['says']}" for p in missing) if missing else "")
                   + f"\n\n== ARTICLE ==\n{_article_text(article)}")
        started = time.monotonic()
        usage = _new_usage()
        try:
            got = await _run_tool_call(model=spec["check_model"], system=_FIX_SYSTEM, user_msg=fix_msg, tools=[_FIX_TOOL], tool_name="submit_fixes",
                                       max_tokens=6000, effort="medium", usage=usage)
            await meter.record("repair", spec["check_model"], usage, started)
            def _kind(find: str) -> str:
                touches = lambda s: s in find or find in s
                if any(touches(g["sentence"]) for g in geo_left):
                    return "wrong_geography"
                if any(touches(s) for s in loan_left):
                    return "overgeneralization"
                return next((h["kind"] for h in hard_left if touches(h["sentence"])), "missing_protection")
            fixes = [dict(f, kind=_kind(f.get("find") or ""), severity="high", quote=f.get("find"))
                     for f in writer._coerce(got.get("fixes"), list) if isinstance(f, dict)]
            repaired, fixes = _apply_fixes(article, fixes)
            issues += fixes
            applied += repaired
        except Exception:
            await meter.record("repair", spec["check_model"], usage, started, failed="repair_failed")
            logger.warning("blog pipeline: repair step failed", exc_info=True)
        geo_left = tk.geography_audit(_article_text(article), profiles, known_geo)
        loan_left = loan_rule_lint(_article_text(article))
        hard_left = bfc.final_lint(_article_text(article), brief)
        missing = bfc.missing_protections(_article_text(article), protections)
    # A hard-rule sentence the repair didn't fix is cut - it never reaches the published article.
    if hard_left:
        # A bullet reads "- item" in the article text but is stored without the dash (11/01 Lancaster).
        cuts = [{"kind": h["kind"], "severity": "high", "quote": h["sentence"], "find": _cut_target(article, h["sentence"]) or h["sentence"], "replace": "",
                 "problem": h["fix"].split(". ")[0] + " - removed before saving."} for h in hard_left]
        cut, cuts = _apply_fixes(article, cuts)
        issues += cuts
        applied += cut
        hard_left = [h for h, c in zip(hard_left, cuts) if not c.get("fixed")]
    # Anything still wrong is reported to the agent as a high-severity open issue.
    still_open = ([{"kind": "wrong_geography", "severity": "high", "quote": f["sentence"], "problem": f"Puts {f['town']} in {f['place']} - {f['truth']}.", "fixed": False} for f in geo_left]
                  + [{"kind": "overgeneralization", "severity": "high", "quote": s, "problem": "A loan rule stated without its loan type.", "fixed": False} for s in loan_left]
                  + [{"kind": h["kind"], "severity": "high", "quote": h["sentence"], "problem": h["fix"], "fixed": False} for h in hard_left]
                  + [{"kind": "missing_protection", "severity": "medium", "quote": p["name"], "problem": f"Not mentioned: {p['says']}", "fixed": False} for p in missing])
    issues += still_open
    # --- 3c. The engine's own grader: the fixed pass list, one targeted rewrite, re-checked ---------
    pass_list, nice_to_have, graded, regraded = await _grade_and_fix(
        article=article, ledger_text=ledger_text, brief=brief, brief_msg=brief_msg,
        inserted=inserted + [f"Closing button: \"{writer.cta_block_args(brief)[0]}\" (opens the agent's form)"],
        protections=protections, agent_name=profile["name"], spec=spec, meter=meter,
        geo_check=lambda text: tk.geography_audit(text, profiles, known_geo))
    issues += graded
    applied += regraded
    # Last line of defense: nothing fill-in-the-blank reaches the published article, and no section
    # is left empty (the fact-check's own deletions can empty one).
    placeholders = writer.strip_placeholders(article)
    if placeholders:
        logger.warning("blog pipeline: removed %d placeholder line(s) from the draft", len(placeholders))
        issues += [{"kind": "placeholder", "severity": "medium", "quote": t, "problem": "Unfinished fill-in-the-blank text - removed before saving.",
                    "replace": "", "fixed": True} for t in placeholders]
        applied += len(placeholders)

    # --- Assemble (same block layout as the single-call writer) --------------------------------
    blocks = writer.article_to_blocks(article, *writer.cta_block_args(brief), links=link_urls)
    if spotlight:
        heads = [i for i, b in enumerate(blocks) if b["type"] == "heading"]
        at = heads[1] if len(heads) > 1 else (heads[0] if heads else min(2, len(blocks)))
        blocks[at:at] = [writer.area_spotlight_block(c) for c in spotlight]
    if pay_ex:
        blocks = place_payment_example(blocks, {**pay_ex, "cta_form": writer.cta_block_args(brief)[1],
                                                "closing_note": _CLOSING_NOTES.get((state or "").upper())})
    if listings:
        cards = [{"id": writer._bid("property_card"), "type": "property_card", "data": {"property": p, "displaySize": "large"}} for p in listings]
        closing_idx = max((i for i, b in enumerate(blocks) if b["type"] == "heading"), default=len(blocks))
        blocks[closing_idx:closing_idx] = cards
    blocks = writer.place_video(blocks, brief)
    full_text = "\n".join([article.get("title", ""), article.get("subtitle", ""), article.get("meta_description", ""), writer.blocks_to_text(blocks)])
    source_text = ledger_text + "\n" + brief_msg

    by_eid = {it["eid"]: it for it in ledger}
    claims = [c for c in writer._coerce(review.get("claims"), list) if isinstance(c, dict)]
    cited = {e for c in (claims or writer_claims) for e in (c.get("evidence_ids") or []) if e in by_eid}
    sources, seen_src = [], set()
    for eid in sorted(cited, key=lambda e: int(e[1:])):
        it = by_eid[eid]
        key = it.get("source_url") or it.get("source_name")
        if key and key not in seen_src:
            seen_src.add(key)
            sources.append({"name": it.get("source_name"), "url": it.get("source_url"), "as_of": it.get("as_of"), "topic": it.get("topic")})
    # Sources & data notes block (public): the primary sources the article cites, the MLS window,
    # and what each area's numbers cover - so a reader (or reviewer) can check every figure.
    notes = [d["definition"] for d in dossiers if d.get("definition") and not regional]
    # Readers get the plain fact; the "never add these together" instruction is for the writer only.
    notes += [re.sub(r",? so the areas' counts must never be added together\.?$", ".", i["claim"].replace("Overlap: ", "")) for i in overlap]
    if any(not (d.get("sales_snapshot") or {}).get("prior_year_reliable", True) for d in dossiers):
        notes.append("Year-ago comparisons are left out where our Bright MLS history for that window is incomplete.")
    if pay_ex:
        notes.append(f"Payment example: principal and interest at Freddie Mac's 30-year average ({pay_ex['rate']:.2f}%, week of {pay_ex['rate_as_of']}); "
                     "property tax estimated from tax bills on similar-priced listings; homeowners insurance, HOA fees and mortgage insurance not included.")
    mls_through = max((d["sales_snapshot"].get("data_through") for d in dossiers if (d.get("sales_snapshot") or {}).get("data_through")), default=None)
    public_sources = tk.drop_stale_editions(tk.drop_other_county_items([s for s in sources if s.get("url") and s.get("topic") != "agent_brief"], profiles))
    if public_sources or notes:
        block = {"id": writer._bid("sources"), "type": "sources", "data": {
            "items": [{"name": s["name"], "url": s["url"], "as_of": _public_date(s.get("as_of"))} for s in public_sources[:14]],
            "mls": (f"Bright MLS via OpenHousePal - closed sales through {mls_through}; homes for sale as of {datetime.now(timezone.utc).date().isoformat()}." if mls_through else None),
            "notes": notes[:8],
        }}
        at = next((i for i, b in enumerate(blocks) if b["type"] in ("cta", "agent_profile")), len(blocks))
        blocks[at:at] = [block]

    verdicts = [c.get("verdict") for c in claims]
    open_issues = [i for i in issues if not i["fixed"] and i.get("severity") in ("high", "medium")]
    data_dates = [d["sales_snapshot"].get("data_through") for d in dossiers if (d.get("sales_snapshot") or {}).get("data_through")]
    category = article.get("category") if article.get("category") in writer.BLOG_CATEGORIES else (brief.get("category_hint") or None)

    verification = {
        "engine": engine,
        "models": {"research": spec["research_model"], "write": spec["writer_model"], "fact_check": spec["check_model"]},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checked": bool(review),
        "claims_checked": len(claims),
        "claims_supported": verdicts.count("supported") + verdicts.count("analysis"),
        "claims_partly": verdicts.count("partly_supported"),
        "claims_unsupported": verdicts.count("unsupported") + verdicts.count("stale"),
        "issues_found": len(issues),
        "issues_fixed": applied,
        "open_issues": [{k: i.get(k) for k in ("kind", "severity", "quote", "problem")} for i in open_issues][:12],
        "fixed_issues": [{k: i.get(k) for k in ("kind", "severity", "quote", "problem", "replace")} for i in issues if i["fixed"]][:20],
        "sources": sources[:30],
        "source_count": len(sources),
        "evidence_count": len(ledger),
        "research_found": len(research_items),
        "research_not_found": not_found[:8],
        "research_banked": banked,
        "towns": [{"town": d["town"], "facts": len(d["items"]), "gaps": d["gaps"]} for d in dossiers],
        "regional": regional,
        "geography_checked": bool(profiles),
        "geography": [{"town": p["town"], "townships": [t["name"] for t in p.get("townships") or []],
                       "school_districts": [d["name"] for d in p.get("school_districts") or []]} for p in profiles],
        "geography_flags_found": len(geo_first) + len(loan_first),
        "geography_flags_open": len(geo_left) + len(loan_left),
        "research_geography_removed": geo_scrubbed,
        "payment_example": bool(pay_ex),
        "placeholders_removed": placeholders[:8],
        "data_notes": notes[:8],
        "data_as_of": max(data_dates) if data_dates else datetime.now(timezone.utc).date().isoformat(),
        "scores": review.get("scores") or {},
        "summary": review.get("summary") or "",
        "improvements": [x for x in (writer._coerce(review.get("improvements"), list) + writer._coerce(writer_review.get("improvements"), list)) if isinstance(x, str)][:8],
        "seo": {"search_intent": writer_review.get("search_intent"), "primary_keyword": writer_review.get("primary_keyword") or article.get("focus_keyword"),
                "secondary_keywords": writer._coerce(writer_review.get("secondary_keywords"), list)[:8]},
        "agent_claims": [x for x in writer._coerce(writer_review.get("agent_specific_claims"), list) if isinstance(x, str)][:8],
        "image_ideas": [x for x in writer._coerce(writer_review.get("image_ideas"), list) if isinstance(x, dict)][:5],
        "numbers_to_verify": writer.number_audit(full_text, source_text),
        # Laws, programs and deadlines the article names - the agent checks each against its official
        # page before publishing (the article never spells out their terms).
        "out_of_area": out_of_area,
        # The engine's own grade against the fixed pass list (pass / fixed / cut / fail per item).
        "pass_list": pass_list, "nice_to_have": nice_to_have,
        "rules_to_check": [{"name": str(r.get("name"))[:120], "source": r.get("official_source") or None}
                           for r in writer._coerce(review.get("rules_mentioned"), list) if isinstance(r, dict) and r.get("name")][:8],
    }
    return {
        "title": (article.get("title") or "").strip()[:200],
        "subtitle": (article.get("subtitle") or "").strip()[:300],
        "seo_title": (article.get("seo_title") or "").strip()[:70],
        "meta_description": (article.get("meta_description") or "").strip()[:320],
        "focus_keyword": (article.get("focus_keyword") or "").strip()[:80],
        "category": category,
        "tags": [t.strip()[:40] for t in (article.get("tags") or []) if isinstance(t, str) and t.strip()][:8],
        "blocks": blocks,
        "linked_properties": listings,
        "cover_image_idea": (article.get("cover_image_idea") or "").strip(),
        "facts_used": [{"fact": c.get("text", "")[:300], "source": ", ".join(c.get("evidence_ids") or [])} for c in writer_claims][:30],
        "numbers_to_verify": verification["numbers_to_verify"],
        "verification": verification,
    }


# ---------------------------------------------------------------------------
# State facts pack - the state-wide facts most articles lean on, researched once into
# shared_knowledge ("state:XX") so every agent's article reuses them free. Platform-paid (not on
# any agent's allowance); run when a state's pack is empty or stale.
# ---------------------------------------------------------------------------

_STATE_PACK_TASK = {
    "PA": ("Research and verify CURRENT Pennsylvania-wide facts a real estate blog needs, each from the primary source where possible: "
           "(1) Pennsylvania realty transfer tax - the 1% state rate, typical local (municipal + school district) rates in the Philadelphia "
           "suburbs, Philadelphia's own rate, and how buyer/seller customarily split it; "
           "(2) PHFA programs for buyers (Keystone Home Loan, Keystone Advantage / down payment and closing cost assistance) with current limits; "
           "(3) the Pennsylvania Real Estate Seller Disclosure Law (what sellers must disclose, the Seller's Property Disclosure Statement); "
           "(4) Pennsylvania settlement basics (title insurance is customary, who typically pays what); "
           "(6) property tax basics in Montgomery, Delaware and Chester counties (millage, reassessment status) with sources; "
           "(7) Pennsylvania homestead/farmstead exclusion basics (who applies, the March 1 deadline, whether it stays in place once approved, what changes each year); "
           "(8) the Pennsylvania Property Tax/Rent Rebate Program for older homeowners (who qualifies, income limit, maximum rebate, deadline); "
           "(9) post-settlement occupancy (rent-back) in the standard Pennsylvania Agreement of Sale. Use the town value 'general' for every item."),
}


_STATE_PACK_TASK.update({
    "NJ": ("Research and verify CURRENT New Jersey-wide facts a real estate blog needs, from primary sources where possible: "
           "(1) the New Jersey Realty Transfer Fee and the graduated fee on sales over $1M (both the seller's responsibility since the 2025 amendment - the former buyer-paid 'mansion tax'); "
           "(2) NJHMFA first-time homebuyer programs (First-Time Homebuyer Mortgage, Down Payment Assistance) with current limits; "
           "(3) attorney review - the three-business-day attorney review period for contracts prepared by licensees; "
           "(4) New Jersey seller disclosure requirements; "
           "(5) property tax basics (NJ has among the highest effective rates - cite the official source), and the ANCHOR / Stay NJ property tax relief programs; "
           "(6) settlement customs (title insurance, who typically pays what). Use the town value 'general' for every item."),
    "DE": ("Research and verify CURRENT Delaware-wide facts a real estate blog needs, from primary sources where possible: "
           "(1) Delaware realty transfer tax - the state rate, typical county/municipal rates, how buyer/seller customarily split it, and the first-time buyer reduction; "
           "(2) DSHA homebuyer programs (e.g. Welcome Home, first-time buyer assistance) with current limits; "
           "(3) Delaware Seller's Disclosure requirements; (4) attorney-conducted settlements in Delaware; "
           "(5) property tax basics for New Castle, Kent and Sussex counties (assessment / reassessment status); "
           "(6) settlement customs. Use the town value 'general' for every item."),
    "MD": ("Research and verify CURRENT Maryland-wide facts a real estate blog needs, from primary sources where possible: "
           "(1) Maryland state transfer tax and recordation tax, typical county transfer/recordation taxes (Montgomery, Baltimore County, Anne Arundel, Howard, Prince George's), "
           "how buyer/seller customarily split them, and the first-time buyer exemption from the state transfer tax share; "
           "(2) Maryland Mortgage Program (MMP) first-time buyer programs and down payment assistance with current limits; "
           "(3) the Maryland Residential Property Disclosure and Disclaimer Statement; (4) the Homestead Tax Credit; "
           "(5) property tax basics (state + county rates, triennial assessments); (6) settlement customs. Use the town value 'general' for every item."),
    "VA": ("Research and verify CURRENT Virginia-wide facts a real estate blog needs, from primary sources where possible: "
           "(1) Virginia recordation tax (state and local), the grantor's tax paid by the seller, and the regional congestion relief fee in Northern Virginia; "
           "(2) Virginia Housing first-time homebuyer programs and down payment assistance grants with current limits; "
           "(3) the Virginia Residential Property Disclosure Act (buyer-beware notice); "
           "(4) property tax basics for Fairfax, Loudoun, Arlington and Prince William counties; "
           "(5) settlement customs (settlement agents, title insurance). Use the town value 'general' for every item."),
    "DC": ("Research and verify CURRENT District of Columbia facts a real estate blog needs, from primary sources where possible: "
           "(1) DC deed recordation tax and transfer tax rates (and the higher rate tier), and how buyer/seller customarily split them; "
           "(2) DC HPAP (Home Purchase Assistance Program) and DC Open Doors / DCHFA programs with current limits; "
           "(3) the DC Seller's Disclosure Statement requirement; (4) DC TOPA basics as they affect buyers of tenant-occupied homes; "
           "(5) DC residential property tax rate and the Homestead Deduction; (6) settlement customs. Use the town value 'general' for every item."),
    "WV": ("Research and verify CURRENT West Virginia-wide facts a real estate blog needs, from primary sources where possible: "
           "(1) West Virginia real estate excise (transfer) tax - the state rate and the county add-on, and who customarily pays; "
           "(2) WVHDF homebuyer programs (Homeownership Program, down payment assistance) with current limits; "
           "(3) West Virginia seller disclosure practice; (4) property tax basics (classes, assessment at 60%), Homestead Exemption; "
           "(5) settlement customs (attorney-conducted closings). Use the town value 'general' for every item."),
})


async def seed_state_facts(db: AsyncSession, state: str = "PA", *, model: str = "claude-sonnet-5-5", searches: int = 14, fetches: int = 6) -> dict:
    task = _STATE_PACK_TASK.get(state.upper())
    if not task:
        return {"added": 0, "error": "no pack defined for this state"}
    usage = _new_usage()
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": searches},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": fetches}, _EVIDENCE_TOOL]
    found = await _run_tool_call(model=model, system=_RESEARCH_SYSTEM, user_msg=f"Today is {today}.\n\nYOUR TASK: {task}", tools=tools,
                                 tool_name="submit_evidence", max_tokens=24000, effort="medium", usage=usage)
    items = [{"topic": i.get("topic") or "other", "claim": i["claim"].strip(), "source_name": i.get("source_name"), "source_url": i.get("source_url"),
              "as_of": i.get("as_of"), "confidence": i.get("confidence")} for i in (found.get("items") or []) if isinstance(i, dict) and i.get("claim") and i.get("source_url")]
    added = await tk.save_state_items(db, state, items)
    config = await ai_config_service.get_config(db)
    cost = _cost(config, model, usage)
    logger.info("state facts pack seeded", extra={"state": state, "added": added, "cost_usd": cost, "searches": usage["web_search_requests"]})
    return {"added": added, "found": len(items), "not_found": found.get("not_found") or [], "cost_usd": round(cost, 4), "usage": usage}
