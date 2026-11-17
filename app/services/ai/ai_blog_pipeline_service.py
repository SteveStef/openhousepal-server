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

import contextvars
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
OVERNIGHT_TOKEN_MULTIPLIER = 0.5

# How a model request is sent. None = straight away (streamed). The overnight writer (blog_overnight_service) sets
# its own sender here for one run: the same requests, through the half-price batch queue.
TRANSPORT: contextvars.ContextVar = contextvars.ContextVar("blog_pipeline_transport", default=None)

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
A share that comes from only part of the sales or listings (how buyers paid, listings that show a fee) always carries its count ("53% of the 88 sales that recorded how the buyer paid") - never as a share of the whole market. Name the towns before referring to them as a group ("these areas", "these towns"). A number that looks out of line with its neighbors gets its reason in the same sentence when the ledger gives it (a quarter-price far below the median sale price: say how much of that quarter is condos or townhomes). Compare towns only on the same footing: tax bills from different tax years go in separate sentences, each with its year - never ranked against each other.

RULES, PROGRAMS AND DEADLINES - NAME THEM, NEVER SPELL THEM OUT
Laws and contract rights, review or cancellation periods, disclosure rules, assistance programs (grants or loans), tax breaks, exemptions, credits and rebates, loan limits, insurance requirements - anything with eligibility conditions - change often, their conditions matter more than their headline number, and nobody re-checks a published article. So name it in one plain sentence and say what it does in general words ("Frederick County has down payment help for first-time buyers", "Delaware has a school-tax credit for some homeowners 65 and older", "Virginia law gives condo buyers a short window to review the association's documents and cancel"), say that conditions apply, link the official page when the ledger has it, and send the reader to the agent. NEVER state its dollar amounts, benefit percentages, income or age cut-offs, residency periods, day counts, deadlines, repayment terms or loan-limit figures - even when the ledger has them. Never put WHO qualifies in your own words either ("for buyers who are the first in their family", "if your parents never owned a home") - say it's for qualified buyers and to ask a participating lender or the agent whether they meet the requirements (11/01 Cherry Hill misstated NJHMFA's First Generation program). Right after that sentence add an `ask_agent` block whose text names the topic in 2-6 words ("down payment help", "the senior school-tax credit") - the page shows a small "Ask [agent] about ..." line with call, text and email under it. At most 3 ask_agent blocks, never two in a row, none in the closing. NOT rules (state these normally, dated): the ledger's market numbers (prices, days on market, homes for sale, fees, what sold), this week's dated mortgage rate, and plain explanations of how something works (what a rent-back or a reserve study is). One more exception (11/14): a municipality's own lead time for applying for a resale or occupancy certificate may be given exactly as its ledger item states it, as what that municipality currently asks, with its page linked.
Taxes and fees at closing (transfer, grantor, recordation, regional or "mansion" fees): name them and who customarily pays them, never their rates or amounts and never which office collects them or when (local procedure - 11/01 Frederick misnamed the collecting office) - places stack several charges and a partial list misleads (11/01: an article listed two of three Northern Virginia charges). The one exception (11/13): when the request has a TRANSFER TAX YOU MAY STATE section, those figures - and only those - may be given exactly as listed, each worded as an estimate ("about", "currently"), with the official page linked and a line that the settlement company gives the exact amount; keep every caveat that section gives (a local part that varies, reduced rates for some sellers). When a ledger item gives a dividing line - a date, a program generation, a property or buyer type on each side of which the rule differs - state both sides and tell the reader to find out which one applies to them; never give one side as the rule for everyone. Jumbo loans: never say in general that a home or loan "is", "will be" or "would be" jumbo - it depends on the amount borrowed and the county's conforming loan limit; the payment example card marks each down payment, so point to it or say "may be a jumbo loan depending on how much you borrow" (11/01 Ashburn called a loan jumbo at 20% down when it wasn't). Property taxes: a dated median tax bill in dollars is fine; never a property tax as a percent of price or value (it reads like an official rate) - say taxes vary by municipality and to check the home's actual tax bill and assessment. Tell the reader to ask the settlement company or attorney for an itemized estimate of what they'll pay or net.
A one-level home means one floor - never imply step-free entry or an elevator unless the ledger says so. Association records (board minutes, reserve study, budget): tell the reader to ask for them early - never promise they come with the resale package.

WHETHER A RULE APPLIES TO THE READER - NEVER DECIDE IT FOR THEM
Never state flatly that a law, tax, exemption, credit, disclosure duty or eligibility rule does or doesn't apply ("the seller must disclose...", "it doesn't carry over to a new owner", "you'll owe...", "you qualify") when the answer depends on the reader's situation - who is selling, how the title is held, an estate or trust, age, income, residency, the loan type. Explain the general idea, say that exceptions apply - and when the ledger has an exception that fits THIS article's situation (an estate sale, a foreclosure, a condo, a manufactured home, a first-time buyer, an older homeowner), name it in general words ("sales handled by an estate's personal representative can follow different disclosure rules") - then point to the right professional or office: an estate attorney, a CPA, the county tax office, the lender, the settlement company. You help with the real estate side; never offer legal or tax advice yourself.
YOUR OWN VOICE when you send readers to yourself: always first person, the way you'd say it to a client ("These rules change often and have exceptions, so I'd confirm this with an estate attorney - and I'm glad to talk through the selling side with you", "reach out and I'll walk you through it"). Never refer to yourself in the third person or by name ("ask Peter", "contact your agent", "reach out to the agent", "your Realtor can...").

EVERYTHING MUST FIT THIS READER
Before you mention any program, tax break, loan rule, payment figure or step, check its basic requirement against this article's reader: will they live in the home or rent it out (investment) or use it as a second home; buying or selling; first-time or not; owner, heir or co-owner. Leave out anything whose basic requirement clashes with the reader - "conditions apply" never rescues a program the reader plainly can't use (a first-time-buyer or homestead benefit in an article about buying a rental). Investment and second-home purchases: say plainly that loans usually need a larger down payment and carry different rates and terms than a home you live in, and send readers to a lender - no down-payment percentages or payment figures. Rents: keep them general ("check what similar rentals actually lease for") - no rent figures.
Places are never a complete list: say which municipalities and school districts serve "homes with this address in our records include ...", never that a list covers every address, and never that everything outside a city is unincorporated.

EVIDENCE - THE ONLY SOURCE OF FACTS
A share of homes "within about 0.6 miles ... measured in a straight line" is a DISTANCE: write it that way, with "straight line" in the sentence. Never turn it into a walking time ("a 15-minute walk"), "walking distance" or "walkable" - the walk can be much longer than the line.

The request contains an EVIDENCE LEDGER: numbered items [E1], [E2]... each with its source and date. Every statistic, price, count, date, rate, tax figure, school or district statement, named place, business, park or station, and every statement about market conditions must come from a ledger item or from the agent's own brief. Never use a second-hand figure (a news site "citing" Freddie Mac) when the primary figure is in the ledger, and never talk about your own sourcing in the article ("I haven't confirmed...", "my research shows", "as the 2020 version of the code reads; current law may differ") - cite a law by its name and what it says; if the ledger's copy of a rule may be out of date, tell the reader to confirm the current rule with their attorney or settlement company, without saying which copy you read. If a point would benefit from a fact that is not in the ledger, leave it out, make the point qualitatively, or tell the reader what to verify - never invent, estimate or "round into" a figure. Never turn an assumption into a fact. In `claims`, list every factual sentence with the ledger ids it rests on.

CONFIDENCE
Bright MLS / agent data, government, school-district, Census and Freddie Mac items are VERIFIED - state them plainly. Third-party estimates (Zillow, Redfin, Realtor.com, news, directories) are LIKELY - attribute them ("Zillow's June 2026 estimate puts..."). Anything weaker is UNVERIFIED - leave it out. Trip planners (Rome2rio, Moovit) are never a source for travel times or fares - use the transit agency's item or leave the number out.

DATES
Anything that changes over time (prices, inventory, days on market, rates, taxes, programs, rules) carries its date or window from the ledger: "In the 90 days ending September 9, 2026..." / "Freddie Mac's survey for the week of September 25, 2026...". Never make stale information sound current. Prefer numbers to adjectives, but one sale or one month never proves a trend - use transactions as examples, not universal evidence.

FACT VS INTERPRETATION
State facts plainly; label analysis as analysis ("that limited supply appears to be one factor supporting prices"). No causation the evidence doesn't show. Market labels ("seller's market", "bidding wars are common", "prices are rising", "inventory is extremely low", "homes sell immediately") only when the ledger numbers show it - and then show the numbers. The same goes for what a season does and how buyers or sellers behave ("winter buyers are more serious", "spring is the busiest time to list", "buyers here expect...", "most sellers..."): state it only from ledger numbers, or - when the brief's personal take says it - as the agent's own view ("in my experience"); otherwise leave it out. A common-sense point with no number behind it is written as a possibility ("some buyers...", "it can..."), never as how the market works.

LOCAL GEOGRAPHY
Mailing address / postal town, municipality or township, county, school district and ZIP code are different things and often don't line up - one postal town can span several townships and school districts. Which township, county or school district a town's homes sit in comes ONLY from the ledger's `geography` items (verified from Census boundaries and the MLS records) - name exactly the townships/districts they list for that town, never one they don't, and never generalize one town's split to another ("Villanova and Wayne both lie in..." is wrong unless the geography items say so for each). Never assume a ZIP equals a municipality or that a town has one school district; where location matters, say it depends on the specific address. Say a split in plain words a reader can use ("most Villanova addresses are in Lower Merion Township; the rest are in Radnor Township") - a rough share is fine, sample sizes and record counts are not. Mention only towns and places that are relevant and in the ledger - never sprinkle town names for SEO.

MARKET NUMBERS
Each area's `Definition -` item says what its numbers cover (a township, or a mailing address that spans several townships). When the article uses more than one area's numbers, say in one plain sentence what each covers. Where an `Overlap:` item says areas share homes, never add their counts together or call them separate markets - and tell the reader plainly ("14 of Wayne's listings are inside Radnor Township, so they're counted in both lines"), never as an instruction ("never add these together"). "For sale" means active listings; homes under contract or pending are a separate number - never call them for sale, and when inventory matters mention both (that share already under contract is part of the story). A small-sample item stays qualified. Leave out year-ago comparisons the ledger says are unreliable.

LOANS, PROGRAMS AND ABSOLUTE STATEMENTS
Rules differ by loan type: mortgage insurance, minimum down payment and credit rules for conventional, FHA, VA, USDA and state-agency (e.g. PHFA) loans are not the same. Always name the loan type a rule applies to ("on a conventional loan with less than 20% down, lenders generally require PMI"), and mention when a common alternative differs (VA: no monthly mortgage insurance; FHA: mortgage insurance at any down payment) if the ledger has it. Words like must, always, never, required, all, every, only need a ledger item that says exactly that for the same scope - otherwise qualify them ("generally", "on most conventional loans") or name the scope.

CONTACT DETAILS AND OTHER PEOPLE'S SMALL PRINT - LEAVE THEM ON THEIR PAGE
Never print a phone number, an email address, or the street address, room number or opening hours of an office, inspector, authority or company: they change, and they are not what the article is for (owner 11/15). Say whose page has them and link that page. The same goes for a train, bus or ferry timetable: say about how long the ride takes and about how often it runs ("about 40 minutes to Suburban Station on weekday morning trains, which run every 20-40 minutes"), with the timetable's date and its link - never a departure or arrival clock time ("the 7:48 AM train", "arrives at 8:28", ":51 past the hour"), even when the ledger gives one: one misread row sends a commuter to the wrong train, and the times change with every new timetable. When you say what an inspection looks at, give a few items as examples ("items such as ...") and link the full checklist - never equipment specifications (which kind of alarm, battery, wiring or size): the checklist states them exactly and a partial version sends a seller to buy the wrong thing. A law, contract right or protection keeps EVERY limit of its ledger wording (which contracts, which homes, who it covers): never widen it to every sale.

THE TITLE'S PROMISE
If the title names a season, a year or a situation ("this winter"), at least one short passage must speak to what is different about it for this reader, as your own practical advice in general words (for a winter sale: short daylight for photos and showings, clear walks and steps, the holiday weeks) - no statistic the ledger doesn't hold, and never a claim that homes sell better or worse then unless the ledger says so.

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
The request lists cards the system inserts into the article (Town Spotlight cards, a payment example, the neighborhood-guide links, a Sources list). They are real - you may point to them in one short sentence ("the payment example below"), but never repeat their numbers in full.

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
    # Grader 11/14: the same Ardmore brief ("Getting to Center City by train") got the real timetable in one run and a
    # Google Maps door-to-door estimate in the other - whichever the research happened to open.
    (re.compile(r"\btrain\b|\brail\b|commut|\bstation\b|septa|nj transit|\bmarc\b|\bvre\b|metro\b|\btransit\b", re.I),
     "This article is asked about GETTING AROUND BY TRAIN or transit. From the transit agency's OWN pages only: the line and the station "
     "that serve the town; the scheduled trip from that station to the main downtown station - open the line's current timetable and "
     "report ABOUT HOW LONG the ride takes on weekday morning trains, in minutes (a range across two or three trains: \"about 38-45 "
     "minutes\"), about how often they run, and the weekend pattern, with the timetable's effective date. Read each time from THAT "
     "station's own row, and do NOT report departure or arrival clock times (\"the 7:48 AM\"): one row off gives a commuter the wrong "
     "train, and the times change with every timetable (grader 11/15: Rosemont's 7:48 was reported as Bryn Mawr's). Also the "
     "fare for that trip from the agency's fare page. A map's door-to-door estimate is not a train time. Budget: one search to find "
     "the timetable, one page read of it, and one look-up for the fare - three in all, never the same page twice - so the article's "
     "other points still get their look-ups.",
     "A reader who asked about the train expects: the station and the line, about how long the scheduled ride to downtown takes and about "
     "how often trains run (with the timetable's date and its link), and where to check fares - from the agency's own pages in the ledger. "
     "Never a departure or arrival clock time, even when the ledger holds one. If the ledger holds only a map's door-to-door estimate, say plainly "
     "that it is an estimate from the center of town and not the train's timetable, and point to the agency's schedule."),
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


# Grader audit 11/13: a Cherry Hill "selling in winter" article - about paperwork, closing and timing -
# never mentioned the township's resale Certificate of Continued Occupancy or its smoke / carbon-monoxide
# certificate; a Fishtown article described a tax abatement ending but not that it steps down each year,
# or that it can't be combined with the Homestead Exemption. Town rules are researched, not hoped for.
# 11/14: a Fishtown BUYING article ("how quickly homes are selling") was taken for a selling one - research spent
# look-ups on resale certificates and the checker demanded a seller's caveat. "Selling" alone isn't a seller.
# 11/14 engine audit: who the article is for is decided once, in blog_final_checks.reader_sides - the audience the
# agent picked first, the words only as the fallback.
_SELLER_ARTICLE = bfc.SELLER_ARTICLE


def _for_sellers(brief: dict) -> bool:
    return "seller" in bfc.reader_sides(brief)


_COVERAGE = _COVERAGE + (
    (_for_sellers,
     "This is a SELLING article - LOCAL SALE REQUIREMENTS ARE REQUIRED RESEARCH. On the OFFICIAL website of each municipality "
     "(township, borough or city) the article's towns sit in, find what the municipality itself requires before a home can be resold: "
     "a resale / use-and-occupancy / certificate of (continued) occupancy inspection or certificate; a smoke-detector, carbon-monoxide "
     "or fire-extinguisher compliance certificate; a sewer-lateral, sidewalk or zoning certification. A municipality that needs NO "
     "occupancy certificate can still have a sale requirement run by a SEPARATE body - most often its sewer or water authority (a sewer "
     "certification or lateral inspection before any sale, transfer or refinancing): look for that authority's own page too, and never "
     "report a municipality as having no resale requirement on the strength of one FAQ line about occupancy certificates. Report EACH requirement as its own "
     "'regulation' item from the municipality's own page: what it is, that it is required before settlement, who applies, which homes "
     "the page says are exempt (condos, townhomes, new construction), and how far ahead of settlement the municipality says to apply - "
     "in the page's own words (\"at least three weeks before settlement\") when it gives one. "
     "If a municipality's own site shows no such requirement, or you can't find its page, add 'no resale certificate requirement found "
     "for <municipality>' to not_found - never guess. Also find, from the state's official pages, the seller's disclosure duty and who "
     "customarily pays the state and local realty transfer tax or fee.",
     "A seller MUST be told about every municipal resale requirement in the ledger (a resale or continued-occupancy certificate, a "
     "smoke / carbon-monoxide certificate and the like): name each one, say plainly it has to be done before settlement and to apply "
     "early, say which homes the ledger says are exempt, link the municipality's page, and add an ask_agent line. A municipal step like "
     "this is not fine print to keep vague - name it - but give no fees. The ONLY timings you may state are the municipality's own lead times for its sale certificate, when its ledger item gives them - "
     "and state EVERY one that item gives (file the application 20 days before settlement AND call to schedule the inspection 15 days "
     "before: leaving one out sends a seller in late). Say each as what that municipality currently asks (\"Upper Providence currently "
     "asks for the application at least three weeks before settlement\"), with its page linked, and say what KIND of time it is in the "
     "item's own words: a filing deadline, a scheduling step, or how long the work takes (\"the certification takes at least two weeks, so "
     "apply well before that\") - never write a processing time as a filing deadline. Where an item says a lead time or fee is \"not yet confirmed\", give NO number for it: "
     "name the requirement, say it has to be started well before settlement, link the municipality's page for the current timing and fee, and "
     "offer to confirm it for the seller's address - a number we have read only once is not stated (owner 11/15) - a seller planning a move needs it "
     "(owner 11/14; grader 11/15). Where the item says the municipality's own official pages give it DIFFERENTLY, add one plain sentence "
     "that its own pages give different figures (name the two, e.g. its web page and its code), so the current one has to be confirmed "
     "with the municipality - still no number from either (grader round 11). "
     "No other day counts, and never a lead time the ledger doesn't give. If the ledger has NO such item for a town the "
     "article covers, say in one sentence that many municipalities require a resale inspection or a smoke-detector certificate before "
     "settlement and that the seller should confirm with the municipality early - never say or imply that none is required. When the "
     "town's homes sit in several municipalities and the ledger has a requirement for only some of them, name the municipality each "
     "requirement belongs to and say that a seller whose home is in any other municipality should confirm that municipality's own "
     "rules early - never present one municipality's rule as the whole town's. State each requirement as the municipality's general "
     "rule and say that its code has exceptions (a recent certificate, a newly installed system and the like) the seller should ask "
     "about - never as applying to every home. Commission: say it is negotiable and set in the "
     "listing agreement, and that you'll go over yours with the seller before they sign - never a rate, and never \"I don't have a "
     "figure\" (it is your own fee). Group the "
     "seller's costs by kind: required by law (transfer tax or fee), negotiated (commissions, concessions), and payoffs and adjustments."),
    (re.compile(r"abatement|abated", re.I),
     "This article involves a PROPERTY-TAX ABATEMENT. From the municipality's official abatement page: whether the abated share stays "
     "full for the whole term or STEPS DOWN each year (and which application dates each schedule applies to), which kinds of work or "
     "construction each program covers, and whether an abated home can also receive the municipality's homestead exemption.",
     "A reader weighing an abated home needs, in general words with no percentages or dollar amounts: whether the tax bill rises "
     "in steps DURING the abatement or all at once when it ends - and when the ledger gives TWO schedules split by a date, BOTH of them "
     "with that dividing line (the year is the point, not fine print: an abatement from before the change behaves differently from one "
     "after it, and both are on homes for sale) - that the listing's own abatement must therefore be confirmed, and - when the ledger "
     "says so - that an abated home generally can't also take the homestead exemption. Add an ask_agent line."),
)

# The municipal resale requirements a seller article has to name when the ledger holds them (two families).
_LOCAL_SALE_REQUIREMENTS = (
    ("a resale / occupancy certificate", re.compile(
        r"certificate of (?:continued |re-?)?occupancy|use[- ]and[- ]occupancy|\bU\s?&\s?O\b|resale (?:inspection|certificate|certification)|"
        r"continued occupancy|occupancy (?:inspection|certificate|permit)", re.I)),
    ("a smoke / carbon-monoxide certificate", re.compile(
        r"smoke[- ](?:detector|alarm)|smoke (?:and|&|/) ?carbon|carbon[- ]monoxide", re.I)),
)
_MUNICIPAL_CAVEAT = re.compile(r"municipalit\w+|township|borough|city hall|code office", re.I)


def local_sale_requirement_checks(text: str, ledger: list[dict], brief: dict) -> list[str]:
    """AUTOMATED CHECKS for a selling article: every municipal resale requirement the ledger holds is
    named, and when it holds none the article still tells the seller to confirm with the municipality."""
    if not _for_sellers(brief):
        return []
    plain = bfc._plain(text or "")
    out, found_any = [], False
    for label, rx in _LOCAL_SALE_REQUIREMENTS:
        items = [it for it in ledger or [] if it.get("topic") == "regulation" and it.get("origin") != "protection"
                 and rx.search(str(it.get("claim") or ""))]
        if not items:
            continue
        found_any = True
        if not rx.search(plain):
            it = items[0]
            out.append(f"missing_local_requirement: the article never tells the seller about {label} - add one plain sentence from the ledger "
                       f"(name it, say it must be done before settlement and to apply early - with the municipality's own lead time if the item gives one - and no fees): \"{it['claim']}\""
                       + (f" Link {it['source_url']}." if it.get("source_url") else ""))
    if not found_any and not (any(rx.search(plain) for _l, rx in _LOCAL_SALE_REQUIREMENTS) and _MUNICIPAL_CAVEAT.search(plain)):
        out.append("missing_local_requirement: the article never tells the seller that many municipalities require a resale inspection or a "
                   "smoke-detector certificate before settlement - add one sentence saying so and to confirm with the municipality early "
                   "(do not say none is required).")
    return out


_ABATEMENT_MUST = (
    ("that there are TWO schedules for new construction, split by when the abatement was APPLIED FOR (on or before December 31, 2021, "
     "or after) - newer abatements step the tax bill up each year, older ones stay in full and end all at once - and that the buyer "
     "must find out which one the listing has",
     re.compile(r"(?=.*(?:declin|step|each year|every year|year by year|shrink|phase|gradual))(?=.*(?:2021|2022))(?=.*appli)"
                r"(?=.*(?:all at once|in full|full(?:y)? (?:exempt|abated|abatement)|whole (?:term|ten)|entire (?:term|ten)|until it ends|at the end))", re.I | re.S),
     re.compile(r"TWO SCHEDULES", re.I)),
    ("that an abated home can't also get the Homestead Exemption until the abatement ends",
     re.compile(r"homestead", re.I), re.compile(r"Homestead Exemption", re.I)),
    ("that the listing's own abatement program and dates must be confirmed (new-construction and rehab programs differ)",
     re.compile(r"rehab|renovat|improvement|which (?:program|abatement)|program", re.I), re.compile(r"separate abatement programs", re.I)),
)


_ABATEMENT_WRONG_BASIS = re.compile(
    r"permit\w*[^.]{0,80}\b202[12]\b|\b202[12]\b[^.]{0,60}\bpermit|year (?:the|an|its) abatement (?:started|began|begins|starts)"
    r"|\b(?:built|constructed|completed|sold)\b[^.]{0,40}\b(?:by|before|after|in) (?:the end of )?202[12]\b[^.]{0,80}(?:schedule|version|program|abatement)", re.I)


def abatement_checks(text: str, ledger: list[dict]) -> list[str]:
    """AUTOMATED CHECKS when the ledger holds the City's own abatement facts: the article's abatement
    passage must carry each of them (grader audit 11/13, findings 7 and 8)."""
    held = [it for it in ledger or [] if it.get("origin") == "verified_local" and "ABATEMENT" in str(it.get("claim") or "")]
    if not held:
        return []
    sentences = bfc._sentences(bfc._plain(text or ""))
    passage = " ".join(s for s in sentences if re.search(r"abate", s, re.I)) + " "
    # the sentences right around an abatement mention count too (the Homestead line rarely repeats the word)
    plain = bfc._plain(text or "")
    out = []
    for label, said, which in _ABATEMENT_MUST:
        it = next((i for i in held if which.search(i["claim"])), None)
        if it and not (said.search(passage) if "Homestead" not in label else said.search(plain)):
            out.append(f"missing_abatement_fact: the article talks about the abatement but never says {label} - add it in plain words from the "
                       f"ledger, with no percentages or dollar amounts: \"{it['claim']}\" Link {it['source_url']}.")
    # The right two schedules on the wrong date is still wrong for some homes (11/14: "projects permitted by the end of
    # 2021", "the year the abatement started decides").
    first = next((i for i in held if "TWO SCHEDULES" in i["claim"]), None)
    # ("it is the application date, not the permit date" names a permit to rule it out - that one is right)
    wrong = next((s for s in sentences if _ABATEMENT_WRONG_BASIS.search(s) and not re.search(r"\bnot\b|n't\b|rather than", s, re.I)), None) if first else None
    if wrong:
        out.append("missing_abatement_fact: the article ties the two schedules to a permit date or to the year the abatement started "
                   f"(\"{wrong.strip()}\") - add or correct it in plain words from the ledger: the schedule depends on when the abatement "
                   f"was APPLIED FOR (on or before December 31, 2021, or after), which a listing's dates don't show. Link {first['source_url']}.")
    return out


_NOW_WORDS = re.compile(r"\bright now\b|\bcurrently\b|\bat the moment\b|\bthis week\b|\blatest (?:numbers|figures|data|sales)\b|\btoday'?s market\b", re.I)
_MLS_NUMBER = re.compile(r"\d[\d,]*\s+(?:homes?|sales?|listings?|townhomes?|condos?|days?)\b|\$\s?\d[\d,.]*\s?[KkMm]?\b|median", re.I)


def stale_wording_checks(text: str, freshness: Optional[dict]) -> list[str]:
    """AUTOMATED CHECKS when our MLS data is more than a week old: no sentence may put "right now" /
    "currently" / "latest" on an MLS number (grader audit 11/13, finding 2)."""
    if not freshness or freshness.get("fresh_wording", True):
        return []
    out = []
    for s in bfc._sentences(bfc._plain(text or "")):
        if _NOW_WORDS.search(s) and _MLS_NUMBER.search(s):
            out.append(f"stale_data: \"{s.strip()}\" calls MLS numbers current, but our MLS data was last updated "
                       f"{freshness.get('current_through_label')} - give the date or window from the ledger instead of 'right now' / 'currently' / 'latest'.")
    return out[:6]


def research_list(value) -> list:
    """A list from the research tool's answer, whether it came as a list or as a JSON-encoded string
    of one (a bare sentence becomes a one-item list) - never a string walked character by character."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return [value] if value.strip() else []
    # ...or as a list holding ONE string that is itself the encoded list (11/14 Ardmore: the whole "could not
    # verify" list reached the writer as a single run-on item).
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str) and value[0].lstrip().startswith("["):
        try:
            inner = json.loads(value[0])
            if isinstance(inner, list):
                return inner
        except (ValueError, TypeError):
            pass
    return value if isinstance(value, list) else []


_HOW_COUNTED = (
    (re.compile(r"\d+-minute walk|straight line|\bmiles? of (?:a |the )?(?:train|trolley|rail|grocery|park|playground|restaurant|caf)", re.I),
     "Distance figures (\"within about 0.6 miles\"): an estimate from a sample of homes on record in the area and OpenStreetMap's map of "
     "shops, parks and stations, measured in a straight line from the home - not a walking time; the walk is often longer."),
    (re.compile(r"minutes (?:by car|to )|\bminutes\b[^.]{0,60}(?:Google|drive)|Google Maps", re.I),
     "Drive times: Google Maps estimates measured from the center of the town's map area for the departure time shown; a specific home's trip will differ."),
    (re.compile(r"\bacres?\b|lot size|square[- ]f(?:oo|ee)t lot", re.I),
     "Lot sizes: single-family houses sold in the dates shown that have a lot size on record (townhomes and condos are not counted); "
     "a municipality is listed only with 5 or more such sales."),
    (re.compile(r"Census|residents\b|population", re.I),
     "Population and household figures: the U.S. Census Bureau's American Community Survey 5-year estimates for the place named (data.census.gov)."),
    (re.compile(r"typical sales|price range", re.I),
     "The Town card's two-year range is the middle 90% of homes sold in the last two years (the lowest and highest 5% are left out)."),
)


def how_counted_notes(text: str, pay_ex: Optional[dict] = None, town_card: bool = False) -> list[str]:
    """Plain 'how we counted' lines for the measured facts this article actually uses - so a reader or reviewer
    can see what a walk share, a drive time or a lot size is made of (11/14 outside grader: the numbers were
    right but their samples and methods were invisible)."""
    plain = bfc._plain(text or "")
    out = [note for rx, note in _HOW_COUNTED[:4] if rx.search(plain)]
    # The drive-time note is for a drive. "Trains take about 48 minutes to 30th Street" matched it (11/16, Wilmington)
    # and the article carried a Google Maps note with no drive time in it.
    drive = _HOW_COUNTED[1]
    if drive[1] in out and not any(drive[0].search(x) and (re.search(r"\bdriv|\bby car\b|Google", x, re.I) or not _TRANSIT_WORD.search(x))
                                   for x in _SENTENCES.split(plain)):
        out.remove(drive[1])
    if town_card:
        out.append(_HOW_COUNTED[4][1])
    return out


_COVERAGE = _COVERAGE + (
    # Grader 11/14 + the 11/15 same-brief rerun: a first-time-buyer brief asking about "help with the down payment"
    # named Fannie Mae's HomeReady in one article and left it out of the next two. The fact is in every ledger
    # (tk.federal_items) - which low-down-payment routes got a mention was the writer's pick that day. Nothing to
    # research: these are already supplied.
    (re.compile(r"first[- ]time|first home|starter home|down[- ]?payment", re.I),
     "",
     "A reader buying a first home, or asking about the down payment, expects EVERY low-down-payment route the ledger holds and that "
     "fits this reader, each named in one plain sentence: the state housing agency's program, FHA, VA (for those eligible) and Fannie "
     "Mae's HomeReady (a low-down-payment conventional loan with income limits - not only for first-time buyers). General words only, "
     "nothing the ledger doesn't give, and the next step is asking a lender which ones they qualify for. Leaving one out makes the list "
     "look complete when it isn't. Where the ledger says a program may not be available at the payment example's price (an FHA county "
     "limit, a state program's price limit), say that in the same place you name the programs - a reader must not be sent to ask about "
     "a loan the example's own price rules out without being told."),
)


_COVERAGE = _COVERAGE + (
    # Grader round 12 (11/16), Wilmington DE: a relocation article comparing Delaware's taxes and living costs with
    # Pennsylvania's never said Delaware has no sales tax, never named the City of Wilmington's wage tax (the page was
    # found on a shared code site and could not be tied to the city's county), and named no neighborhood although the
    # title and the first key point asked for them. Moving somewhere is its own kind of article, like downsizing.
    (re.compile(r"\bmoving to\b|\bmove to\b|relocat|cost of living|living costs?|\bsales tax|income tax|wage tax|taxes? compare|\bvs\.? .{0,40}\btax", re.I),
     "This is a MOVING / RELOCATION or tax-comparison article. Only for what the topic, goal or key points actually ask about and ALREADY "
     "KNOWN does not answer - one look-up each, official pages only, with the destination's COUNTY in the search: (1) when taxes or living "
     "costs are asked about and the reader is coming from, or comparing with, ANOTHER STATE: the destination state's revenue or tax "
     "agency - whether the state has a general sales tax at all, and what kind of personal income tax it has (flat or graduated); "
     "(2) when taxes or living costs are asked about: the destination municipality's OWN website - whether it levies its own wage, "
     "earned-income or local income tax and who pays it (residents, people who work there, or both), reporting the municipality, county "
     "and state the page shows; (3) when the topic or key points ask about neighborhoods or areas: the names of the place's "
     "neighborhoods or districts from the city's, county's or its planning or tourism agency's own page, with one plain fact each that "
     "the page gives (where it is, the kind of homes or streets, a park, a main street, a transit stop). Never who lives there.",
     "A reader moving in from another state expects, in plain words with NO rates or amounts: whether the new state has a general sales tax; "
     "the kind of state income tax it has next to the one they pay now; any city or local wage or earned-income tax the ledger holds and "
     "who pays it - it can differ between a home inside the city line and one just outside it; and, when neighborhoods were asked for, the "
     "neighborhoods the ledger names, each with the plain fact the ledger gives - a place and what is there, never who lives there or who "
     "it suits. Researched public facts about a place are not the agent's own services: an agent who is not licensed in the state still "
     "gives them, and offers the introduction for the home search itself. If the ledger holds none of these, say which one you could not "
     "confirm rather than passing over it."),
)


def _coverage(brief: dict, part: int) -> str:
    """The research (part 1) or writer (part 2) checklist for this kind of article (11/01: a downsizing
    article skipped rent-backs, one-floor living, 55+ communities and senior tax relief)."""
    said = " ".join(str(brief.get(k) or "") for k in ("topic", "goal", "key_points")) + " " + " ".join(brief.get("audiences") or [])
    return "\n".join(c[part] for c in _COVERAGE if c[part] and (c[0](brief) if callable(c[0]) else c[0].search(said)))


def _writer_system(state: Optional[str]) -> str:
    return _ENGINE_RULES.replace("{state_rules}", _STATE_RULES.get((state or "").upper(), _STATE_DEFAULT))


# Writer output = the existing article tool + per-claim citations + the agent's quality report.
_WRITER_TOOL = json.loads(json.dumps(writer._ARTICLE_TOOL))
_WRITER_TOOL["input_schema"]["properties"].pop("facts_used", None)
_WRITER_TOOL["input_schema"]["required"] = [r for r in _WRITER_TOOL["input_schema"]["required"] if r != "facts_used"] + ["claims", "review"]
_WRITER_TOOL["input_schema"]["properties"]["claims"] = {
    "type": "array",
    "description": "Every factual sentence or phrase in the article and the ledger ids it rests on - one entry per factual sentence, "
                   "none skipped.",
    "items": {"type": "object", "properties": {
        # Cost audit 11/14: the writer re-typed every factual sentence here in full - a second copy of most of the
        # article at the most expensive rate. The opening words find the sentence; code restores the rest
        # (expand_quotes).
        "text": {"type": "string", "description": "ONLY the first six words of that sentence, exactly as written in the article - "
                                                  "never the whole sentence."},
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
            "jurisdiction": {"type": "string", "description": "For any fact from a township, borough, city or municipal-authority page (or a code site's page for one): the municipality, COUNTY and state as that PAGE itself shows them - its header, footer or office address - e.g. 'Upper Providence Township, Delaware County, PA'. For a fact that is not from a municipal page, write 'n/a'. A municipal fact without its county is not used."},
            "as_of": {"type": "string", "description": "Date or period the fact describes, e.g. '2026-09-25' or 'FY 2026-27'."},
            "confidence": {"type": "number", "description": "0-1."},
            "note": {"type": "string", "description": "Optional researcher's caveat (never part of the claim, never shown to the writer as fact)."},
        }, "required": ["town", "topic", "claim", "source_name", "source_url", "jurisdiction", "as_of", "confidence"]}},
        "not_found": {"type": "array", "items": {"type": "string"}, "description": "What you looked for and could not verify."},
    }, "required": ["items", "not_found"]},
}

_RESEARCH_SYSTEM = """You are the research desk for a licensed real estate agent's blog. Your job is to find CURRENT, VERIFIABLE facts the article needs - not to write anything.

Rules:
- Use the web_search tool. Prefer, in order: county/municipal government, school district websites, state/federal government (Census), Freddie Mac / Federal Reserve, Bright MLS / brokerage research, established real estate data providers, reputable local journalism. Never rely on random blogs, SEO listicles or AI-generated pages when a primary source exists.
- Every item must be a fact you actually read on the page you cite, with its number, geography and date window. If two sources disagree, report the more authoritative one and say so in the claim.
- A claim states ONLY what that page says. Your own reasoning, advice, caveats or context ("so confirm each property's municipality", "Villanova and Wayne lie partly in...") never goes in a claim - put caveats in `note`. Never state which township, county or school district a town lies in unless the cited page says exactly that for that town; the TOWNS section already gives the verified split.
- Train, bus and ferry times and fares come only from the transit agency's own schedule or fare pages (NJ Transit, SEPTA, MARC, VRE, WMATA, DART, Amtrak for Amtrak trains) - never trip planners or aggregators (Rome2rio, Moovit, Wanderu); name the line and the stations. Report about how long the ride takes (minutes, read from that station's OWN row of the timetable) and about how often it runs, with the timetable's effective date - not departure or arrival clock times: the article does not print them.
- Never report a passing status as a fact: a parking lot that is "full" or has a waiting list today, a service alert, a suspension "until further notice", a sale or closure "this week". Report what the page gives as standing (the lot has 109 spaces; permits are required) with the page's date, or leave it out.
- For every law, tax rule, credit, exemption or disclosure duty the article will lean on, also look for (a) exceptions that fit the article's situation - an estate or trust sale, a foreclosure, a condo, a manufactured home, a first-time buyer, an older homeowner - and (b) changes enacted or taking effect in the last two years. Report each exception or change as its own item from the official source (e.g. 'Maryland's seller disclosure law does not apply to sales by a personal representative administering an estate').
- For every program, credit or tax break you report, say its basic eligibility condition in the claim when the source gives it (who it's for, whether the buyer must live in the home as a principal residence) - the writer uses that to leave out programs that don't fit the reader.
- Never cite a political party, caucus or campaign site (a "House Democrats" or "Senate Republicans" page) - find the same fact on the government agency's own page or a news report.
- Laws and regulations come from the CURRENT official code or agency page (the state legislature's code site, e.g. law.lis.virginia.gov, legis.delaware.gov, mgaleg.maryland.gov, njleg.state.nj.us, legis.state.pa.us) - never an archived or dated copy (a 2020 code edition, a law firm's old summary); cite the section and say if it was renumbered or replaced.
- A rule, tax, fee, abatement or program often applies differently by the date of an application or permit, by program generation, by property type or by who the buyer is. When the source shows such a dividing line, report BOTH sides in the same claim ("applications through 2021: ...; 2022 and later: ...") - never one side as the whole rule (11/14: a Philadelphia abatement's newer step-down schedule was reported as the rule for every abated home).
- Keep geography exact: postal town vs township/municipality vs county vs school district vs ZIP. If a fact is for a township or county rather than the town, say which.
- Loan and program rules: keep the loan type and any conditions with the rule (e.g. "on PHFA's conventional Keystone Home Loan, ..."), never a bare "buyers must...".
- Schools: districts, boundaries, budgets, millage, official policies - never rankings or opinions unless attributed to a named source.
- Never research or report anything about the demographics, religion, ethnicity or "type" of residents, or crime.
- A page that sets out a procedure usually carries more than one deadline, fee or step ("mail the application and the check at least 20 days before settlement" AND "call to schedule the inspection at least 15 days before settlement"). Report EVERY deadline, lead time and fee that page gives, each with what it is for - one of two deadlines reported as the procedure is a wrong item (grader 11/15, Media Borough).
- WHOSE PAGE IS IT? Many municipalities share a name inside one state (Pennsylvania has an Upper Providence Township in Delaware County and another in Montgomery County; several Middletown, Springfield and Newtown Townships). Put the COUNTY in every search for a township, borough, city or authority ("Upper Providence Township Delaware County sewer certification"; "City of Wilmington New Castle County wage tax"). Before you report anything from a municipal page, find on the page itself - its header, footer or office address - which county it is in, and give it in `jurisdiction`. If the page belongs to a same-named municipality in another county, do NOT report it: add "found only <name>, <other county>'s page" to not_found (grader 11/15: a Montgomery County township's sewer page was used for the Delaware County township of the same name).
- The deadline and the fee for a certificate are usually on the APPLICATION FORM or instruction sheet, not on the page that links to it. When a page says a certificate or inspection is required and points to a form, open the form (a page read) and report what it says: how far before settlement to apply, how long it takes, and the fee (grader 11/15: Marple Township's resale application says "at least two (2) weeks prior to settlement" and "$200" - we had read only the page above it).
- Say what KIND of time a page gives, in its own words: a deadline for filing ("submit the application at least 20 days before settlement"), a step to schedule ("call 15 days before"), or how long the work takes ("certification takes a minimum of two weeks"). Never turn one kind into another.
- Do NOT report phone numbers, email addresses, street addresses, room numbers or office hours: report what the page REQUIRES and the page's address - the article sends readers to the page for contact details (owner 11/15).
- Use the look-ups you were given. Never stop while a REQUIRED LOOK-UP is still unchecked and you have a search left: "not checked" may go in not_found only once your searches are used up.
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
            "quote": {"type": "string", "description": "For a supported or analysis claim: ONLY the first six words of its sentence, exactly as "
                                                       "written. For any other verdict: the whole sentence, exactly as written."},
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

_REVIEW_TOOL["input_schema"]["properties"]["brief_points"] = {
    "type": "array",
    "description": "Every question or point the agent's brief raises (topic, goal, each key point) and whether the article answers it.",
    "items": {"type": "object", "properties": {
        "point": {"type": "string", "description": "The question or point, in a few words."},
        "status": {"type": "string", "enum": ["answered", "partly", "said_no_data", "not_answered"]},
        "note": {"type": "string", "description": "One sentence: where the ledger has the answer (ids), or that the ledger has nothing on it."},
    }, "required": ["point", "status", "note"]},
}
_REVIEW_TOOL["input_schema"]["required"].append("brief_points")

_REVIEW_SYSTEM = """You are the fact-check and compliance editor for a licensed real estate agent's blog - strict, specific and fair. You receive the EVIDENCE LEDGER the writer was allowed to use, the agent's brief, and the finished ARTICLE.

1. In `claims`, list every factual claim (numbers, dates, places, businesses, schools/districts, market statements, rates, taxes, statements about the agent's experience) - one entry per claim, none skipped - with the ledger ids that support it and a verdict. To keep the list short, a claim whose verdict is supported or analysis is named by ONLY the first six words of its sentence, exactly as written; a claim with any other verdict is quoted as its whole sentence, exactly. The verdicts: supported (the ledger says it - same number, date window and geography), partly_supported (right idea but a detail is off, unattributed or undated), unsupported (not in the ledger or brief), stale (the ledger date is too old to state as current), analysis (clearly labeled interpretation - fine).
2. In `issues`, report every problem: unsupported_claim, wrong_number, stale_data, wrong_geography (check EVERY statement about which township, county or school district a town is in against that town's `geography` ledger items - a township or district they don't list is wrong even if another ledger item says it; ZIP vs township confusion; assuming one district; one town's split applied to another), market_definition (areas' counts added together or treated as separate when an Overlap item says they share homes; homes under contract or pending called "for sale"; an area's numbers described as something other than its Definition item; a small sample stated without its caveat; a year-ago comparison the ledger marks unreliable), overgeneralization (a rule stated for everyone that the ledger gives for one loan type, program or place, or for one side of a dividing line such as an application date or program generation - "buyers with under 20% down must get mortgage insurance" is conventional-only; must/always/never/required/all without a ledger item saying exactly that), fair_housing (protected classes, steering, "family", "safe", "perfect for..."), school_language (any subjective school/district wording), invented_experience (agent involvement, buyer/seller side, clients, years or volume not in the brief - but an opinion or observation the agent wrote in the brief, framed as theirs ("In my experience, ..."), is supported by the brief and must stay framed as theirs; flag it only when it is stated as a general fact), causation (unsupported cause-and-effect; also a flat statement about what a season does to the market or how buyers or sellers behave - "winter buyers are more serious", "spring is the busiest time" - with no ledger numbers behind it and not from the agent's own take in the brief: soften it to a possibility or delete it), legal_tax (definitive legal/tax advice, advising to waive contingencies - but a sentence that only explains that some buyers consider changing inspection or contingency terms and that the risks should be understood first is allowed and stays), generic_ai_phrasing (filler, hype, cliches like nestled/vibrant/dream home/hidden gem/navigating), repetition, applicability (a flat statement that a law, tax, exemption, credit, disclosure duty or eligibility rule does or doesn't apply - "the seller must disclose", "it doesn't carry over", "you'll owe" - when it depends on the reader's situation, or one that ignores an exception the ledger gives for THIS article's situation: rewrite as the general idea + 'exceptions apply' (naming the one that fits, in general words) + the professional to confirm with, in the agent's first-person voice), rule_detail (a law, contract right, deadline, program, tax break, exemption, credit, rebate or loan limit stated WITH its amounts, benefit percentages, income or age cut-offs, residency periods, day counts, deadlines, repayment terms or loan-limit figures - the article may only name it and what it does in general words: replace the detail with one sentence that names it, says conditions apply and sends the reader to the agent; keep any "Ask" line under it; a municipality's own lead time for applying for a resale or occupancy certificate, exactly as its ledger item gives it and worded as what the municipality currently asks, is allowed; who customarily pays a closing tax and a dated mortgage rate are allowed, but a transfer, grantor or recordation tax rate or amount is fine print too - EXCEPT the figures in a ledger item that begins "TRANSFER TAX", "Transfer tax example" or "Realty Transfer Fee example": the article may give exactly those, worded as an estimate. Flag such a sentence only when a figure differs from the item, is stated as exact or guaranteed, or drops or contradicts a caveat the item gives), wrong_reader (a program, tax break, loan rule or calculation whose basic requirement doesn't fit this article's reader - a first-time-buyer or homestead benefit, or a 5% down payment, in an article for investors buying a rental they won't live in; a buyer program in a seller article: delete it with ""), placeholder (any blank, bracketed template text, "$___", "(area)", "(date)", "[insert ...]", "TBD" or note to the agent - the article is published to the public as-is: fill it from the ledger or delete it with ""), reader_voice (the article is for the agent's clients and the public, not other agents: insider jargon left unexplained - days on market, sale-to-list, seller assist, millage, conforming limit, jumbo, contingency - wording aimed at agents, valuation-method talk, or talk about how the data was gathered such as "homes checked" or "records matched", talk about which copy of a law or source was read ("as the 2020 code reads; current law may differ"), a mission statement lifted from the agent's About text ("My work centers on careful valuation"), or the agent referred to in the third person or by name ("ask Peter", "contact your agent"): rewrite in plain words a buyer or seller would use, same facts), other (this includes a DODGED QUESTION: a question or point in the agent's brief that the article answers with a different fact - a hospital's distance for a commute - or waves off with "compare listing by listing" / "it varies" when the ledger has the numbers; rewrite the passage to answer it from the ledger, or, when the ledger has nothing, to say so in one honest first-person sentence with an offer to get it for the reader's own situation).
3. Give every issue a minimal fix: `find` = an exact, verbatim substring of the ARTICLE text (copy it character for character, long enough to be unique, inside one sentence or list item), `replace` = the corrected text (qualify, attribute, add the date, soften, correct it from the ledger, or "" to delete). Never add a fact that isn't in the ledger. Keep any [anchor](url) link markup intact in your replacement unless the link itself is the problem. Every item under AUTOMATED CHECKS is a confirmed problem - each needs an issue with a fix. The elements listed under INSERTED ELEMENTS are real parts of the article - a sentence pointing to them is not an unsupported claim. A figure the ledger only has second-hand (a news site "citing" the primary source) or marks as unconfirmed gets DELETED, not hedged, when the primary figure is also present. The article must never talk about its own sourcing ("I haven't confirmed...", "according to my research", "the ledger") - fix those by deleting or rewriting. A sentence saying real estate commissions are negotiable and set in the listing agreement is general practice, not an unsupported claim: leave it, and never have the agent say they "don't have a figure" for their own fee.
4. Score 1-10 - honestly, a 9-10 must be earned: accuracy, local_specificity (could only have been written about this place), usefulness (answers the reader's real question), voice (sounds like a knowledgeable local agent talking to a client, not AI and not agent-to-agent), readability (a buyer or seller with no real estate background can follow it), seo, compliance.
5. `summary`: 1-2 plain sentences for the agent. `improvements`: up to 4 specific things the agent could add (their own experience, a photo, a number they have) to make it stronger.
6. `rules_mentioned`: every law, contract right, deadline, program, tax break, exemption, credit, rebate or loan limit the article names (after your fixes) - the transfer tax and a first-time-buyer transfer-tax break count - each with the ledger's official URL when it has one. An empty list only when the article names none.

7. `brief_points`: go through the agent's brief - the topic, the goal and EACH key point - and list every question or point it raises, a few words each, with a status: answered (the article answers it directly, from the ledger or the brief), partly (it touches the point but leaves the actual question open), said_no_data (the article says plainly that it doesn't have this and offers to get it for the reader), not_answered (skipped, or a different fact stands in for it). `note`: one sentence - which ledger ids hold the answer, or that the ledger has nothing on it. Every partly or not_answered point ALSO needs an issue (kind other) with a fix: the answer from the ledger, or - when the ledger has nothing - one honest first-person sentence saying so with an offer to get it.

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
    tokens = (u["input_tokens"] * inp
              + u["cache_creation_input_tokens"] * inp * config.get("blog_pipeline_cache_write_multiplier", 1.25)
              # Cache reads are 0.1x input on most models, 0.05x on Opus 5.5 / Sonnet 5.5 (model_pricing).
              + u["cache_read_input_tokens"] * inp * p.get("cache_read_multiplier", config.get("blog_pipeline_cache_read_multiplier", 0.1))
              + u["output_tokens"] * out) / 1_000_000
    # Save usage mode sends requests through Anthropic's batch queue: every token of an answer that came that way is
    # half price. A web search costs the same either way (their pricing page, read 11/14). `batch` = this whole tally
    # came that way; `batched` = only these answers did (a step finished the fast way after the time cap is charged at
    # the normal price - owner 11/14: agents pay "the amount that they actually use").
    half = float(config.get("blog_overnight_token_multiplier", OVERNIGHT_TOKEN_MULTIPLIER))
    if u.get("batch"):
        tokens *= half
    elif u.get("batched"):
        b = u["batched"]
        tokens -= (1 - half) * (b["input_tokens"] * inp
                                + b["cache_creation_input_tokens"] * inp * config.get("blog_pipeline_cache_write_multiplier", 1.25)
                                + b["cache_read_input_tokens"] * inp * p.get("cache_read_multiplier", config.get("blog_pipeline_cache_read_multiplier", 0.1))
                                + b["output_tokens"] * out) / 1_000_000
    return round(tokens + u["web_search_requests"] * config.get("blog_pipeline_web_search_cost_usd", 0.01), 6)


def _tool_input(resp, name: str) -> Optional[dict]:
    for b in resp.content:
        if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == name and isinstance(b.input, dict):
            return b.input
    return None


async def _send(client, kwargs: dict, usage: dict):
    """Send one request: streamed now, or through the run's own transport (the overnight batch queue)."""
    transport = TRANSPORT.get()
    if transport is not None:
        return await transport.send(client, kwargs, usage)
    async with client.messages.stream(**kwargs) as stream:
        return await stream.get_final_message()


def _system_blocks(system: str, shared: Optional[str]) -> list[dict]:
    """`shared` (the evidence ledger) goes first and is the cached part: the fact-check, the grader and its
    re-check all read the same ledger with the same model minutes apart, so the first pays for it once and the
    others read it at a twentieth of the price (cost audit 11/14: it was sent in full, at full price, each time)."""
    if not shared:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    return [{"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": system}]


async def _run_tool_call(*, model: str, system: str, user_msg: str, tools: list[dict], tool_name: str, max_tokens: int,
                         effort: Optional[str], usage: dict, shared: Optional[str] = None) -> dict:
    """One model turn that must end in `tool_name` (forced tool_choice isn't available on the 5.5
    models, so this asks, resumes server-tool pauses, and nudges once if the call didn't come)."""
    client = writer._client()
    messages: list[dict] = [{"role": "user", "content": user_msg}]
    extra = {"output_config": {"effort": effort}} if effort else None
    nudged = False
    for _ in range(6):
        kwargs: dict[str, Any] = dict(
            model=model, max_tokens=max_tokens, system=_system_blocks(system, shared),
            tools=tools, tool_choice={"type": "auto"}, messages=messages,
        )
        if extra:
            kwargs["extra_body"] = extra
        resp = await _send(client, kwargs, usage)
        _add_usage(usage, resp)
        # What the model actually looked up (search words, pages opened) - kept with the run so a thin research
        # result can be explained afterwards (11/14: eight searches found nothing and there was no record of them).
        for b in resp.content:
            if getattr(b, "type", None) == "server_tool_use" and isinstance(getattr(b, "input", None), dict):
                what = b.input.get("query") or b.input.get("url") or b.input.get("code") or ""
                usage.setdefault("lookups", []).append(f"{getattr(b, 'name', 'tool')}: {str(what).strip()[:160]}")
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
        # A call to one of our other tools (the checkers share one tool list) must be answered before anything else
        # can be said - an unanswered tool call is a rejected request.
        strays = [{"type": "tool_result", "tool_use_id": b.id, "is_error": True, "content": f"Not this tool - call {tool_name}."}
                  for b in resp.content if getattr(b, "type", None) == "tool_use"]
        messages.append({"role": "user", "content": strays + [{"type": "text", "text": f"Now call {tool_name} with your result."}]})
    raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI didn't return a result - please try again. Nothing was charged for this try.")


# Research uses the DIRECT web tools: each search is one tool call and its results come straight back to the model.
# The newer "_20260209" versions make the model write a small program for every search and print slices of the
# result ("dynamic filtering"). The look-up log of 11/14 showed what that did here: three searches spent re-printing
# one train-fare result, "the search tool failed repeatedly" in the notes, and 0-2 facts found for an article -
# with nothing wrong in the questions being asked. Direct first; the newer pair only if this model refuses them.
_WEB_TOOLS = (("web_search_20250305", "web_fetch_20250910"), ("web_search_20260209", "web_fetch_20260209"))

# Every source the article leans on is listed for the reader. It was the first 14 in ledger order - and the township
# pages come last in the ledger, so a seller article full of township rules showed none of them (grader round 8).
SOURCES_SHOWN = 30

_UNCHECKED = re.compile(r"\bnot (?:yet )?(?:checked|researched|looked (?:up|for|into)|searched)\b|\bdid(?: not|n't) get to\b", re.I)


def research_follow_up(not_found: list[str], lookups: Optional[list], searches: int, fetches: int, extra: Optional[list[str]] = None) -> Optional[dict]:
    """Research that stopped with look-ups unspent AND told us what it left unchecked gets one more pass, on exactly
    those items, with exactly the look-ups it did not use - the article's total budget is unchanged.
    Grader round 7 (11/15): a Media seller article used 2 of its 8 searches, wrote "sewer lateral resale certificates
    not checked beyond FAQ" and "Marple Township ... not checked", and stopped; Middletown's sewer certification - a
    requirement before any sale there - never reached the article."""
    used_s = sum(1 for q in lookups or [] if str(q).startswith("web_search"))
    used_f = sum(1 for q in lookups or [] if str(q).startswith("web_fetch"))
    gaps = [x for x in not_found or [] if _UNCHECKED.search(x)]
    left = int(searches) - used_s
    # `extra`: gaps plain code found in what came back (round 8: a sale requirement with no lead time - the form was
    # never opened). One search left is enough to go and open a form; a vague "not checked" needs two.
    extra = [x for x in extra or [] if x]
    if left < 1 or (not extra and (not gaps or left < 2)):
        return None
    return {"gaps": (extra + gaps)[:6], "searches": left, "fetches": max(0, int(fetches) - used_f)}


def follow_up_message(again: dict, found: list[dict]) -> str:
    return ("\n\nSECOND PASS. A first pass on this article stopped with " + str(again["searches"]) + " searches"
            + (f" and {again['fetches']} page reads" if again["fetches"] else "") + " unused and left the items below unchecked. Check THESE now, "
            "in this order, one look-up at a time, and nothing else. Report only what is new.\nLEFT UNCHECKED:\n"
            + "\n".join(f"  - {g}" for g in again["gaps"])
            + ("\nALREADY FOUND IN THE FIRST PASS (do not repeat):\n" + "\n".join(f"  - {i['claim']}" for i in found[:20]) if found else ""))


def research_tools(searches: int, fetches: int, state: Optional[str], direct: bool = True) -> list[dict]:
    search_type, fetch_type = _WEB_TOOLS[0 if direct else 1]
    search: dict = {"type": search_type, "name": "web_search", "max_uses": int(searches)}
    if state:
        search["user_location"] = {"type": "approximate", "region": "Pennsylvania" if state == "PA" else state, "country": "US"}
    tools = [search]
    if fetches:
        tools.append({"type": fetch_type, "name": "web_fetch", "max_uses": int(fetches)})
    return tools + [_EVIDENCE_TOOL]


async def _research_call(*, model: str, user_msg: str, searches: int, fetches: int, state: Optional[str], max_tokens: int,
                         effort: Optional[str], usage: dict) -> dict:
    """The research turn. A 400 for the direct tools (a model or platform that only takes the newer ones) costs
    nothing and falls back to them - research never fails over a tool version."""
    import anthropic
    try:
        return await _run_tool_call(model=model, system=_RESEARCH_SYSTEM, user_msg=user_msg, tools=research_tools(searches, fetches, state),
                                    tool_name="submit_evidence", max_tokens=max_tokens, effort=effort, usage=usage)
    except anthropic.BadRequestError:
        logger.warning("blog pipeline: the direct web tools were refused for %s - using the newer versions", model, exc_info=True)
        usage.setdefault("lookups", []).append("(direct web tools refused - newer versions used)")
        return await _run_tool_call(model=model, system=_RESEARCH_SYSTEM, user_msg=user_msg,
                                    tools=research_tools(searches, fetches, state, direct=False),
                                    tool_name="submit_evidence", max_tokens=max_tokens, effort=effort, usage=usage)


class _Meter:
    """Records each stage as its own usage event on the agent's allowance, and keeps the totals."""

    def __init__(self, db: AsyncSession, user: User, config: dict, journal=None):
        self.db, self.user, self.config = db, user, config
        self.stages: list[dict] = []
        self.total = 0.0
        self.events: list = []
        # An overnight run can be picked up again after a restart (blog_overnight_service). Its journal remembers
        # which usage events each step wrote, so a step is never charged twice and a failed run refunds them all.
        self.journal = journal

    async def record(self, stage: str, model: str, usage: dict, started: float, *, failed: Optional[str] = None) -> None:
        cost = _cost(self.config, model, usage)
        self.total += cost
        self.stages.append({"stage": stage, "model": model, "cost_usd": round(cost, 4), "seconds": round(time.monotonic() - started, 1),
                            "input_tokens": usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"],
                            "cached_tokens": usage["cache_read_input_tokens"],
                            "output_tokens": usage["output_tokens"], "web_searches": usage["web_search_requests"],
                            **({"overnight": True} if usage.get("batch") or usage.get("batched") else {}),
                            # a Save usage step that was (partly) finished the fast way after the time cap
                            **({"fast_finish": True} if usage.get("batched") is not None and usage["output_tokens"] > usage["batched"]["output_tokens"] else {})})
        # Answers picked up again after a restart were charged the first time round.
        replayed = usage.get("replayed")
        charge = max(0.0, cost - _cost(self.config, model, {**replayed, "batch": True})) if replayed else cost
        if replayed and charge < 0.000001:
            return
        event = await record_usage_event(
            self.db, self.user.id, None, model, usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"],
            usage["output_tokens"], charge, 1, bool(failed), failed,
            tool_names=[f"blog_pipeline_{stage}"] + (["overnight"] if usage.get("batch") or usage.get("batched") else []), event_type=EVENT_TYPE,
        )
        self.events.append(event)
        if self.journal is not None:
            await self.journal.charged(self.db, stage, event)

    async def refund(self) -> None:
        """A run that ends without an article costs the agent nothing (user, 10/17 - Peter's usage
        went up for blog runs that failed): every stage it charged stays in the log, marked
        "refunded_failed_run", and drops out of the monthly allowance (which only counts
        un-blocked events)."""
        from sqlalchemy import update
        from app.models.database import AIUsageEvent
        ids = [e.id for e in self.events if e is not None and not e.blocked]
        if self.journal is not None:
            ids = list({*ids, *self.journal.event_ids()})   # what this run charged before a restart, too
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


def overnight_usd(config: dict, usd: float) -> float:
    """What a run that costs `usd` now costs overnight: every token at half price, web searches unchanged."""
    searches = min(float(usd), float(config.get("blog_overnight_typical_search_usd", 0.06)))
    return round((float(usd) - searches) * float(config.get("blog_overnight_token_multiplier", OVERNIGHT_TOKEN_MULTIPLIER)) + searches, 4)


async def _gate(db: AsyncSession, user: User, config: dict, engine: str) -> None:
    """Only start when the agent's remaining monthly allowance covers a typical full run - the
    allowance is the only limit (no article count), and a run can't push them past it. Save usage
    mode asks for the same: at worst (the slower line is busy and the time cap finishes it the fast
    way) it uses the normal amount."""
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

def sale_vs_asking(sale_price, asking, days) -> str:
    """ "; its last asking price was $7,495,000, so it closed about 4.6% below asking, after 125 days on the market" -
    or "" when the listing record gives neither. The two prices as recorded on the MLS; no reason is ever given."""
    try:
        sp, ap = float(sale_price or 0), float(asking or 0)
    except (TypeError, ValueError):
        sp = ap = 0.0
    out = ""
    if sp > 0 and ap > 0:
        gap = (sp - ap) / ap * 100
        how = "at its asking price" if abs(gap) < 0.05 else f"about {abs(gap):.1f}% {'below' if gap < 0 else 'above'} asking"
        out += f"; its last asking price was {writer._money(ap)}, so it closed {how}"
    try:
        d = int(days) if days is not None else None
    except (TypeError, ValueError):
        d = None
    if d is not None and d >= 0:
        out += f"{', after' if out else '; it took'} {d} day{'' if d == 1 else 's'} on the market"
    return out


async def sale_records(db: AsyncSession, sales: list[dict]) -> dict:
    """The MLS record behind each of the agent's own sales (their ids are listing keys): the last asking price and the
    days on market. Grader 11/14: an article titled "what my recent sales say about pricing" could only repeat each
    sale's price and month - while we held that one closed 4.6% under asking after 125 days and the other 10% under."""
    from sqlalchemy import select
    from app.models.database import Property
    keys = [str(s.get("id")) for s in sales if s.get("id")]
    if not keys:
        return {}
    try:
        rows = (await db.execute(select(Property.listing_key, Property.price, Property.days_on_market).where(Property.listing_key.in_(keys)))).all()
    except Exception:  # noqa: BLE001 - an extra, never a reason to fail the article
        logger.warning("blog pipeline: could not read the agent's sale records", exc_info=True)
        return {}
    return {str(k): {"asking": p, "days": d} for k, p, d in rows}


def _sale_items(sales: list[dict], records: Optional[dict] = None) -> list[dict]:
    out = []
    for s in sales:
        rec = (records or {}).get(str(s.get("id"))) or {}
        bits = [writer._money(s.get("price")) and f"closed at {writer._money(s.get('price'))}",
                s.get("sold_date") and f"on {s['sold_date']}",
                s.get("beds") and f"{s['beds']} bd", s.get("baths") and f"{s['baths']} ba",
                s.get("sqft") and f"{s['sqft']} sqft"]
        out.append({"topic": "agent_sales", "claim": f"{s.get('address')}, {s.get('city')} {', '.join(b for b in bits if b)}{sale_vs_asking(s.get('price'), rec.get('asking'), rec.get('days'))} - one of the agent's own closed transactions (buyer/seller side not recorded).",
                    "source_name": "Bright MLS (agent's closed sales)", "source_url": None, "as_of": s.get("sold_date"), "origin": "agent_sales", "town": s.get("city")})
    return out


def _listing_items(listings: list[dict]) -> list[dict]:
    return [{"topic": "agent_listing", "claim": f"{writer._fmt_property(p)} - a listing the agent attached.", "source_name": "Bright MLS (agent's listing)",
             "source_url": None, "as_of": datetime.now(timezone.utc).date().isoformat(), "origin": "agent_listing", "town": p.get("city")} for p in listings]


_BRIEF_STOP = {"about", "what", "know", "should", "before", "making", "with", "that", "this", "from", "have", "your", "their", "there", "they",
               "will", "much", "many", "more", "most", "into", "over", "home", "homes", "house", "houses", "buying", "selling", "guide", "versus",
               "whether", "them", "these", "those", "when", "where", "which", "does", "life", "cost", "costs", "prices", "price", "works", "work",
               "moving", "living", "feel", "ready", "smart", "nearby", "surrounding", "town", "area"}
# What a word in the brief is really asking for: the stored facts that answer it rarely use the same word.
_BRIEF_TOPICS = (
    (re.compile(r"commut|drive|driving|train|transit|rail|getting (?:to|around)|airport", re.I), {"transit", "commute"},
     re.compile(r"minutes by (?:car|public transit)|miles away|train station|rail|bus", re.I)),
    (re.compile(r"school|district", re.I), {"schools"}, re.compile(r"school district|elementary|middle school|high school", re.I)),
    (re.compile(r"flood|insurance", re.I), set(), re.compile(r"flood", re.I)),
    (re.compile(r"walk|downtown|restaurant|dining|shop|day-to-day|daily life|things to do|weekend", re.I), {"downtown"},
     re.compile(r"15-minute walk|grocery|park|restaurant", re.I)),
)


def brief_first(items: list[dict], brief: dict) -> list[dict]:
    """The same items with the ones the brief asks about first (a stable sort - ties keep their order).
    11/14: asked for the commute to Wilmington and Philadelphia, a Kennett Square article said it had no
    drive times - the town's record held both, but they sat after 49 other stored facts and the per-town
    cap cut them (restaurants made it in; the commute did not)."""
    said = " ".join(str(brief.get(k) or "") for k in ("topic", "key_points", "goal"))
    words = {w for w in re.findall(r"[a-z]{4,}", said.lower()) if w not in _BRIEF_STOP}
    # the town's own name is in nearly every one of its facts - it says nothing about what was asked
    words -= {w for a in brief.get("areas") or [] for w in re.findall(r"[a-z]{4,}", str(a).lower())}
    asked = [(topics, rx) for ask, topics, rx in _BRIEF_TOPICS if ask.search(said)]

    def score(it: dict) -> int:
        claim = str(it.get("claim") or "")
        low = claim.lower()
        n = sum(1 for w in words if w in low)
        for topics, rx in asked:
            n += 3 * (it.get("topic") in topics) + 3 * bool(rx.search(claim))
        return n
    return sorted(items, key=lambda it: -score(it))


_TOO_MANY_WS = re.compile(r"^(https?://)w{4,}\.", re.I)


def tidy_url(url) -> str:
    """A source link as a reader should see it: "https://wwww.septa.org/fares/" (research typed one w too many; the
    site answers on any name, so nothing caught it - rounds 10 and 11) becomes "https://www.septa.org/fares/"."""
    return _TOO_MANY_WS.sub(r"\1www.", str(url or "").strip())


def _build_ledger(items: list[dict], per_town_cap: int = 55) -> tuple[list[dict], str]:
    """Number every item [E1]..[En] and render the ledger text. The cap is on a town's STORED research
    only (it used to count the town's MLS numbers too, so every new MLS fact pushed a stored one out)."""
    seen, ledger, per_town = set(), [], {}
    for it in items:
        key = (it.get("claim") or "").strip().lower()
        if not key or key in seen:
            continue
        town = it.get("town") or "general"
        capped = it.get("origin") in ("idx_research", "evidence_bank")
        if capped and per_town.get(town, 0) >= per_town_cap:
            continue
        per_town[town] = per_town.get(town, 0) + (1 if capped else 0)
        seen.add(key)
        ledger.append({**it, **({"source_url": tidy_url(it["source_url"])} if it.get("source_url") else {}), "eid": f"E{len(ledger) + 1}"})
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
    # A buyer's worked example belongs in an article for buyers. 11/13: a seller article that mentioned
    # buyers and mortgages got "a random $350,000 example" - the words alone decided it.
    sides = bfc.reader_sides(brief)
    if "buyer" in sides:
        return True
    if "seller" in sides:
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


_DOWNSIZING = _COVERAGE[0][0]
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
    # A downsizer is buying the smaller home. Grader round 7 (11/15): a Malvern downsizing article ("... selling the big
    # house ...") got a $1,080,000 single-family example beside its own $690,000 condo-and-townhome median.
    if _DOWNSIZING.search(text):
        return "attached"
    if _MOVE_UP.search(text) or _HOUSE_TYPE.search(text):
        return "house"
    return "attached" if _ATTACHED_TYPE.search(text) else "entry"


# A fee joins the payment card only when at least this share of that type's listings report one. 11/16 out-of-state
# round: Wilmington, DE - 36 of the 240 townhomes for sale listed a fee (median $28 a month) and the card added
# "$28/month" to a $200,000 example, as if the typical townhome there had one. The article's own text still says how
# many listings give a fee.
FEE_TYPICAL_SHARE = 0.5


def example_fees(cost: Optional[dict]) -> dict:
    """{"townhouse" / "condo": the median association fee of that type's listings} - only where the town's own fact
    states that median (10+ listings) and at least half of that type's listings report a fee."""
    out = {}
    for kind, label in (("townhouse", "townhomes"), ("condo", "condos")):
        h = ((cost or {}).get("hoa") or {}).get(kind) or {}
        homes, of = h.get("homes") or 0, h.get("of") or 0
        if h.get("median_monthly") and homes >= 10 and (not of or homes / of >= FEE_TYPICAL_SHARE):
            out[kind] = {"monthly": int(h["median_monthly"]), "homes": int(homes), "label": label}
    return out


def example_fee(cost: Optional[dict]) -> Optional[dict]:
    """The association fee an attached-home example may add: the median of the condos or townhomes for sale that
    list one, whichever the town has more of - only where the town's own fact states that median (10+ listings)."""
    best = None
    for f in example_fees(cost).values():
        if best is None or f["homes"] > best["homes"]:
            best = f
    return best


# An entry-price example ("a quarter of the homes for sale are at or below this price") adds the association fee when
# at least this share of the homes for sale near that price are condos or townhomes (and at least five are near it).
ENTRY_FEE_SHARE = 2 / 3


def entry_fee(entry: dict, fees: Optional[dict]) -> Optional[dict]:
    """The fee an ENTRY-price example adds: when most homes for sale near that price are condos or townhomes, the
    median fee of whichever of the two is more common there - and only where the town's own fact states that median.
    Grader round 10 (11/15): Wayne's $290,000 example sat among condos whose listings showed a $352 median fee, and
    Kennett Square's $540,000 example among townhomes (38 fees, $215 median); both cards left the fee out because the
    ARTICLE was not about condos. The reader's monthly cost is about the home, not the article's title."""
    near = (entry or {}).get("near_homes") or 0
    condos, towns = (entry or {}).get("near_condos") or 0, (entry or {}).get("near_townhomes") or 0
    if near < 5 or (condos + towns) / near < ENTRY_FEE_SHARE:
        return None
    fee = (fees or {}).get("condo" if condos >= towns else "townhouse")
    return {**fee, "near": near, "near_attached": condos + towns} if fee else None


def tax_basis_words(kind: str, homes: Optional[int], scope: Optional[str]) -> str:
    """Whose tax bills the example's estimate used, in the card's words. "similar-priced" only when it is true: with
    too few bills near the price the estimate uses every listing that shows one, and says so (round 10, Radnor: "the
    42 similar-priced homes for sale" beside the article's own "32 listings that show an annual property tax")."""
    what = {"house": "single-family houses", "attached": "condos and townhomes"}.get(kind, "homes")
    # "scaled to this price": the estimate is each bill's share of its own home's price, applied to the example's
    # price - so it is NOT the median bill the article quotes for the same homes (round 11, Radnor: a $9,583 median
    # bill in the text beside about $5,892 a year on the card's $460,000 home, both from the same 32 listings).
    if scope == "all" and homes:
        return f"tax bills of the {homes} {what} for sale that list one{TAX_SCALED}"
    return (f"tax bills of the {homes} " if homes else "tax bills of ") + f"similar-priced {what} for sale{TAX_SCALED}"


TAX_SCALED = ", scaled to this price"


def payment_example(entry: dict, rate: dict, area_names: list[str], *, conforming_limit: Optional[int] = None, limit_area: Optional[str] = None,
                    basis: str = "entry", sold_house: Optional[dict] = None, transfer_tax: Optional[dict] = None,
                    fee: Optional[dict] = None, fha: Optional[dict] = None, fees: Optional[dict] = None,
                    homes_for_sale: Optional[int] = None) -> Optional[dict]:
    """A worked example for a realistic entry price (the 25th-percentile list price of homes for
    sale across the article's towns): down payment, loan, principal & interest at Freddie Mac's
    latest 30-year average, a property-tax estimate from similar listings' tax bills, and closing
    costs at the CFPB's 2-5% range. Every number is computed here, never by the writer."""
    if not entry or not entry.get("p25") or not rate or not rate.get("rate_30"):
        return None
    kind = basis if basis in ("house", "attached") and entry.get(f"{basis}_median") else "entry"
    # ONE count of homes for sale per article. The card reads the listings a moment after the article's own snapshot,
    # and a listing can leave the market in between (11/17 Wilmington proof: the article said 482 homes, the card 481).
    # When the two are the same homes give or take a couple, the card states the article's number.
    n_homes = entry.get("homes") or 0
    if homes_for_sale and n_homes and abs(int(homes_for_sale) - n_homes) <= max(2, round(0.01 * n_homes)):
        n_homes = int(homes_for_sale)
    price = _round_price(entry[f"{kind}_median"] if kind != "entry" else entry["p25"])
    tax_pct = entry.get(f"tax_pct_near_{kind}") if kind != "entry" else entry.get("tax_pct_near_p25")
    tax_homes = entry.get(f"tax_homes_{kind}") if kind != "entry" else entry.get("tax_homes")
    # "near": bills of homes priced near the example; "all": too few of those, so every listing's bill was used.
    tax_scope = entry.get(f"tax_scope_{kind}") if kind != "entry" else entry.get("tax_scope")
    # A house example is priced at what houses SOLD for, when there are enough recent sales - asking prices of
    # the houses still for sale lean high (11/14 Kennett Square: a $990,000 example while houses sold for a
    # median of $738,000, which also put two of its three loans over the jumbo line).
    sold = sold_house if kind == "house" and sold_house and (sold_house.get("count") or 0) >= SOLD_BASIS_MIN_SALES and sold_house.get("price") else None
    if sold:
        price = _round_price(sold["price"])
        if entry.get("tax_pct_near_house_sold"):
            tax_pct, tax_homes, tax_scope = entry["tax_pct_near_house_sold"], entry.get("tax_homes_house_sold"), "near"
    tax_mo = round(price * tax_pct / 100 / 12) if tax_pct else None
    # Closing costs: the CFPB's national 2-5%. Where we hold the place's whole transfer tax, the low end can't
    # sit below the buyer's customary half of that tax alone (11/14 grader, Fishtown: "$6,900-$17,250" beside a
    # transfer tax whose half was $7,900).
    closing_low, closing_high, tt = round(price * 0.02), round(price * 0.05), None
    if transfer_tax and transfer_tax.get("total_pct"):
        whole = int(round(price * float(transfer_tax["total_pct"]) / 100 / 100.0) * 100)
        tt = {"place": transfer_tax.get("place"), "total": whole, "half": whole // 2}
        # 11/14 second round: with the low end AT the buyer's half ($7,900) there was $3 left for the lender, the title
        # company and everything else. Where we hold the whole transfer tax it is its own line, and the national 2-5%
        # guide (built for places without a tax this size) covers the rest - disclosed on the card as exactly that.
        closing_low, closing_high = tt["half"] + round(price * 0.02), tt["half"] + round(price * 0.05)
    scenarios = []
    for pct in (5, 10, 20):
        down = round(price * pct / 100)
        loan = price - down
        pi = round(_monthly_pi(loan, rate["rate_30"]))
        scenarios.append({"down_pct": pct, "down": down, "loan": loan, "principal_interest": pi,
                          "mortgage_insurance": "none" if pct >= 20 else "lender quote", "tax": tax_mo,
                          "monthly_before_insurance": pi + (tax_mo or 0),
                          "jumbo": bool(conforming_limit and loan > conforming_limit)})
    # Grader round 7: "what it really costs each month" - where the example IS a condo or townhome and the town's
    # listings give a median association fee, the card adds it. Insurance and mortgage insurance stay out: we hold
    # no real quote for either.
    # Round 10: an entry-price example adds it too when most homes for sale near its price are condos or townhomes.
    fee = fee if kind == "attached" and fee and fee.get("monthly") else entry_fee(entry, fees) if kind == "entry" else None
    if fee:
        for sc in scenarios:
            sc["with_fee"] = sc["monthly_before_insurance"] + fee["monthly"]
    # FHA at this price: with FHA's minimum 3.5% down, is the loan above FHA's lowest county limit, or above its
    # highest? (Round 7: a $680,000 Doylestown example beside "ask about FHA" - over Bucks County's FHA limit.)
    fha_note = None
    if fha and fha.get("floor") and fha.get("ceiling"):
        fha_loan = price - round(price * 0.035)
        fha_note = {"loan": fha_loan, "year": fha.get("year"), "source_name": fha.get("source_name"), "source_url": fha.get("source_url"),
                    "verified": fha.get("verified"), "over": "everywhere" if fha_loan > fha["ceiling"] else "some" if fha_loan > fha["floor"] else None}
    return {
        "fee": fee, "fha": fha_note,
        "price": price, "price_basis": (f"About the median price of the {sold['count']} single-family houses sold in {_and_list(area_names)} in {sold['window']}." if sold
                                        else f"About the median list price of the {entry['houses']} single-family houses for sale across {_and_list(area_names)}." if kind == "house"
                                        else f"About the median list price of the {entry['attached']} condos and townhomes for sale across {_and_list(area_names)}." if kind == "attached"
                                        else f"About a quarter of the {n_homes} homes for sale across {_and_list(area_names)} are listed at or below this price."),
        "rate": rate["rate_30"], "rate_as_of": rate.get("as_of"), "term_years": 30,
        # The estimate's basis in words only - a tax bill as a percent of price reads like a tax
        # rate (11/01 grader, Princeton). The percent stays internal (tax_pct) for the math.
        "tax_pct": tax_pct, "tax_basis": tax_basis_words(kind, tax_homes, tax_scope) if tax_pct else None,
        "closing_low": closing_low, "closing_high": closing_high, "transfer_tax": tt,
        # What the range is made of, in the card's own words (None = the plain national 2-5%).
        "closing_basis": (f"your half of {tt['place']}'s realty transfer tax (about {writer._money(tt['half'])} when buyer and seller split it evenly, "
                          f"about {writer._money(tt['total'])} in all) plus the CFPB's typical 2-5% of the price for lender, title, recording and "
                          "prepaid costs." if tt else None),
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
    tt = ex.get("transfer_tax")
    items.append(f"Payment example closing costs: {m(ex['closing_low'])}-{m(ex['closing_high'])} on top of the down payment "
                 + (f"- an estimate built from two parts: the buyer's half of {tt['place']}'s realty transfer tax (about {m(tt['half'])} at this "
                    f"price when buyer and seller split it evenly; about {m(tt['total'])} in all) PLUS the CFPB's typical 2-5% of the price for "
                    "lender, title, recording and prepaid costs. The settlement company's itemized estimate is the real figure." if tt
                    else "(the CFPB's typical 2-5% of the price is a national guide)."))
    fee = ex.get("fee")
    if fee:
        items.append("Payment example with the association fee: "
                     + (f"{fee['near_attached']} of the {fee['near']} homes for sale near this price are condos or townhomes, so the example adds a fee. "
                        if fee.get("near") else "")
                     + f"{'The' if fee.get('near') else 'the'} {fee['homes']} {fee['label']} for sale that list a monthly fee show a median of "
                     f"{m(fee['monthly'])}/month, all fees combined. Adding that fee brings the example to about "
                     + ", ".join(f"{m(sc['with_fee'])}/month at {sc['down_pct']}% down" for sc in ex["scenarios"])
                     + " - still before homeowners insurance, and before mortgage insurance under 20% down. A home's own fee can be higher or lower.")
    out = [{"topic": "market", "claim": c, "source_name": "OpenHousePal calculation", "source_url": None, "as_of": ex["as_of"], "confidence": 0.99,
            "town": "general", "origin": "calc"} for c in items]
    fha = ex.get("fha") or {}
    if fha.get("over"):
        out.append({"topic": "regulation", "claim": (
            f"Loan programs at the payment example's price: FHA sets a loan limit for each county. With FHA's minimum down payment, a {m(ex['price'])} home "
            f"needs a loan of about {m(fha['loan'])}, which is "
            + (f"above FHA's {fha['year']} loan limit in every county, so an FHA loan is not an option at this price." if fha["over"] == "everywhere"
               else f"above FHA's {fha['year']} limit in the counties with the lowest limits - so FHA may not be available at this price here; a lender can "
                    "confirm this county's limit.")
            + " State housing-agency programs also have purchase-price and income limits that differ by program and county, so at this price some of "
              "them may not apply. Name the programs, and say plainly that at this example's price some may not be available."),
            "source_name": fha.get("source_name") or "HUD", "source_url": fha.get("source_url"), "as_of": fha.get("verified"), "confidence": 0.98,
            "town": "general", "origin": "calc"})
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

SOLD_BASIS_MIN_SALES = 10      # a house example is priced at the sold median only with this many recent house sales


def closing_note_for(ex: dict, state: Optional[str]) -> Optional[str]:
    """The line under the payment card's closing costs: the place's transfer tax in dollars where we hold all
    of it, else the state's general note."""
    tt = (ex or {}).get("transfer_tax")
    if tt:
        m = writer._money
        return "Ask the settlement company for an itemized estimate - it is the real figure."
    return _CLOSING_NOTES.get((state or "").upper())


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
- A municipality's own lead time for applying for a resale or occupancy certificate ("at least three weeks before settlement"), taken from that municipality's ledger item and worded as what it currently asks, is allowed - it is not fine print. Any other day count still is. Where that ledger item says a lead time or fee is "not yet confirmed", the article is RIGHT to name the requirement with no number, say to start it early and point to the municipality's page - never fail any item because that number is missing, and never ask for it in a fix; a number the article gives for it anyway is a fine_print failure.
- A transfer-tax rate or dollar example taken from a ledger item that begins "TRANSFER TAX", "Transfer tax example" or "Realty Transfer Fee example", worded as an estimate, is allowed - it is not fine print. Any other closing-tax rate or amount still is.
- The agent's own closing offer ("I'll start your search", "I'll set up showings") is correct even for towns outside the agent's state. The agent's personal take from the brief is theirs to say. When the ledger says the agent is NOT licensed in the article's state, the right next step is an offer to introduce the reader to an agent who is licensed there - never fail that, and DO fail `next_step` for a sentence in which the agent offers to list, price, value, show or represent a home there themselves.

Respond only by calling submit_grade."""


# The fact-check, the grader, its re-check and its rewrite run on the same model, minutes apart, over the same
# evidence ledger. They are sent the same tool list and the same first system block (the ledger), so the first of
# them pays for the ledger and the rest read it from the cache (cost audit 11/14).
_CHECK_TOOLS = [_REVIEW_TOOL, _GRADE_TOOL, _FIX_TOOL]


def _ledger_block(ledger_text: str) -> str:
    return ("== EVIDENCE LEDGER ==\nThe verified facts for this article - the only facts it may state. Your instructions come after "
            "it. Several tools are listed because other steps share this request's opening; call only the one your instructions name."
            f"\n\n{ledger_text}")


_CLAIM_SENTENCES = re.compile(
    r"(?<!\bSt\.)(?<!\bMt\.)(?<!\bDr\.)(?<!\bRd\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bNo\.)(?<!\bvs\.)(?<!\bAve\.)(?<!\bMrs\.)(?<!\bU\.S\.)(?<=[.!?])\s+(?=[\"\u201c(\[]?[A-Z0-9$])|\n+")


def expand_quotes(text: str, claims: list[dict], key: str) -> int:
    """A claim named by its opening words gets its whole sentence back (cost audit 11/14: the writer and the
    fact-checker each re-typed every factual sentence of the article in their claim lists). Claims come in article
    order, so the search runs forward from the last sentence found and only then from the top. A claim already
    given as a whole sentence is left alone. Returns how many were restored."""
    squash = lambda t: " ".join(str(t or "").split()).strip(" \"'\u201c\u201d\u2026.").lower()
    sentences = [x.strip() for x in _CLAIM_SENTENCES.split(re.sub(r"(?m)^(?:#{2,3} |- )", "", text or "")) if x and x.strip()]
    keys = [squash(x) for x in sentences]
    at = restored = 0
    for c in claims:
        q = squash(c.get(key))
        if not q or len(q.split()) > 9:
            continue
        order = list(range(at, len(keys))) + list(range(at))
        hit = next((i for i in order if keys[i].startswith(q)), None)
        if hit is None:
            hit = next((i for i in order if q in keys[i]), None)
        if hit is not None:
            c[key] = sentences[hit]
            at = hit
            restored += 1
    return restored


def card_transit(card: dict, facts: Optional[dict]) -> None:
    """The town card's "Transit nearby" line names the stops our distance check measured from the town's homes, when
    it found any - the same ones the article's own text is given. Grader 11/14: Gladwyne's card said "Manayunk" (from
    the town's general write-up) while the article said the stops in range were Miquon and Spring Mill."""
    stops = [str(x).strip() for x in ((facts or {}).get("walk") or {}).get("stations") or [] if str(x).strip()]
    nearby = card.get("nearby") if isinstance(card.get("nearby"), dict) else None
    if stops and nearby is not None:
        nearby["transportation"] = stops[:3]


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


_OFFICIAL_HOST = re.compile(r"\.(?:gov|edu|us|org|mil)$", re.I)
_OPEN_WIKI = re.compile(r"(?:^|\.)(?:wikipedia|wikimedia|wiktionary|wikivoyage|wikidata|wikibooks|fandom|wikia)\.(?:org|com)$", re.I)


def official_source(url) -> bool:
    """A government, school, military or .org address - the bar for a source a READER is shown from a town's older
    stored research, and for any stored fact about schools, taxes or rules. 11/14, the owner's account: a Bryn Mawr
    article's public source list linked another agent's website (a stored "schools" fact had come from it)."""
    from urllib.parse import urlparse
    try:
        host = urlparse(str(url or "")).hostname or ""
    except ValueError:
        return False
    # An encyclopedia anyone can edit ends in .org and is not anybody's official page (grader round 10: Wikipedia sat
    # in three source lists, behind stored facts about school districts and private schools).
    return bool(_OFFICIAL_HOST.search(host)) and not _OPEN_WIKI.search(host)


def trusted_stored_research(items: list[dict]) -> list[dict]:
    """Stored town research minus facts about schools, taxes or rules that did not come from an official address."""
    return [i for i in items if not (i.get("origin") == "idx_research" and i.get("topic") in ("schools", "taxes", "regulation")
                                     and i.get("source_url") and not official_source(i["source_url"]))]


def picked_sales(brief: dict, sales: list[dict]) -> list[dict]:
    """The agent's own sales they ticked in the writer (not the automatic "my recent sales" set): each one is
    something they asked to see in the article. 11/14, the owner's own account: 401 Thornbrook Ave was picked for a
    Bryn Mawr seller article and never appeared - it reached the ledger as one fact among 130 and nothing asked for it."""
    wanted = {str(x) for x in brief.get("sale_ids") or []}
    out = []
    for s in sales or []:
        address = str(s.get("address") or "").strip()
        if str(s.get("id")) in wanted and len(address.split()) >= 2:
            out.append({"address": address, "needle": " ".join(address.split()[:2]), "city": s.get("city"),
                        "price": writer._money(s.get("price")), "sold": str(s.get("sold_date") or "")[:7]})
    return out


def code_problems(text: str, *, brief: dict, protections: list[dict], agent_name: str = "", tax_rates=None, ledger: Optional[list[dict]] = None,
                  freshness: Optional[dict] = None, geo_check=None, must_mention: Optional[list[dict]] = None,
                  not_licensed: Optional[str] = None) -> list[dict]:
    """EVERY problem plain code can confirm in this text, as one list - the same list after the first draft,
    after the fact-check's edits, after the repair, inside the grader and for the agent's final report.
    Engine audit 11/14: the checks lived in four places and three of them (local sale rules, the abatement
    facts, the data's age) ran once on the first draft and were never looked at again - a later step could
    delete the sentence that satisfied one and the article was still saved as verified.
    Each: {kind, pass (pass-list id), quote ("" = something missing), problem, note (for the agent),
    severity, cut (cut if the repair fails), cut_final (cut if the grader's rewrite fails), missing}."""
    out: list[dict] = []

    def add(kind, pid, quote, problem, *, severity="high", cut=False, cut_final=False, note=None, **more):
        out.append({"kind": kind, "pass": pid, "quote": quote or "", "problem": problem, "note": note or problem, "severity": severity,
                    "cut": cut, "cut_final": cut or cut_final, "missing": not quote, **more})

    for f in (geo_check(text) if geo_check else []):
        add("wrong_geography", "place", f["sentence"], f"puts {f['town']} in {f['place']} ({f['kind']}), but verified: {f['truth']}.",
            cut_final=True, note=f"Puts {f['town']} in {f['place']} - {f['truth']}.")
    for sent in loan_rule_lint(text):
        add("overgeneralization", "facts", sent, "states a mortgage-insurance / down-payment rule as universal - name the loan type it applies to.",
            note="A loan rule stated without its loan type.", loan=True)
    for h in bfc.final_lint(text, brief, tax_rates):
        add(h["kind"], bfc._LINT_TO_PASS[h["kind"]], h["sentence"], h["fix"], cut=True)
    for pr in bfc.missing_protections(text, protections):
        add("missing_protection", "protections", "", f"the article never names {pr['name']} - add one plain sentence (reworded to fit, same voice) after "
            f"the most related sentence: \"{pr['says']}\"", severity="medium", note=f"Not mentioned: {pr['says']}", name=pr["name"], says=pr["says"])
    own = bfc.code_checks(text, brief, [], agent_name, tax_rates)
    for pid, kind in (("voice", "reader_voice"), ("complete", "placeholder")):
        for q in own.get(pid, []):
            add(kind, pid, q["quote"], q["fix"], severity="medium")
    for kind, label, found in (("missing_local_requirement", "Local sale requirement", local_sale_requirement_checks(text, ledger or [], brief)),
                               ("missing_abatement_fact", "Tax abatement", abatement_checks(text, ledger or []))):
        for x in found:
            msg = x.split(": ", 1)[1]
            said = msg.split(" - add", 1)[0].strip()
            add(kind, "protections", "", msg, note=said[:1].upper() + said[1:] + ".", name=label)
    low = bfc._plain(text or "").lower()
    for s in must_mention or []:
        if s["needle"].lower() not in low:
            add("missing_sale", "asked", "", f"the article never mentions the agent's own sale they chose to include - add one sentence where it helps the "
                f"reader, as \"a recent sale of mine\" with its public facts only ({s['address']}, {s.get('city') or ''}, {s.get('price') or ''}"
                + (f", {s['sold']}" if s.get("sold") else "") + "), never which side the agent represented",
                severity="medium", note=f"The sale you picked ({s['address']}) isn't mentioned in the article.", name=s["address"])
    for x in stale_wording_checks(text, freshness):
        m = re.match(r'stale_data: "(.*)" (calls MLS numbers current.*)$', x, re.S)
        if m:
            add("stale_data", "dates", m.group(1), m.group(2), severity="medium")
    for quote, number in unconfirmed_number_claims(text, ledger or [], brief):
        add("unconfirmed_number", "fine_print", quote, f"gives \"{number}\" as a municipal deadline, lead time or fee, a number we have read only once - reword "
            "with NO number: name the requirement, say it has to be started well before settlement and that the municipality's page has the "
            "current timing and fee (keep its link), and offer to confirm it for the seller's address",
            note=f"A township deadline or fee in the article (\"{number}\") has been read only once - check it on the township's page before you publish.")
    if not_licensed and not _LICENSE_NOTE.search(bfc._plain(text or "")):
        add("missing_license_note", "next_step", "", "the agent is not licensed in this article's state and the article never says so - add one "
            f"first-person sentence in the closing (reworded to fit, same voice): \"{not_licensed}\"", severity="high",
            note="The article doesn't say you aren't licensed in this state.", name="Licence")
    for quote, what in contact_detail_claims(text, ledger or [], brief):
        add("contact_detail", "fine_print", quote, f"prints {what} of an office, inspector or company - take it out and say whose page has it "
            "(keep the page's link); contact details change and are not what the article is for",
            note=f"The article printed {what} from someone else's page - it should send readers to that page instead.")
    for quote, pr in broadened_protections(text, protections):
        add("protection_scope", "protections", quote, f"states {pr['name']} as if it covered every sale - the sentence must keep its limits, as in: "
            f"\"{pr['says']}\"", note=f"{pr['name']} is stated more widely than the rule goes.", says=pr["says"], name=pr["name"])
    for quote in clock_time_claims(text, brief):
        add("clock_time", "fine_print", quote, "prints a departure or arrival clock time for a train, bus or ferry - reword with NO clock time: say "
            "about how long the ride takes and about how often it runs, per the timetable (keep its link and its effective date); a timetable "
            "row is easy to misread by one station and the times change with every new timetable",
            note="The article printed a train or bus clock time - it should give the ride's length and point to the timetable.")
    for quote in walk_time_claims(text):
        add("walk_time", "math", quote, "states a share of homes as a walking TIME; our figure is a straight-line distance - say \"within about "
            "0.6 miles, measured in a straight line\" (same share, same place), and never \"a 15-minute walk\" or \"walkable\"",
            severity="medium", note="A share of homes is given as a walking time, but our figure is a straight-line distance.")
    return out


# A municipal sale requirement, as an article names one (never a private home inspection or a mortgage step).
_MUNI_REQUIREMENT = re.compile(
    r"certificat(?:e|ion)|use[- ]and[- ]occupancy|\bU\s?&\s?O\b|resale (?:inspection|application|permit)|occupancy (?:inspection|permit)|"
    r"sewer|lateral|smoke[- ](?:detector|alarm)|carbon[- ]monoxide", re.I)
# ... or a lead time by its shape ("at least two weeks before the scheduled settlement date"), or a step a municipality
# asks for - real articles say "It currently asks for the application ..." without naming the certificate again.
_BEFORE_SETTLEMENT = re.compile(
    r"\b(?:before|prior to|ahead of|in advance of)\s+(?:the\s+|your\s+)?(?:scheduled\s+|initial\s+|planned\s+|expected\s+)?"
    r"(?:settlement|closing|date of occupancy|occupancy)", re.I)
_MUNI_STEP = re.compile(r"(?=.*\b(?:township|borough|municipalit\w+|code office|authority)\b)(?=.*\b(?:application|inspection|permit|fee)\b)", re.I | re.S)
_FEE_SIZED = 2000          # a municipal fee; a larger dollar figure in the same sentence is a price or a cost, not this


def hold_back_unplaced(items: list[dict]) -> int:
    """House rule 1's backstop: how many numbers were held back. A local sale-requirement fact (a certificate, an
    inspection, a sewer certification) read on the web that could NOT be tied to one of the article's municipalities
    has no page-by-page record, so no second reading can ever confirm it: its day counts and fees are held back like
    any number read once. 11/16 out-of-state round, Haddonfield NJ: the borough's page was read once, the fact said
    "Borough of Haddonfield" where we expected "Haddonfield Borough", so it was nobody's municipal fact and "at least
    10 days before settlement" was printed (the borough's own application form words it differently). The name is
    recognised now (tk.muni_forms); this catches the next wording we have not met."""
    held = 0
    for i in items or []:
        claim = str(i.get("claim") or "")
        if (i.get("municipality") or not i.get("source_url") or str(i.get("town") or "general").split(",")[0].strip().lower() == "general"
                or "do NOT state a number" in claim or not _MUNI_REQUIREMENT.search(claim)):
            continue
        i["claim"], n = tk.mask_unconfirmed(claim, set(), set())
        held += n
    return held


def unconfirmed_number_claims(text: str, ledger: list[dict], brief: Optional[dict] = None) -> list[tuple[str, str]]:
    """[(sentence, number)] - sentences about a municipal sale requirement that give a day count, week count or fee the
    ledger does not hold. Only looked for when the ledger holds a municipal item with a number HELD BACK (read once,
    not yet confirmed by a second reading - owner 11/15): the writer is told to give no number there, and this is the
    check that it didn't. A number the ledger does hold anywhere (a confirmed lead time, a market figure) and a number
    the agent typed into the brief are left alone."""
    marker = tk._NOT_CONFIRMED.split(" - ", 1)[1].rstrip(")")              # "not yet confirmed: do NOT state a number"
    if not any(marker in str(it.get("claim") or "") for it in ledger or []):
        return []
    held = set()
    for it in ledger or []:
        held |= tk.specifics(it.get("claim"))
    for k in ("topic", "goal", "key_points", "personal_take", "facts"):
        v = (brief or {}).get(k)
        held |= tk.specifics(" ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else v)
    out = []
    for sentence in _SENTENCES.split(text or ""):
        t = re.sub(r"^(?:#{2,3} |- )", "", sentence.strip())
        if not t or t.startswith("[Ask-me line") or not (_MUNI_REQUIREMENT.search(t) or _BEFORE_SETTLEMENT.search(t) or _MUNI_STEP.search(t)):
            continue
        for m in tk._SPECIFIC.finditer(t):
            key = tk._specific_key(m.group(0))
            if key in held:
                continue
            if key.startswith("$"):
                try:
                    if float(key[1:] or 0) >= _FEE_SIZED:
                        continue
                except ValueError:
                    continue
            out.append((t, " ".join(m.group(0).split())))
            break
    return out


STATE_NAMES = {"PA": "Pennsylvania", "NJ": "New Jersey", "DE": "Delaware", "MD": "Maryland", "DC": "Washington, DC", "VA": "Virginia",
               "WV": "West Virginia", "NY": "New York"}


def state_names(codes) -> str:
    names = [STATE_NAMES.get(c, c) for c in codes]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


async def out_of_area_states(db, user, town_states: set[str]) -> tuple[list[str], set[str]]:
    """(the article's states the agent does not work in, the states the agent does work in). An article's towns are
    "outside" only when NONE of them is in a state the agent works in - their own state plus their served towns'."""
    agent_states = {(getattr(user, "state", None) or "").upper()} | {(a.state or "").upper() for a in await _served_areas(db, user, 50)}
    agent_states.discard("")
    return (sorted(town_states - agent_states) if town_states and agent_states and not (town_states & agent_states) else []), agent_states


_LICENSE_NOTE = re.compile(r"\bnot licensed in\b|\blicensed in [^.]{0,60}\bnot\b|\b(?:introduce|refer) you to (?:a|an)\b|\bconnect you with (?:a|an)\b", re.I)


def license_note(agent_states, outside) -> str:
    """The sentence an article about another state carries when the agent said they hold no licence there."""
    home = state_names(sorted(agent_states))
    return (f"One thing to know: I'm licensed in {home}, not in {state_names(outside)}, so for a home there I'll introduce you to an agent "
            f"who is licensed in {state_names(outside)}.") if home else (
            f"One thing to know: I'm not licensed in {state_names(outside)}, so for a home there I'll introduce you to an agent who is.")


def ensure_license_note(article: dict, sentence: str) -> bool:
    """Plain code's last word: the article says the agent is not licensed there, even if no earlier step wrote it.
    Added to the closing (or the last section). Returns True when it had to add it."""
    if not sentence or _LICENSE_NOTE.search(bfc._plain(_article_text(article))):
        return False
    closing = article.get("closing")
    if isinstance(closing, dict) and isinstance(closing.get("paragraphs"), list):
        closing["paragraphs"].append(sentence)
        return True
    sections = [x for x in article.get("sections") or [] if isinstance(x, dict)]
    if sections:
        sections[-1].setdefault("blocks", []).append({"type": "paragraph", "text": sentence})
        return True
    return False


# Pages we read on the web (this run's research and what earlier runs stored) - as opposed to our own MLS data, the
# agent's own words and the facts verified by hand.
WEB_READ = ("web_research", "state_bank", "evidence_bank", "idx_research")

_PHONE = re.compile(r"(?<![\d$])(?:\+?1[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?:\s*(?:x|ext\.?)\s*\d+)?(?!\d)", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_OFFICE_ADDRESS = re.compile(
    r"\b\d{1,5}\s+(?:[NSEW]\.?\s+)?(?:[A-Z][\w'.]*\s+){1,4}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Pike|Way|Highway|Hwy|Place|Pl|Court|Ct|"
    r"Circle|Cir|Parkway|Pkwy|Plaza|Square|Sq|Terrace|Ter)\b\.?(?:,?\s*(?:Room|Rm\.?|Suite|Ste\.?|Floor|Fl\.?|Unit|Building|Bldg\.?)\s*#?\w+)?")
_CONTACT_KINDS = ((_PHONE, "a phone number"), (_EMAIL, "an email address"), (_OFFICE_ADDRESS, "a street address"))
_CONTACT_NOTE = " (Contact details are left out on purpose: send the reader to this page for them.)"


def contact_details(text: Optional[str]) -> list[tuple[str, str]]:
    """[(what was found, what kind)] - phone numbers, email addresses and street addresses in a piece of text.
    Web addresses in links are not looked at."""
    plain = re.sub(r"\]\([^)\s]+\)|https?://\S+", "]", text or "")
    return [(m.group(0), kind) for rx, kind in _CONTACT_KINDS for m in rx.finditer(plain)]


def without_contact_details(text: Optional[str], keep: str = "") -> tuple[str, int]:
    """The same text with phone numbers, email addresses and street addresses taken out, with the words that led to
    them ("at 820 Mercer Street, Room 202", "(610-555-0100)"). `keep`: text whose own details stay (the agent's brief,
    our own records). Returns (text, how many were removed). Owner 11/15: "we shouldn't have info like phone number
    and email - just say where to get it"."""
    out, n = text or "", 0
    for found, _kind in contact_details(out):
        if found in (keep or "") or found not in out:
            continue
        if found.startswith("(") and ")" not in found:        # "(610-555-0100": the bracket belongs to the sentence
            found = found[1:]
        tok = re.escape(found)
        for pattern in (r"\s*\([^()]{0,24}?" + tok + r"[^()]{0,12}?\)", r"\s+(?:at|on|to|via|by calling|by emailing)\s+" + tok, r"\s*" + tok):
            out, hit = re.subn(pattern, "", out, count=1)
            if hit:
                n += 1
                break
    if n:
        out = re.sub(r"\s+([,.;:!?])", r"\1", out)
        out = re.sub(r",\s*,", ",", out)
        out = re.sub(r":\s*([.;])", r"\1", out)
        out = re.sub(r"\(\s*\)", "", out)
        out = re.sub(r"[ \t]{2,}", " ", out).strip()
    return out, n


def contact_detail_claims(text: str, ledger: list[dict], brief: Optional[dict] = None) -> list[tuple[str, str]]:
    """[(sentence, what kind)] - sentences of the article that print a phone number, an email address or a street
    address that is not the agent's own (typed in the brief) and not from our own records (a sale's address in the
    ledger). The writer is told to leave them on their page; this is the check that it did."""
    ours = " ".join(str(it.get("claim") or "") for it in ledger or []) + " " + " ".join(
        " ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v or "") for v in (brief or {}).values() if not isinstance(v, dict))
    out = []
    for sentence in _SENTENCES.split(text or ""):
        t = re.sub(r"^(?:#{2,3} |- )", "", sentence.strip())
        if not t or t.startswith("[Ask-me line"):
            continue
        kinds = [kind for found, kind in contact_details(t) if found not in ours]
        if kinds:
            out.append((t, kinds[0]))
    return out


def broadened_protections(text: str, protections: list[dict]) -> list[tuple[str, dict]]:
    """[(sentence, protection)] - a protection whose wording carries a limit (`scope`: which contracts, which homes) is
    in the article, but no sentence that mentions it keeps that limit. The first such sentence is returned."""
    out = []
    for p in protections or []:
        if not p.get("scope"):
            continue
        about = []
        for sentence in _SENTENCES.split(text or ""):
            t = re.sub(r"^- ", "", sentence.strip())
            if t and not t.startswith(("#", "[Ask-me line")) and len(t.split()) >= 7 and re.search(p["check"], bfc._plain(t), re.I):
                about.append(t)
        if about and not any(re.search(p["scope"], bfc._plain(t), re.I) for t in about):
            out.append((about[0], p))
    return out


_COUNT_OF_TIME = (r"(?:\d+|" + "|".join(tk._WORD_NUMBERS) + r")\s*(?:\(\d+\)\s*)?(?:business\s+|calendar\s+|working\s+)?(?:days?|weeks?|months?)")
_LEAD_NUMBER = re.compile(
    r"(?:\b(?:at least|a minimum of|no (?:later|fewer|less) than|about|around|roughly|up to|a full)\s+)?\b" + _COUNT_OF_TIME
    + r"\s+(?P<how>before|prior to|ahead of|in advance of)\b", re.I)


def soften_unconfirmed(sentence: str) -> Optional[str]:
    """The same sentence without its lead-time number: "at least two weeks before the scheduled settlement date" ->
    "well before the scheduled settlement date". None when the sentence has no lead time worded that way (a fee, a
    processing time) - then it can only be removed."""
    def swap(m):
        how = m.group("how").lower()
        return "well " + ("before" if how in ("before", "prior to") else how)
    out, n = _LEAD_NUMBER.subn(swap, sentence or "")
    return out if n else None


# What plain code can put right by itself when every earlier fix left it in the text: kind -> (how to reword the
# sentence, or None when it can only be removed; what the agent's report says was done).
def _settle_how(p: dict) -> Optional[str]:
    if p["kind"] == "unconfirmed_number":
        return soften_unconfirmed(p["quote"])
    if p["kind"] == "contact_detail":
        softer, n = without_contact_details(p["quote"])
        return softer if n and len(softer.split()) >= 4 else None
    if p["kind"] == "protection_scope":
        return p.get("says")
    return None


_SETTLED = {"unconfirmed_number": "A township number we have read only once", "contact_detail": "A phone number, email or office address",
            "protection_scope": "A rule stated more widely than it goes", "clock_time": "A train or bus clock time"}


def settle_leftovers(article: dict, find_problems) -> tuple[int, list[dict]]:
    """LAST step for what plain code can settle by itself and every earlier fix left in the text - a township number
    read only once, a phone number or office address, a protection stated without its limits: the sentence is reworded
    by code, or removed when it can't be - the agent is never handed it to fix (owner 11/15: "it comes out and it's
    actually done"). Each change is guarded like every other fix: one that would break another check (the only sentence
    naming a required certificate) or leave a broken sentence is undone, and only then does the flag stay.
    `find_problems()` -> code_problems for the article as it is now. Returns (applied, issues)."""
    applied, out = 0, []
    counts = lambda: _guard_counts(_article_text(article), find_problems(), missing=True)
    for _round in range(8):
        left = [p for p in find_problems() if p["kind"] in _SETTLED and p["quote"] and p["quote"] not in {o.get("quote") for o in out}]
        if not left:
            break
        p = left[0]
        softer = _settle_how(p)
        target = p["quote"] if softer else _cut_target(article, p["quote"])
        still = {"kind": p["kind"], "severity": "high", "quote": p["quote"], "problem": p["note"], "fixed": False, "code": True}
        if not target:
            out.append(still)
            continue
        n, done = _apply_guarded(article, [{"kind": p["kind"], "severity": "high", "quote": p["quote"], "find": target, "replace": softer or "",
                                            "problem": _SETTLED[p["kind"]] + " - " + ("reworded before saving." if softer else "removed before saving.")}], counts)
        applied += n
        out += done if done else [still]
    _tidy_spaces(article)
    return applied, [o for o in out if o.get("fixed")]


settle_unconfirmed = settle_leftovers       # the name it had while it knew one kind


_WALK_TIME = re.compile(r"\b\d+[- ]minute walk\b|\bwalking distance\b|\bwalk(?:able)? to\b", re.I)
_SHARE = re.compile(r"\b\d{1,3}(?:\.\d)?\s?%|\balmost no\b|\bnearly all\b|\bmost homes\b", re.I)


# "the 7:48 AM train", "reaches Suburban Station at 8:28", "at :51 past the hour", "at :30".
_CLOCK_TIME = re.compile(r"(?<![\w:.$/])(?:[1-9]|1[0-2]):[0-5]\d(?![\d:%])(?:\s*(?:a\.?m\.?|p\.?m\.?)(?![a-z]))?|(?<![\w:.$/]):[0-5]\d(?![\d:%])", re.I)
_TRANSIT_WORD = re.compile(r"\btrains?\b|\brail\b|\bbus(?:es)?\b|\bferr(?:y|ies)\b|\bstations?\b|\btrolleys?\b|\bsubway\b|\bSEPTA\b|"
                           r"\bNJ Transit\b|\bMARC\b|\bVRE\b|\bMetro(?:rail)?\b|\bAmtrak\b|\bPATCO\b|\btimetable|\bdepart", re.I)


def clock_time_claims(text: str, brief: Optional[dict] = None) -> list[str]:
    """Sentences that print a train, bus or ferry clock time. Grader round 10 (11/15): "From Bryn Mawr, the 7:48 AM
    weekday train reaches Suburban Station at 8:28" - 7:48 is Rosemont's row of that timetable; Bryn Mawr's is 7:50.
    The right timetable, read one row off: no later step can catch that without reading the timetable again, and the
    times change with every new one - so the article gives the ride's length and how often, and the timetable's link
    has the times. A time the agent typed in their own brief ("I take the 7:15 from Wayne") is theirs and stays."""
    ours = " ".join(" ".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v or "") for v in (brief or {}).values() if not isinstance(v, dict))
    out = []
    for sentence in _SENTENCES.split(text or ""):
        t = re.sub(r"^(?:#{2,3} |- )", "", sentence.strip())
        if not t or t.startswith("[Ask-me line"):
            continue
        plain = re.sub(r"\]\([^)\s]+\)|https?://\S+", "]", t)
        times = [m.group(0).strip() for m in _CLOCK_TIME.finditer(plain)]
        if times and _TRANSIT_WORD.search(plain) and not all(x in ours for x in times):
            out.append(t)
    return out


def walk_time_claims(text: str) -> list[str]:
    """Sentences that give our distance shares as walking time (grader 11/14, High: "99% of homes within a 15-minute
    walk" - the figure is 0.6 miles in a straight line, and streets, creeks and highways can make the walk far longer).
    The agent's own opinion about walking ("being able to walk to the train is worth more than a bedroom") has no
    share in it and is left alone."""
    out = []
    for sentence in _SENTENCES.split(text or ""):
        t = sentence.strip()
        if t and _SHARE.search(t) and _WALK_TIME.search(t) and not re.search(r"straight line", t, re.I):
            out.append(re.sub(r"^(?:#{2,3} |- )", "", t))
    return out


def drop_repeated_sentences(article: dict, min_words: int = 8) -> list[str]:
    """A sentence the body already said, word for word, is removed where it comes back (11/13 own grading:
    "repeated sentences" - the model, or a repair that adds a line the article already had). Paragraphs only:
    the at-a-glance box and bullets may repeat a key number on purpose. Returns what was removed."""
    seen: set[str] = set()
    removed: list[str] = []

    def clean(text):
        if not isinstance(text, str):
            return text
        out = text
        for sent in bfc._sentences(text):
            key = " ".join(re.sub(r"[^a-z0-9 ]", " ", bfc._plain(sent).lower()).split())
            if len(key.split()) < min_words:
                continue
            if key not in seen:
                seen.add(key)
                continue
            at = out.rfind(sent)
            if at >= 0:
                out = (out[:at].rstrip() + " " + out[at + len(sent):].lstrip()).strip()
                removed.append(sent)
        return out

    article["intro"] = [p for p in (clean(p) for p in article.get("intro") or []) if isinstance(p, str) and p.strip()]
    for sec in article.get("sections") or []:
        for b in (sec.get("blocks") or []) if isinstance(sec, dict) else []:
            if isinstance(b, dict) and b.get("type") in ("paragraph", "callout", "quote") and b.get("text"):
                b["text"] = clean(b["text"])
    closing = article.get("closing") or {}
    if closing.get("paragraphs"):
        closing["paragraphs"] = [p for p in (clean(p) for p in closing["paragraphs"]) if isinstance(p, str) and p.strip()]
    return removed


def _problem_line(p: dict) -> str:
    return f"{p['kind']}: " + (f"\"{p['quote']}\" - " if p["quote"] else "") + p["problem"]


# What a deletion or a clumsy rewrite leaves behind: a space before punctuation (". , so you may face"), a comma
# straight into a full stop, a sentence that ends on a joining word, empty brackets.
_FRAGMENT = re.compile(r"^[,;:.!?](?= )|(?<=\S) [,;.!?](?=\s|$)|[,;:]\s*[.!?](?=\s|$)|\b(?:and|or|but|so|which|because|the|a|an)[.!?](?=\s|$)|\(\s*\)", re.M)


def _guard_counts(text: str, problems: list[dict], missing: bool = True) -> dict:
    """How many confirmed problems of each kind the text has right now (and how many broken-sentence marks) -
    a fix that makes any of these numbers go up is undone."""
    counts = {"_fragments": len(_FRAGMENT.findall(text or ""))}
    for p in problems:
        if missing or not p["missing"]:
            counts[p["kind"]] = counts.get(p["kind"], 0) + 1
    return counts


def _apply_guarded(article: dict, fixes: list[dict], counts) -> tuple[int, list[dict]]:
    """Apply find/replace fixes ONE at a time, each one checked: a fix that breaks a check that passed, or
    leaves a broken sentence behind, is undone and stays an open issue. Engine audit 11/14: only the grader's
    fixes had this guard (added 11/01 after a rewrite made an article worse) - the fact-check's and the
    repair's were applied blind. `counts()` -> _guard_counts for the article as it is now."""
    import copy
    applied, out = 0, []
    before = counts()
    for f in fixes:
        find = (f.get("find") or "").strip()
        was_there = bool(find) and " ".join(find.split()) in " ".join(_article_text(article).split())
        saved = copy.deepcopy(article)
        n, done = _apply_fixes(article, [f])
        if n:
            after = counts()
            if any(after.get(k, 0) > before.get(k, 0) for k in after):
                article.clear()
                article.update(saved)
                out.append({**done[0], "fixed": False, "was_there": True, "undone": True})
                continue
            before = after
        applied += n
        out += [{**d, "was_there": was_there} for d in done]
    return applied, out


async def _grade_and_fix(*, article: dict, ledger_text: str, brief: dict, brief_msg: str, inserted: list[str], spec: dict, meter: "_Meter",
                         find_problems, second_opinion: Optional[list[str]] = None, unapplied: Optional[list[dict]] = None,
                         unanswered: Optional[list[dict]] = None) -> tuple[list[dict], list[str], list[dict], int]:
    """The engine's own grader, its last step: code checks + an AI grade against bfc.PASS_LIST, one
    targeted rewrite of only the failing sentences, a re-check of those items. Only code-confirmed
    rule breaks are cut; anything else still failing is flagged to the agent.

    Guards (11/01 round 6 - the first version made articles worse): the AI grade never cuts on its
    own; a deletion must be whole sentences; and a rewrite that breaks a code check that passed
    (e.g. drops the "who pays" part of a reader protection) is undone.
    Returns (pass_list, nice_to_have, issues, applied)."""
    def checks() -> dict[str, list[dict]]:
        found: dict[str, list[dict]] = {}
        for p in find_problems():
            found.setdefault(p["pass"], []).append({"quote": p["quote"], "why": p["kind"] if p["quote"] else p["problem"].split(" - add", 1)[0],
                                                    "fix": p["problem"], "cut": p.get("cut_final")})
        return found

    def counts() -> dict:
        return _guard_counts(_article_text(article), find_problems(), missing=True)

    def asked(ids: Optional[list[str]]) -> str:
        """The fact-check's two open questions for the grader - a SECOND opinion, so one model's doubt alone
        never flags or rewrites a sentence (11/01: acting on a single AI verdict made articles worse)."""
        out = ""
        if second_opinion and (ids is None or "facts" in ids):
            out += ("\n\nTHE FACT-CHECKER FOUND NO SUPPORT FOR THESE SENTENCES, and they are still in the article. Check each against the "
                    "ledger and the brief: fail `facts` for each one that really has no support there (quote it), and say nothing about the "
                    "ones that are supported:\n" + "\n".join(f"- \"{q}\"" for q in second_opinion))
        if unapplied and ids is None:
            out += ("\n\nTHE FACT-CHECKER FLAGGED THESE SENTENCES BUT ITS OWN FIX COULD NOT BE USED (it broke another rule, or the "
                    "sentence). Check each: if the problem is real, fail the pass-list item it belongs to, quote the sentence and give a "
                    "fix that keeps every rule on the pass list; say nothing about a sentence that is fine as it stands:\n"
                    + "\n".join(f"- \"{u['quote']}\" - {u['problem']}" for u in unapplied))
        if unanswered and (ids is None or "asked" in ids):
            out += ("\n\nTHE FACT-CHECKER SAYS THESE POINTS FROM THE BRIEF ARE NOT ANSWERED. Check each against the article: fail `asked` for "
                    "each one it really leaves open (quote \"\", and in `fix` give the answer from the ledger - or, when the ledger has nothing, "
                    "one honest first-person sentence saying so with an offer to get it):\n"
                    + "\n".join(f"- {b['point']} ({b['status']}; {b['note']})" for b in unanswered))
        return out

    async def grade(ids: Optional[list[str]], code: dict, stage: str) -> dict:
        msg = (f"PASS LIST:\n{_pass_list_text(ids)}\n\nAgent's brief:\n{brief_msg}\n\nINSERTED ELEMENTS:\n" + "\n".join(f"- {x}" for x in inserted)
               + ("\n\nAUTOMATED CODE CHECKS FOUND (confirmed - include them):\n" + "\n".join(f"- {k}: \"{p['quote']}\" ({p['why']})" for k, ps in code.items() for p in ps) if code else "")
               + asked(ids)
               + f"\n\n== ARTICLE ==\n{_article_text(article)}")
        started, usage = time.monotonic(), _new_usage()
        try:
            got = await _run_tool_call(model=spec["check_model"], system=_GRADE_SYSTEM, user_msg=msg, tools=_CHECK_TOOLS, tool_name="submit_grade",
                                       max_tokens=8000, effort="high", usage=usage, shared=_ledger_block(ledger_text))
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
                   + "\n\nVERIFIED FACTS: the EVIDENCE LEDGER above - the only facts you may use."
                   + f"\n\n== ARTICLE ==\n{_article_text(article)}")
        started, usage = time.monotonic(), _new_usage()
        try:
            got = await _run_tool_call(model=spec["check_model"], system=_FIX_SYSTEM, user_msg=fix_msg, tools=_CHECK_TOOLS, tool_name="submit_fixes",
                                       max_tokens=8000, effort="medium", usage=usage, shared=_ledger_block(ledger_text))
            await meter.record("grade_fix", spec["check_model"], usage, started)
            fixes = [dict(f, kind="grader", severity="high", quote=f.get("find")) for f in writer._coerce(got.get("fixes"), list) if isinstance(f, dict)]
            n, done = _apply_guarded(article, fixes, counts)   # a rewrite that breaks a check or a sentence is undone
            applied += n
            issues += [d for d in done if d["fixed"]]
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
            for p in [p for p in still[pid] if p.get("code") and p.get("cut") and p["quote"]]:
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
                    "problem": f"{p['why']}. {p['fix']}".strip(), "fixed": False}
                   # what code can confirm is reported once, from the final text (see _run) - not twice
                   for pid, ps in still.items() for p in ps if not p.get("code")]
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
    # An article built on old MLS data states old numbers as the market (grader audit 11/13) - refused
    # where the sync is meant to be running, before anything is spent.
    from app.services import mls_freshness
    _fresh = await mls_freshness.check(db)
    if _fresh["level"] == "blocked":
        raise HTTPException(status.HTTP_409_CONFLICT, mls_freshness.blocked_message(_fresh, "article"))
    config = await ai_config_service.get_config(db)
    engine = engine if engine in ENGINES else config.get("blog_pipeline_default_engine", "max")
    await _gate(db, user, config, engine)
    return engine


async def run_pipeline(db: AsyncSession, user: User, brief: dict, *, engine: Optional[str] = None, persist_research: bool = True,
                       on_stage: Progress = None, overnight: bool = False, journal=None, spec_overrides: Optional[dict] = None) -> dict:
    """Research -> write -> fact-check. Returns generate_article's shape plus `verification`.
    `on_stage(name)` is awaited as each stage starts: "gathering", "research", "write", "fact_check", "finishing".
    `overnight`: the caller has set TRANSPORT to the half-price queue (Save usage mode). `journal`: see _Meter.
    `spec_overrides`: a side-by-side test only."""
    writer._require_key()
    config = await ai_config_service.get_config(db)
    engine = engine if engine in ENGINES else config.get("blog_pipeline_default_engine", "max")
    spec = {**config["blog_pipeline_engines"][engine], **(spec_overrides or {})}
    # A run picked up again after a restart already passed the allowance check when the agent pressed Write.
    if not (journal is not None and journal.resumed):
        await _gate(db, user, config, engine)
    estimate = float(config["blog_pipeline_reservation_estimate_usd"][engine])
    try:
        reservation = await reserve_global_and_emergency(
            db, "blog_pipeline_global", estimate,
            config["blog_pipeline_global_daily_cost_ceiling_usd"], config["blog_pipeline_global_monthly_cost_ceiling_usd"], config,
        )
    except AIRateLimitExceeded as e:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, e.user_message)

    meter = _Meter(db, user, config, journal)
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
    if overnight:
        result["verification"]["overnight"] = True
    return result


async def _stage(on_stage: Progress, name: str) -> None:
    if on_stage:
        try:
            await on_stage(name)
        except Exception:  # progress is cosmetic - never fail the article over it
            logger.warning("blog pipeline: progress update failed", exc_info=True)


_COUNTY_NAMED = re.compile(r"\b((?:[A-Z][A-Za-z.'-]+ ){1,3})County\b")


def county_named(names) -> Optional[str]:
    """'Delaware County' when exactly one county is what the agent named as the article's area - else None."""
    found = {m.group(0).strip() for n in names or [] for m in _COUNTY_NAMED.finditer(str(n))}
    return next(iter(found)) if len(found) == 1 else None


def is_county(name) -> bool:
    """'Chester County', 'Chester County, PA' - a county, never a town to look up, create or research."""
    return bool(_COUNTY_NAMED.search(str(name or "")))


def county_share(geo: Optional[dict], county: str) -> float:
    """How much of a town's homes (percent) sit in municipalities of this county, from our own records."""
    want, total = tk.norm_place(re.sub(r"\s+County$", "", county or "", flags=re.I)), 0.0
    for t in (geo or {}).get("townships") or []:
        if tk.norm_place(re.sub(r"\s+County$", "", str(t.get("county") or ""), flags=re.I)) != want:
            continue
        try:
            total += float(t.get("sort", t.get("share")) or 0)
        except (TypeError, ValueError):
            total += 0.5                                   # "under 1"
    return total


def towns_inside(served: list, geos: dict, county: str, limit: int = 5, at_least: float = 50.0) -> list:
    """The agent's towns that sit mostly inside the county, the most-inside first. `geos`: {slug: geo profile}."""
    scored = [(county_share(geos.get(a.slug), county), a) for a in served]
    return [a for share, a in sorted(scored, key=lambda x: -x[0]) if share >= at_least][:limit]


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
    # Related content (owner 11/15): the agent's video, sales and listings are closing cards placed by code. The AI
    # is told about a sale or a listing only when the article is ABOUT them - then they are the topic, not an extra.
    related = writer.related_blocks(brief, sales, listings)
    text_sales = sales if writer.about_own(brief, "sales") else []
    text_listings = listings if writer.about_own(brief, "listings") else []
    chosen_sales = picked_sales(brief, text_sales)
    state = (user.state if hasattr(user, "state") and user.state else None) or next((s.get("state") for s in sales if s.get("state")), None) or "PA"

    # --- 1. What we already know (free) ---------------------------------------------------
    # A county is never looked up as a town (11/15: typing "Chester County" had made a town record by that name, and the
    # article treated the county as a town with no homes) - it is handled by the county rules below.
    areas = await tk.find_areas(db, slugs=list(brief.get("area_slugs") or []), names=[n for n in brief.get("areas") or [] if not is_county(n)], state=state)
    areas = [a for a in areas if not is_county(a.name)][:4]
    # The article's towns decide the state, not the agent's profile: 11/01 a PA agent writing about
    # Frederick, MD would otherwise get Pennsylvania's transfer tax, programs and research.
    town_states = {(a.state or "").upper() for a in areas if a.state}
    # Towns outside every state the agent works in (their own state + their served towns'): the
    # article never claims local experience there, but keeps the agent's chosen closing - owner
    # 11/01: "I'll start your search" stays (the agent may hand it to a referral agent).
    out_of_area, agent_states = await out_of_area_states(db, user, town_states)
    # The agent told the writer they hold no licence there (asked only when the town is outside their states): the
    # article offers an introduction to an agent who does, never their own services (grader 11/15).
    not_licensed = out_of_area if out_of_area and brief.get("licensed_there") is False else []
    if len(town_states) == 1:
        state = next(iter(town_states))
    # A general article ("buying a home in the Philly suburbs", "PA closing costs") names no town:
    # the agent's own served towns become REGIONAL context - a few dated market numbers and the
    # basics per town for examples - and research focuses on state/county-level facts instead.
    region_words = ("suburb", "county", "main line", "region", "area", "metro", "valley", "shore", "greater ", "philly", "philadelphia")
    named = [str(a) for a in (brief.get("areas") or [])]
    regional = not areas and (not named or all(any(w in n.lower() for w in region_words) for n in named))
    about_county = county_named(named) if regional else None
    if regional and about_county:
        # An article about ONE county: only the agent's towns that sit mostly inside it are its examples (11/15: the
        # first five served towns were used whatever their county).
        served = list(await _served_areas(db, user, 50))
        geos = {}
        for a in served:
            try:
                geos[a.slug] = (await tk.town_dossier(db, a, include_bank=False, mls=False)).get("geo")
            except Exception:
                logger.warning("blog pipeline: county check failed for %s", a.slug, exc_info=True)
        areas = towns_inside(served, geos, about_county)
    elif regional:
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
    # A municipality's resale requirements change rarely. What an earlier article read on the municipality's own
    # page is kept on the town's record - a seller article reuses it, so its look-ups go to what is still unknown
    # (engine audit 11/14: Premium saved every finding to the town's record and never read one back).
    if not regional and _for_sellers(brief) and not spec.get("reuse_bank"):
        for d, a in zip(dossiers, areas):
            have = {i.get("claim") for i in d["items"]}
            d["items"] += [i for i in tk.bank_items(a) if i.get("topic") == "regulation" and i.get("source_url") and i.get("claim") not in have
                           and any(rx.search(str(i.get("claim") or "")) for _l, rx in _LOCAL_SALE_REQUIREMENTS)]
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
    # One township's own rule is not a state-wide fact: it belongs to the articles whose homes sit in that township
    # (grader round 7 - see tk.municipalities). Older ones stored state-wide are sorted out here, on the way in.
    munis = [] if regional else tk.municipalities(profiles)
    muni_known: list[dict] = []
    try:
        state_known, from_state = tk.split_state_items(state_known, munis)
        if persist:
            # township facts stored state-wide before 11/15 move to their municipality (kept with the day they were read)
            for i in from_state:
                m = tk.muni_of({**i, "town": ""}, munis)
                if m:
                    await tk.save_muni_items(db, state, m, [i])
        muni_known = tk.merge_items(await tk.muni_items(db, state, munis), from_state)
    except Exception:
        logger.warning("blog pipeline: municipal facts lookup failed", exc_info=True)
    known_items += state_known + muni_known
    try:
        rate_items = await tk.freddie_mac_rate_items()
    except Exception:
        logger.warning("blog pipeline: Freddie Mac PMMS fetch failed", exc_info=True)
        rate_items = []
    # Transfer tax in numbers - only what the hand-verified table holds for this place, worded as an estimate
    # (owner 11/13: "stay safe but be informative"; grader audit findings 9 and 14 wanted real dollars).
    tax_facts, house_sold = None, None
    try:
        if len(town_states) <= 1:
            sold = None
            if mls_ok and len(dossiers) == 1 and not regional:
                snap0 = dossiers[0].get("sales_snapshot") or {}
                cur = snap0.get("current") or {}
                if cur.get("median_close_price"):
                    sold = {"price": cur["median_close_price"], "count": cur.get("closed_count"), "from": cur.get("from"),
                            "to": cur.get("to"), "town": areas[0].name}
                hs = (cur.get("by_type") or {}).get("house")
                if hs and snap0.get("data_through"):
                    house_sold = {"price": hs["median_close_price"], "count": hs["count"],
                                  "window": f"the {snap0.get('window_days', 90)} days ending {snap0['data_through']}"}
            tax_facts = tk.transfer_tax_facts(state, [] if regional else profiles, sold)
    except Exception:
        logger.warning("blog pipeline: transfer tax facts failed", exc_info=True)
    tax_rates = (tax_facts or {}).get("rates") or None
    try:
        abatement_items = tk.philadelphia_abatement_items(state, [] if regional else profiles, brief) if len(town_states) <= 1 else []
    except Exception:
        logger.warning("blog pipeline: abatement facts failed", exc_info=True)
        abatement_items = []
    # Buying articles get a worked payment example, computed here from real listings + the rate.
    pay_ex = None
    if mls_ok and areas and not regional and wants_payment_example(brief):
        try:
            entry = await entry_price_profile(db, [geo_scope.scope_for_market_area(a) for a in areas],
                                              house_near=(house_sold or {}).get("price") if payment_basis(brief) == "house" else None)
            # Jumbo or not is judged against the county's own FHFA limit, never the national baseline
            # (11/01 round-11 grader, Ashburn/Loudoun). No single known limit -> the card says it depends.
            states = {(a.state or "").upper() for a in areas}
            cll = tk.county_conforming_limit(next(iter(states)), sorted({c for p in profiles for c in p.get("counties") or []})) if len(states) == 1 else None
            pay_ex = payment_example(entry, await tk.freddie_mac_latest(), [a.name for a in areas], conforming_limit=(cll or {}).get("limit"),
                                     limit_area=_and_list((cll or {}).get("counties") or []) or None, basis=payment_basis(brief),
                                     sold_house=house_sold, transfer_tax=tax_facts,
                                     fee=example_fee(dossiers[0].get("cost")) if len(dossiers) == 1 else None, fha=tk.fha_limits(),
                                     fees=example_fees(dossiers[0].get("cost")) if len(dossiers) == 1 else None,
                                     homes_for_sale=(dossiers[0].get("sales_snapshot") or {}).get("active_count") if len(dossiers) == 1 else None)
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
        # Our MLS numbers first: they sit last in the dossier, so the 30-line cut used to hide them and research
        # went looking for a median price we already had (11/14).
        held = ([i for i in d["items"] if i["origin"] == "mls_live"][:12]
                + [i for i in d["items"] if i["origin"] != "mls_live" and (i["origin"] != "idx_research" or i["topic"] in ("schools", "market", "transit", "downtown"))])
        known = [f"  - {i['claim']}" for i in held][:36]
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
    if tax_facts:
        rates_note += ("\n\nThe realty transfer tax for this place is already supplied from the government's own page - do NOT research "
                       "transfer-tax rates (the article may not state any rate but the supplied one).")
    # 11/01 round-10 grader: a Lancaster article linked the 2025-2026 school millage table after the
    # 2026-2027 one was out. Last year's editions are also dropped in code (tk.drop_stale_editions).
    rates_note += (f"\n\nToday is {datetime.now(timezone.utc).date().isoformat()}. For anything published by year (tax-rate or millage tables, "
                   "school-year or fiscal-year documents, program guidelines), use the NEWEST edition - check whether this year's version is "
                   "already out (school and fiscal years start July 1) and never cite an edition whose year has ended.")
    if engine == "max":
        # Engine audit 11/14: all four test runs ended "tool limit reached" with 7-9 facts found. The task asked for
        # everything at once - including things we already hold (it went looking for a median price "from an official
        # source", transfer-tax rates the article may not state, mortgage rates it was told not to research) - and
        # never said how many look-ups there were. Now: a budget, an order, and a list of what NOT to spend it on.
        n_search, n_fetch = int(spec["research_searches"]), int(spec.get("research_fetches") or 0)
        task = (f"You have {n_search} web searches" + (f" and {n_fetch} page reads" if n_fetch else "") + " for this article - no more. Spend "
                "them in THIS order, ONE look-up at a time - read each result before you choose the next, and never send several "
                "searches at once:\n"
                "  1. the REQUIRED LOOK-UPS below, if there are any (the official page itself);\n"
                "  2. each question or point in the topic and key points that neither the ALREADY KNOWN facts nor anything listed as "
                "already supplied answers - one focused search per point, aimed at the official source for it (the city or township, "
                "its water or code department, the state agency). EVERY key point gets its first look-up before ANY point gets a "
                "second one: a point left with no look-up at all is the worst outcome (a relocation article used four look-ups on "
                "income tax and none on the neighborhoods its title promised);\n"
                "  3. the official page that publishes property-tax rates for the relevant township(s)/school district(s) (to link - "
                "articles never quote rates), if it isn't already known;\n"
                "  4. only with look-ups left over: local developments from the last 12 months that matter to this topic.\n"
                "Do NOT spend a look-up on: home prices, sales counts, days on market or homes for sale (our Bright MLS numbers under "
                "ALREADY KNOWN are the source for those - there is nothing more official to find), mortgage rates, or anything listed as "
                "already supplied below. Report a fact when a result shows you the official page's own words for it; when a result only "
                "points to the right page, open that page with a page read. What you could not confirm goes in not_found.")
    else:
        task = ("Fill ONLY the gaps: the official page publishing property-tax rates for the relevant township(s)/school district(s) if missing (to link, not to quote), the current 30-year "
                "mortgage rate (Freddie Mac PMMS latest week) if buying, selling or the market is discussed, and at most one fact specific "
                "to this topic that the known facts don't cover. Keep it tight.")
    # What we already hold is not a look-up: with the City's abatement facts supplied, research was still told to
    # "do these FIRST: ... the municipality's official abatement page" and, two lines later, not to research it.
    required = "\n".join(line for line in _coverage(brief, 1).split("\n")
                         if line and not (abatement_items and line.startswith("This article involves a PROPERTY-TAX ABATEMENT")))
    # A mailing town can sit in several municipalities, each with its own resale rules (a Media address: seven) -
    # more than the look-ups can cover. Biggest share of the homes first, and the rest are named as not checked.
    if required and _for_sellers(brief) and not regional:
        to_check = sorted(({"name": t["name"], "county": t.get("county"), "share": float(t.get("sort", t.get("share")))} for p in profiles for t in p.get("townships") or []
                        if share_at_least(t, 8)), key=lambda m: -m["share"])[:5]
        if len(to_check) > 1:
            required += ("\nMUNICIPALITIES TO CHECK for resale requirements, in THIS order (largest share of the homes first) - first one search each "
                         "for its resale / occupancy certificate, opening a page only where the search result doesn't settle it; then, with "
                         "searches left, one each in the same order for a sewer or water authority's own sale certification "
                         "(\"<municipality> <county> sewer authority certification sale of property\" - always with the county): "
                         + "; ".join(f"{m['name']}{(', ' + m['county']) if m.get('county') else ''} (about {round(m['share'])}% of the homes)" for m in to_check)
                         + ". Skip any whose requirement is already listed under ALREADY KNOWN. For every municipality you did not get to, add "
                         "'resale requirements not checked for <municipality>' to not_found.")
    # Municipal procedure pages read under the old "one deadline is enough" rule get one more read (tk.recheck_pages),
    # on a seller article - where those deadlines are what the reader acts on - and only when there are page reads.
    again_pages = tk.unconfirmed_pages(muni_known) if _for_sellers(brief) and int(spec.get("research_fetches") or 0) else []
    if again_pages:
        required += ("\nRE-READ THESE PAGES, one page read each, after the look-ups above: each is an official procedure page we hold a deadline or "
                     "fee from that has been read only once - a number is only published after a second reading agrees. Open it and report EVERY "
                     "deadline, lead time and fee it gives, exactly as the page words them, as a new item: "
                     + "; ".join(f"{p['url']} ({p['municipality']})" for p in again_pages) + ".")
    research_msg = (
        f"Today is {today}. Agent: {profile['name']} ({profile['brokerage']}), market: {profile['market'] or state}.\n"
        f"ARTICLE TOPIC: {brief.get('topic', '').strip()}\n"
        + (f"Goal: {brief['goal']}\n" if brief.get("goal") else "")
        + (f"Audience: {', '.join(brief['audiences'])}\n" if brief.get("audiences") else "")
        + (f"Key points: {brief['key_points']}\n" if brief.get("key_points") else "")
        + (f"\nREGION the article covers: {', '.join(named)}" if regional and named else "")
        + (f"\n{state} STATE-WIDE FACTS ALREADY KNOWN (don't re-research unless they may have changed):\n" + "\n".join(f"  - {i['claim']} ({i.get('as_of')})" for i in state_known[:40]) + "\n" if state_known else "")
        + ("\nMUNICIPAL FACTS ALREADY KNOWN (each is ONE township's or borough's own rule - it applies only to homes in that municipality; "
           "don't re-research it unless it may have changed):\n"
           + "\n".join(f"  - [{i.get('municipality')}] {i['claim']} ({i.get('as_of')})" for i in muni_known[:30]) + "\n" if muni_known else "")
        + "\nTOWNS:\n" + ("\n\n".join(town_lines) if town_lines else "(no towns with stored research)")
        + (f"\n\nTowns with NO stored research (research their basics: municipality/township, county, school district(s), rail/transit, downtown): {', '.join(unknown_towns)}" if unknown_towns else "")
        + f"\n\nYOUR TASK: {task}{general}{rates_note}"
        + (f"\n\nREQUIRED LOOK-UPS - do these FIRST, before any other search, and open the official page itself rather than "
           f"relying on a search result's summary:\n{required}" if required else "")
        + ("\n\nPhiladelphia's property-tax abatement rules (the declining schedule for new construction, the separate rehab program, "
           "the Homestead Exemption rule) are ALREADY SUPPLIED from the City's own pages - do NOT research them." if abatement_items else "")
    )
    research_items: list[dict] = []
    not_found: list[str] = []

    def _take(found: dict) -> list[str]:
        # The model sometimes returns a list as a JSON-encoded string. Read one character at a time, that
        # lost the whole "could not verify" list (11/13 Fishtown: the writer was told "[; \"; O; f; f; i; c; i").
        for it in research_list(found.get("items")):
            if isinstance(it, dict) and it.get("claim") and it.get("source_url"):
                research_items.append({"topic": it.get("topic") or "other", "claim": it["claim"].strip(), "source_name": it.get("source_name") or tk._domain(it.get("source_url")),
                                       "source_url": tidy_url(it["source_url"]), "as_of": it.get("as_of"), "confidence": it.get("confidence"), "town": it.get("town") or "general",
                                       "origin": "web_research", "read_on": datetime.now(timezone.utc).date().isoformat(),
                                       **({"jurisdiction": str(it["jurisdiction"])[:160]} if it.get("jurisdiction") else {})})
        return [x.strip() for x in research_list(found.get("not_found")) if isinstance(x, str) and x.strip()]

    def _form_gaps() -> list[str]:
        # a seller acts on these: a requirement we hold, or just found, with no "how far ahead" anywhere
        if not _for_sellers(brief) or not munis:
            return []
        found_here = [{**i, "municipality": m["name"]} for i in research_items if (m := tk.muni_of(i, munis))]
        return tk.lead_time_gaps(munis, muni_known + found_here)

    n_s, n_f = int(spec["research_searches"]), int(spec.get("research_fetches") or 0)
    try:
        found = await _research_call(model=spec["research_model"], user_msg=research_msg, searches=n_s, fetches=n_f, state=state, max_tokens=16000,
                                     effort=spec.get("research_effort"), usage=usage)
        not_found = _take(found)
        again = research_follow_up(not_found, usage.get("lookups"), n_s, n_f, extra=_form_gaps())
        if again:
            try:
                more = await _research_call(model=spec["research_model"], user_msg=research_msg + follow_up_message(again, research_items),
                                            searches=again["searches"], fetches=again["fetches"], state=state, max_tokens=16000,
                                            effort=spec.get("research_effort"), usage=usage)
                # what the first pass could not settle stays listed unless it was one of the items sent back
                not_found = [x for x in not_found if x not in again["gaps"]] + _take(more)
            except Exception:
                logger.warning("blog pipeline: the research follow-up pass failed - keeping the first pass", exc_info=True)
        await meter.record("research", spec["research_model"], usage, started)
    except HTTPException:
        # Research is an enrichment - the article can still be written from what we hold.
        await meter.record("research", spec["research_model"], usage, started, failed="research_incomplete")
        logger.warning("blog pipeline: research stage returned nothing", exc_info=True)
    except Exception:
        await meter.record("research", spec["research_model"], usage, started, failed="research_error")
        logger.warning("blog pipeline: research stage failed", exc_info=True)

    research_lookups = usage.get("lookups") or []
    # Nothing that contradicts the verified geography is used - or banked for the next article.
    # A municipal fact is used - and stored - only when its page is tied to THIS municipality's county (round 8).
    research_items, wrong_place = tk.only_this_jurisdiction(research_items, munis, muni_known)
    if wrong_place:
        logger.warning("blog pipeline: %d municipal fact(s) left out - not this county's page", len(wrong_place))
        not_found += wrong_place
    research_items, geo_scrubbed = tk.scrub_geography(research_items, profiles, known_geo)
    research_items = tk.drop_stale_editions(tk.drop_other_county_items(research_items, profiles))
    banked = 0
    if persist and research_items:
        town_names = {a.name.lower() for a in areas}
        # A fact about ONE of the article's municipalities is kept under that municipality (tk.save_muni_items) - not
        # under the mailing town, and not state-wide.
        by_muni: dict[str, tuple[dict, list[dict]]] = {}
        rest = []
        for i in research_items:
            m = tk.muni_of(i, munis)
            if m:
                by_muni.setdefault(tk.muni_scope(state, m), (m, []))[1].append(i)
            else:
                rest.append(i)
        for m, mine in by_muni.values():
            try:
                banked += await tk.save_muni_items(db, state, m, mine)
            except Exception:
                logger.warning("blog pipeline: municipal facts save failed for %s", m.get("name"), exc_info=True)
        research_kept, research_items = research_items, rest
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
        research_items = research_kept

    # Only what we are sure of is given to the writer as a number (owner 11/15). A municipal deadline, processing time
    # or fee that no second reading of its page has confirmed is replaced, in the writer's copy, by a note saying so:
    # the article then says the requirement exists and links the page. The store keeps the full wording.
    # A number the municipality's own official pages give differently (its web page says 10 business days, its code
    # says 30 days - round 11) is held back the same way, however often either page was read, with a note saying the
    # pages differ.
    held_back = 0
    for m in munis:
        mine = [i for i in muni_known + research_items if tk._muni_key(i.get("municipality")) == tk._muni_key(m["name"])]
        sure, disputed = tk.confirmed_specifics(mine), tk.disputed_specifics(mine)
        if disputed:
            logger.info("blog pipeline: %s's official pages disagree about %s - held back", m["name"], sorted(disputed))
        for i in mine:
            i["claim"], n = tk.mask_unconfirmed(i["claim"], sure, disputed)
            held_back += n
    held_back += hold_back_unplaced(research_items + [i for i in known_items if i.get("origin") in ("evidence_bank", "idx_research", "web_research")])
    if held_back:
        logger.info("blog pipeline: %d municipal number(s) held back - not yet confirmed by a second reading, or in dispute", held_back)

    brief_facts = []
    if not_licensed:
        brief_facts.append({"topic": "agent_brief", "claim": f"The agent's own answer: licensed in {state_names(sorted(agent_states))}, NOT licensed in "
                            f"{state_names(not_licensed)}. For a home in {state_names(not_licensed)} the agent offers to introduce the reader to an agent "
                            "licensed there.", "source_name": "The agent (brief)", "source_url": None,
                            "as_of": datetime.now(timezone.utc).date().isoformat(), "origin": "agent", "town": "general"})
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
        # A plan with no MLS reads nothing from the listings, not even to decide which rules to mention (owner, 11/16:
        # "it just can't use data from our database or the Bright MLS at all"). The lead-paint rule is then named
        # when the agent's own brief talks about older homes.
        older = await older_homes_share(db, [geo_scope.scope_for_market_area(a) for a in areas]) if areas and mls_ok else None
    except Exception:
        logger.warning("blog pipeline: older-homes share failed", exc_info=True)
        older = None
    protections = (bfc.protections_for(town_states or {(state or "").upper()}, brief, investment_article(brief), places=places, older_share=older)
                   + ([] if regional else tk.place_protections(profiles)))
    ordered = ([i for i in known_items if i["origin"] == "geo_verified"] + [i for i in known_items if i["origin"] == "mls_live"] + overlap
               + rate_items + calc_items + ((tax_facts or {}).get("items") or []) + abatement_items + federal + bfc.protection_items(protections, datetime.now(timezone.utc).date().isoformat())
               + _sale_items(text_sales, await sale_records(db, text_sales)) + _listing_items(text_listings) + brief_facts
               + research_items + [i for i in known_items if i["origin"] == "state_bank"]
               # stored research last, what the brief asks about first - the per-town cap cuts from the end
               + brief_first(trusted_stored_research([i for i in known_items if i["origin"] not in ("mls_live", "state_bank", "geo_verified")]), brief))
    ordered, scrubbed_more = tk.scrub_geography(ordered, profiles, known_geo)
    geo_scrubbed += scrubbed_more
    # Every item - saved state facts too, not just this run's research (11/01 Annapolis).
    ordered = tk.drop_stale_editions(tk.drop_other_county_items(ordered, profiles))
    ordered = fit_to_reader(ordered, brief)
    for i in ordered:
        if i.get("origin") in WEB_READ:
            cleaned, n_contact = without_contact_details(i.get("claim"))
            if n_contact:
                i["claim"] = cleaned + _CONTACT_NOTE
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
        card_transit(c, next((a.facts for a in areas if a.slug == c.get("slug")), None))
        # The town's stored AI blurb is unchecked marketing copy (10/24: "acclaimed dining",
        # "ideal setting for discerning families" - a fair-housing problem) - an article's card
        # carries only verified numbers, districts and places.
        c["summary"] = None
        # A card's lists are what we hold, not every district that reaches the town (11/14 grader: Media's card showed
        # three districts as if that were all of them).
        geo = next((d.get("geo") for d in dossiers if d["slug"] == c.get("slug")), None) or {}
        c["school_districts_partial"] = bool(geo.get("school_districts")) and not geo.get("districts_complete")
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
        inserted.append(f"A 'What it could cost' payment example card for a {writer._money(pay_ex['price'])} home (5%, 10% and 20% down; the numbers are the 'Payment example' ledger items), placed after the section about mortgages, budgets or costs - point to it in one sentence; don't redo its math. If you say why the example uses that price, give ONLY the reason in the 'Worked payment example' ledger item (\"{pay_ex['price_basis']}\") - never tie the price to another ledger number that happens to equal it.")
    inserted.append("A 'Sources & data notes' list at the end with every source and what each area's numbers cover.")
    if brief.get("area_slugs"):
        inserted.append("Links from each town's first mention to the agent's neighborhood guide for it (homes for sale, prices, schools).")
    from app.services import mls_freshness
    _fresh = await mls_freshness.check(db)
    _stale_note = ""
    if not _fresh.get("fresh_wording", True):
        _stale_note = (
            f"\nMLS DATA AGE: our MLS numbers were last updated {_fresh['current_through_label']}, {_fresh['age_days']} days "
            "before today. Give every MLS number (sales, prices, days on market, homes for sale, under contract) its date or "
            "window exactly as the ledger states it. Never write \"right now\", \"currently\", \"today\", \"this week\", "
            "\"latest\" or \"at the moment\" about any of them, and never call the homes-for-sale count current.\n")
    write_msg = (
        f"{brief_msg}\n\nToday's date: {today}.\n" + _stale_note
        + "\nINSERTED ELEMENTS (added around your text automatically):\n" + "\n".join(f"- {x}" for x in inserted) + "\n"
        + (f"\nWHAT THIS READER EXPECTS: {_coverage(brief, 2)}\n" if _coverage(brief, 2) else "")
        # Grader audit 11/13: asked about the commute to Wilmington and Philadelphia, the article gave a hospital's
        # distance; asked how much land you get, it said "compare listing by listing" while the ledger had lot sizes.
        + (f"\nTRANSFER TAX YOU MAY STATE (read on the government's own page - the only transfer-tax numbers allowed in this article): "
           f"{tax_facts['note']} Use it only where the article talks about closing costs, what a buyer or seller pays at settlement, or what "
           "a seller nets - never as a detour. Word every figure as an estimate (\"about\", \"currently\"), link the official page "
           f"({tax_facts['source_url']}), and say the settlement company gives the exact amount.\n" if tax_facts else "")
        + ("\nANSWER WHAT WAS ASKED: every question or point in the brief's topic and key points gets a direct answer from the ledger, "
           "in the section a reader would look for it. Never stand in a different fact for the one asked about (a hospital's distance for "
           "a commute; \"compare listing by listing\" when the ledger has lot sizes or prices). If the ledger truly has nothing on it, say "
           "so in one honest first-person sentence and offer to get it for the reader's own address or situation - don't write around it.\n")
        + ("\nSALES THE AGENT CHOSE TO MENTION (their own closed sales, each in the ledger): work each one into the article once, where "
           "it helps the reader, as \"a recent sale of mine\" with its public facts only (street address, town, price, month - and, where "
           "the ledger gives them, how the sale price compared with the asking price and how long it took to sell: when the article is "
           "about pricing, that comparison is the point of mentioning the sale) - never which side you represented, never a reason for the "
           "gap, and no story around it:\n"
           + "\n".join(f"- {s['address']}, {s.get('city') or ''} - {s.get('price') or ''}" + (f", {s['sold']}" if s.get("sold") else "") for s in chosen_sales)
           + "\n" if chosen_sales else "")
        + ("\nPROTECTIONS THIS READER MUST HEAR ABOUT (each is in the ledger - name every one in plain words where it fits, "
           "no rates, amounts, limits or deadlines, never say the reader qualifies, and follow it with an ask_agent line):\n"
           + "\n".join(f"- {p['name']}" for p in protections) + "\n" if protections else "")
        + (f"\nOUTSIDE THE AGENT'S AREA: the agent works in {', '.join(sorted(agent_states))}; this article's towns are in {', '.join(out_of_area)}. "
           "Never claim to live, work or have sold homes there, and never invent local experience. Keep the closing the agent chose "
           "(a showing request, starting their home search, a home value, a question) in your own first-person voice (\"I'll start your search\").\n" if out_of_area else "")
        + (f"NOT LICENSED THERE: the agent has told us they are NOT licensed in {state_names(not_licensed)}. Never offer to list, price, value, show or "
           "represent a home there yourself - no \"I'll pull the recent sales for your home\", no \"before we set a number\", no \"I'll go over my commission\". "
           f"Say once, plainly and in the first person, that you are not licensed in {state_names(not_licensed)} and that for a home there you'll introduce "
           "the reader to an agent who is. Every other offer of help is to make that introduction or to answer a general question. The closing button the "
           "agent chose stays - it is how the reader reaches you for the introduction - but the words around it offer the introduction, not the service.\n"
           if not_licensed else "")
        + ("This is a REGIONAL article, not about one town. The town items in the ledger are the agent's service area - use a few as concrete, dated examples where they genuinely help the reader; never force town names in or imply one town represents the region.\n" if regional else "")
        + (f"THIS ARTICLE IS ABOUT {about_county.upper()}. The towns in the ledger are the ones the agent works in that sit mostly inside {about_county} - "
           f"say they are examples, not the whole county, and never describe a town or a number from outside {about_county} as part of it. We hold no "
           "county-wide totals: never give a price, count or pace for the county as a whole - give each example town's own dated numbers.\n"
           if regional and about_county else "")
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
    expand_quotes(_article_text(article), writer_claims, "text")   # the writer names each claim by its opening words

    # --- 3. Fact-check ------------------------------------------------------------------------
    geo_check = lambda text: tk.geography_audit(text, profiles, known_geo)

    def problems_now() -> list[dict]:
        return code_problems(_article_text(article), brief=brief, protections=protections, agent_name=profile["name"], tax_rates=tax_rates,
                             ledger=ledger, freshness=_fresh, geo_check=geo_check, must_mention=chosen_sales,
                             not_licensed=license_note(agent_states, not_licensed) if not_licensed else None)

    draft_text = _article_text(article)
    first_problems = problems_now()
    geo_first = [p for p in first_problems if p["kind"] == "wrong_geography"]
    loan_first = [p for p in first_problems if p.get("loan")]
    automated = [_problem_line(p) for p in first_problems]
    check_msg = (
        f"Agent: {profile['name']} - {profile['brokerage']} - {profile['market'] or state}. Today's date: {today}.\n"
        f"Agent's brief:\n{brief_msg}\n\nINSERTED ELEMENTS (real parts of the article, added around the text):\n" + "\n".join(f"- {x}" for x in inserted)
        + ("\n\nAUTOMATED CHECKS (confirmed problems - each needs a fix):\n" + "\n".join(f"- {x}" for x in automated) if automated else "")
        + f"\n\n== ARTICLE ==\n{draft_text}"
    )
    await _stage(on_stage, "fact_check")
    started = time.monotonic()
    usage = _new_usage()
    review: dict = {}
    try:
        review = await _run_tool_call(model=spec["check_model"], system=_REVIEW_SYSTEM, user_msg=check_msg, tools=_CHECK_TOOLS,
                                      tool_name="submit_review", max_tokens=24000, effort=spec.get("check_effort"), usage=usage,
                                      shared=_ledger_block(ledger_text))
        await meter.record("fact_check", spec["check_model"], usage, started)
        # The checker names a supported claim by its opening words; the whole sentence is put back here, once.
        review["claims"] = [c for c in writer._coerce(review.get("claims"), list) if isinstance(c, dict)]
        expand_quotes(draft_text, review["claims"], "quote")
    except Exception:
        await meter.record("fact_check", spec["check_model"], usage, started, failed="fact_check_failed")
        logger.warning("blog pipeline: fact-check failed - saving with the unchecked report", exc_info=True)
    await _stage(on_stage, "finishing")
    # The "[Ask-me line: ...]" marker in _article_text is a real element - never a placeholder to report
    # (11/01 round 6: the fact-check listed both of Lancaster's Ask lines as unfinished text).
    issues = [i for i in writer._coerce(review.get("issues"), list) if isinstance(i, dict) and not str(i.get("quote") or "").lstrip().startswith("[Ask-me line")]
    applied, issues = _apply_guarded(article, issues, lambda: _guard_counts(_article_text(article), problems_now(), missing=False))

    # --- 3b. Re-check what can be checked without a model; one targeted repair if needed ----------
    left = problems_now()
    if left:
        geo_facts = "\n".join(f"- {i['claim']}" for i in ledger if i.get("origin") == "geo_verified")
        loan_facts = "\n".join(f"- {i['claim']}" for i in ledger if i.get("origin") == "federal" or "mortgage insurance" in (i.get("claim") or "").lower())
        says = [p["says"] for p in left if p.get("says")]
        fix_msg = ("PROBLEMS:\n" + "\n".join(("- MISSING: " + p["problem"]) if p["missing"] else f"- \"{p['quote']}\": {p['problem']}" for p in left)
                   + f"\n\nVERIFIED GEOGRAPHY:\n{geo_facts}\n\nVERIFIED LOAN FACTS:\n{loan_facts}"
                   + ("\n\nVERIFIED PROTECTIONS:\n" + "\n".join(f"- {x}" for x in says) if says else "")
                   + f"\n\n== ARTICLE ==\n{_article_text(article)}")
        started = time.monotonic()
        usage = _new_usage()
        try:
            got = await _run_tool_call(model=spec["check_model"], system=_FIX_SYSTEM, user_msg=fix_msg, tools=[_FIX_TOOL], tool_name="submit_fixes",
                                       max_tokens=6000, effort="medium", usage=usage)
            await meter.record("repair", spec["check_model"], usage, started)

            def _kind(find: str) -> str:
                hit = next((p["kind"] for p in left if p["quote"] and (p["quote"] in find or find in p["quote"])), None)
                return hit or next((p["kind"] for p in left if p["missing"]), "other")
            fixes = [dict(f, kind=_kind(f.get("find") or ""), severity="high", quote=f.get("find"))
                     for f in writer._coerce(got.get("fixes"), list) if isinstance(f, dict)]
            repaired, fixes = _apply_guarded(article, fixes, lambda: _guard_counts(_article_text(article), problems_now(), missing=True))
            issues += fixes
            applied += repaired
        except Exception:
            await meter.record("repair", spec["check_model"], usage, started, failed="repair_failed")
            logger.warning("blog pipeline: repair step failed", exc_info=True)
        left = problems_now()
    # A hard-rule sentence the repair didn't fix is cut - it never reaches the published article.
    hard_left = [p for p in left if p["cut"] and p["quote"]]
    if hard_left:
        # A bullet reads "- item" in the article text but is stored without the dash (11/01 Lancaster).
        cuts = [{"kind": p["kind"], "severity": "high", "quote": p["quote"], "find": _cut_target(article, p["quote"]) or p["quote"], "replace": "",
                 "problem": p["problem"].split(". ")[0] + " - removed before saving."} for p in hard_left]
        cut, cuts = _apply_fixes(article, cuts)
        issues += [c for c in cuts if c["fixed"]]
        applied += cut
    # --- 3c. The engine's own grader: the fixed pass list, one targeted rewrite, re-checked ---------
    # Two things only the fact-check knows are handed on for a second opinion (engine audit 11/14: a claim it
    # marked "unsupported" but gave no fix for stayed in the article and the report still said Verified; and
    # "did the article answer what the agent asked" was one clause in a long instruction, never a list).
    squash = lambda t: " ".join(str(t or "").split())
    now_text = squash(_article_text(article))
    lingering = [squash(c.get("quote")) for c in writer._coerce(review.get("claims"), list)
                 if isinstance(c, dict) and c.get("verdict") in ("unsupported", "stale") and len(squash(c.get("quote"))) >= 25
                 and squash(c.get("quote")) in now_text][:8]
    brief_points = [{"point": str(b.get("point") or "").strip()[:200], "status": b.get("status") or "answered", "note": str(b.get("note") or "").strip()[:300]}
                    for b in writer._coerce(review.get("brief_points"), list) if isinstance(b, dict) and str(b.get("point") or "").strip()][:12]
    pass_list, nice_to_have, graded, regraded = await _grade_and_fix(
        article=article, ledger_text=ledger_text, brief=brief, brief_msg=brief_msg,
        inserted=inserted + [f"Closing button: \"{writer.cta_block_args(brief)[0]}\" (opens the agent's form)"],
        spec=spec, meter=meter, find_problems=problems_now, second_opinion=lingering,
        # A real problem whose fix was thrown out by the guard gets a second, different attempt - not just a flag
        # (11/14 Media: the opening line stayed a flat claim because the checker's rewrite tripped another rule).
        unapplied=[{"quote": squash(i.get("find") or i.get("quote")), "problem": str(i.get("problem") or "")[:300]} for i in issues
                   if not i.get("fixed") and i.get("was_there") and i.get("severity") in ("high", "medium")
                   and squash(i.get("find") or i.get("quote")) in now_text][:6],
        unanswered=[b for b in brief_points if b["status"] in ("partly", "not_answered")])
    issues += graded
    applied += regraded
    # What plain code can still confirm in the FINAL text is what the agent is told - checked last, so a problem
    # a later step fixed is not reported and one a later step caused is (it used to be listed before the grader ran).
    n_settled, settled = settle_leftovers(article, problems_now)
    applied += n_settled
    issues += settled
    if not_licensed and ensure_license_note(article, license_note(agent_states, not_licensed)):
        applied += 1
        issues.append({"kind": "missing_license_note", "severity": "high", "quote": "", "problem": "Added: you aren't licensed in this state, and offer an introduction.",
                       "replace": license_note(agent_states, not_licensed), "fixed": True})
    final_left = problems_now()
    geo_left = [p for p in final_left if p["kind"] == "wrong_geography"]
    loan_left = [p for p in final_left if p.get("loan")]
    issues += [{"kind": p["kind"], "severity": p["severity"], "quote": p["quote"] or p.get("name") or "", "problem": p["note"], "fixed": False, "code": True}
               for p in final_left]
    # A point the brief asked for that is still open after the grader: the agent is told which one.
    if any(i["id"] == "asked" and i["status"] == "fail" for i in pass_list):
        for b in brief_points:
            if b["status"] in ("partly", "not_answered"):
                b["open"] = True
    # Last line of defense: nothing fill-in-the-blank reaches the published article, and no section
    # is left empty (the fact-check's own deletions can empty one).
    repeats = drop_repeated_sentences(article)
    if repeats:
        issues += [{"kind": "repetition", "severity": "low", "quote": t, "problem": "The same sentence appeared twice - the repeat was removed.",
                    "replace": "", "fixed": True} for t in repeats]
        applied += len(repeats)
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
                                                "closing_note": closing_note_for(pay_ex, state)})
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
            # `as_of` = the date or period the fact describes; `read` = the day we read the page (grader 11/13: one
            # date in brackets could be either, and some showed none at all).
            # A fact we read by hand on the official page carries the day it was read (not "for" that day); a built-in
            # protection carries no date at all rather than today's, which nobody read it on.
            origin = it.get("origin")
            by_hand = origin in ("transfer_tax", "verified_local", "federal")
            sources.append({"name": it.get("source_name"), "url": it.get("source_url"), "as_of": None if origin == "protection" else it.get("as_of"),
                            "topic": it.get("topic"), "origin": origin,
                            "read": it.get("read_on") or str(it.get("saved_at") or "")[:10] or (it.get("as_of") if by_hand else None)})
    # Sources & data notes block (public): the primary sources the article cites, the MLS window,
    # and what each area's numbers cover - so a reader (or reviewer) can check every figure.
    notes = [d["definition"] for d in dossiers if d.get("definition") and not regional]
    # Readers get the plain fact; the "never add these together" instruction is for the writer only.
    notes += [re.sub(r",? so the areas' counts must never be added together\.?$", ".", i["claim"].replace("Overlap: ", "")) for i in overlap]
    if any(not (d.get("sales_snapshot") or {}).get("prior_year_reliable", True) for d in dossiers):
        notes.append("Year-ago comparisons are left out where our Bright MLS history for that window is incomplete.")
    if pay_ex:
        notes.append(f"Payment example: principal and interest at Freddie Mac's 30-year average ({pay_ex['rate']:.2f}%, week of {pay_ex['rate_as_of']}) - a "
                     "national average for well-qualified borrowers putting 20% down, so a smaller down payment or a jumbo loan is usually priced "
                     f"differently; property tax estimated from the {pay_ex.get('tax_basis') or 'tax bills on similar-priced listings'}; homeowners "
                     + ("insurance and mortgage insurance not included; the line with the association fee adds the median fee of the "
                        f"{pay_ex['fee']['homes']} {pay_ex['fee']['label']} for sale that list one." if pay_ex.get("fee")
                        else "insurance, HOA fees and mortgage insurance not included."))
    notes += how_counted_notes(_article_text(article), pay_ex, town_card=bool(spotlight))
    mls_through = max((d["sales_snapshot"].get("data_through") for d in dossiers if (d.get("sales_snapshot") or {}).get("data_through")), default=None)
    # the for-sale count carries the day our MLS data was last updated, not today (grader audit 11/13, finding 2)
    _inv_day = min((d["sales_snapshot"].get("inventory_as_of") for d in dossiers if (d.get("sales_snapshot") or {}).get("inventory_as_of")),
                   default=None) or datetime.now(timezone.utc).date().isoformat()
    # Readers are shown a town's older stored research only when it came from an official address.
    public_sources = tk.drop_stale_editions(tk.drop_other_county_items(
        [s for s in sources if s.get("url") and s.get("topic") != "agent_brief"
         and (s.get("origin") != "idx_research" or official_source(s["url"]))], profiles))
    if public_sources or notes:
        block = {"id": writer._bid("sources"), "type": "sources", "data": {
            # a page read on the web shows the day we read it - never the "effective" date the AI gave for it (grader 11/15: a
            # checklist shown as "for January 1, 2026" was the March-December 2025 edition)
            "items": [{"name": s["name"], "url": s["url"], "as_of": None if s.get("origin") in WEB_READ and s.get("read") else _public_date(s.get("as_of")),
                       "read": s.get("read")} for s in public_sources[:SOURCES_SHOWN]],
            "mls": (f"Bright MLS via OpenHousePal - closed sales through {mls_through}; homes for sale as of {_inv_day}." if mls_through else None),
            "notes": notes[:12],
        }}
        at = next((i for i, b in enumerate(blocks) if b["type"] in ("cta", "agent_profile")), len(blocks))
        blocks[at:at] = [block]
    # The agent's own video, sales and listings: cards after the sources, just before the closing button.
    blocks = writer.place_related(blocks, related)

    verdicts = [c.get("verdict") for c in claims]
    final_text = " ".join(_article_text(article).split())

    def _still_there(i: dict) -> bool:
        """An unfixed AI flag stays unless its sentence WAS in the article and a later step has since changed it."""
        if i.get("code") or not i.get("was_there"):
            return True
        q = " ".join(str(i.get("find") or i.get("quote") or "").split())
        return not q or q in final_text
    open_issues = [i for i in issues if not i["fixed"] and i.get("severity") in ("high", "medium") and _still_there(i)]
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
        "research_lookups": list(research_lookups)[:16],
        "research_banked": banked,
        "towns": [{"town": d["town"], "facts": len(d["items"]), "gaps": d["gaps"]} for d in dossiers],
        "regional": regional,
        "about_county": about_county,
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
        "licensed_there": brief.get("licensed_there") if out_of_area else None,
        # The engine's own grade against the fixed pass list (pass / fixed / cut / fail per item).
        "pass_list": pass_list, "nice_to_have": nice_to_have,
        # Each question or point from the agent's brief and whether the article answers it.
        "brief_points": brief_points,
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
    found = await _research_call(model=model, user_msg=f"Today is {today}.\n\nYOUR TASK: {task}", searches=searches, fetches=fetches,
                                 state=None, max_tokens=24000, effort="medium", usage=usage)
    items = [{"topic": i.get("topic") or "other", "claim": i["claim"].strip(), "source_name": i.get("source_name"), "source_url": i.get("source_url"),
              "as_of": i.get("as_of"), "confidence": i.get("confidence")} for i in (found.get("items") or []) if isinstance(i, dict) and i.get("claim") and i.get("source_url")]
    added = await tk.save_state_items(db, state, items)
    config = await ai_config_service.get_config(db)
    cost = _cost(config, model, usage)
    logger.info("state facts pack seeded", extra={"state": state, "added": added, "cost_usd": cost, "searches": usage["web_search_requests"]})
    return {"added": added, "found": len(items), "not_found": found.get("not_found") or [], "cost_usd": round(cost, 4), "usage": usage}
