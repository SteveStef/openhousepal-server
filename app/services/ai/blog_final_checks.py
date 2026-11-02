"""Checks every AI blog article gets in its single run - plain code, not the writer's instructions.

11/01 owner: rules the writer is only *told* about don't hold (round 4 published a school millage
rate the prompt banned, and missed Maryland's first-time-buyer closing-tax rules entirely). So:

1. PROTECTIONS - a built-in, checked list of the state rules and programs a given reader should
   hear about (first-time buyers, condo/HOA buyers, sellers, older owners), named only, each
   with its official source. They go into the evidence ledger, the writer is told to name each
   one, and `missing_protections` confirms they're in the finished article.
2. `final_lint` - after the fact-check, sentences that break a hard rule are found by pattern:
   fine print (tax rates, mills, program limits, deadlines), a sale-to-list median read as
   "half sold above asking", townhomes treated as condos, and first-person stories the agent
   didn't give. The pipeline sends them back for one targeted fix, then cuts any that remain.
   The agent's own closing ("I'll start your search") is never flagged, in any state - owner
   11/01: the agent may hand an out-of-area lead to a referral agent.
"""
import re
from typing import Optional

# --- 1. Reader protections -------------------------------------------------------------------
# readers: "first_time", "condo" (condo / townhome / HOA), "buyer", "seller", "older".
# `says` = one plain sentence, no amounts, rates, ages or deadlines (the article names the rule;
# the reader asks the agent or the settlement company for the details). `check` = words that
# show the finished article mentioned it.
PROTECTIONS = [
    # Pennsylvania
    {"state": "PA", "readers": {"buyer", "seller"}, "key": "pa_disclosure", "check": r"disclosure",
     "name": "Pennsylvania Real Estate Seller Disclosure Law",
     "says": "Under Pennsylvania's Real Estate Seller Disclosure Law, most sellers must give the buyer a Seller's Property Disclosure Statement before the Agreement of Sale is signed.",
     "source_name": "Pennsylvania General Assembly - 68 Pa.C.S. Chapter 73 (Seller Disclosure)",
     "source_url": "https://www.legis.state.pa.us/cfdocs/legis/LI/consCheck.cfm?txtType=HTM&ttl=68&div=0&chpt=73"},
    {"state": "PA", "readers": {"condo"}, "key": "pa_resale_cert", "check": r"resale certificate",
     "name": "Pennsylvania condo and planned-community resale certificates",
     "says": "In Pennsylvania, a resale condo or a home in a planned community (HOA) comes with a resale certificate from the association, and the buyer gets a short window after receiving it to review it and cancel the agreement.",
     "source_name": "Pennsylvania General Assembly - 68 Pa.C.S. Sections 3407 and 5407",
     "source_url": "https://www.legis.state.pa.us/cfdocs/legis/LI/consCheck.cfm?txtType=HTM&ttl=68&div=0&chpt=54"},
    {"state": "PA", "readers": {"first_time"}, "key": "pa_phfa", "check": r"PHFA|Pennsylvania Housing Finance",
     "name": "PHFA home loans and down payment help",
     "says": "The Pennsylvania Housing Finance Agency (PHFA) offers home loans and down payment and closing cost help for qualifying buyers through participating lenders.",
     "source_name": "Pennsylvania Housing Finance Agency", "source_url": "https://www.phfa.org/"},
    {"state": "PA", "readers": {"older"}, "key": "pa_ptrr", "check": r"Property Tax/Rent Rebate|Rent Rebate",
     "name": "Pennsylvania Property Tax/Rent Rebate Program",
     "says": "Pennsylvania's Property Tax/Rent Rebate Program can return part of the property taxes paid by qualifying older homeowners.",
     "source_name": "Pennsylvania Department of Revenue - Property Tax/Rent Rebate Program",
     "source_url": "https://www.revenue.pa.gov/IncentivesCreditsPrograms/PropertyTaxRentRebateProgram/"},
    # Maryland
    # Round 4 named the lower transfer tax but not who pays. Round 7 grader: "by default toward the seller,
    # unless the contract says otherwise" blurred the law - some of these costs are assigned by law
    # (the state transfer tax), others can be negotiated (recordation, local transfer). Never say which.
    {"state": "MD", "readers": {"first_time"}, "key": "md_first_time_taxes",
     "check": r"first-time[^.]{0,240}(which party|seller|which side|who pays)|(which party|seller|which side|who pays)[^.]{0,240}first-time",
     "name": "Maryland's first-time homebuyer closing-tax rules",
     "says": "Qualifying first-time Maryland homebuyers may receive special state transfer-tax treatment, and the law also affects which party pays certain closing taxes - some of those costs can be negotiated, while others are assigned by law, so ask your settlement company to explain the rules and estimate your actual costs.",
     "source_name": "Maryland Code, Tax-Property Section 13-203 and Real Property Section 14-104",
     "source_url": "https://mgaleg.maryland.gov/mgawebsite/Laws/StatuteText?article=gtp&section=13-203"},
    {"state": "MD", "readers": {"condo"}, "key": "md_condo_resale", "check": r"resale (documents|package|certificate)|cancel",
     "name": "Maryland condo and HOA resale disclosures",
     "says": "In Maryland, buyers of a resale condo or a home in an HOA receive the association's resale documents and get a short window after receiving them to review them and cancel the contract.",
     "source_name": "Maryland Code, Real Property Sections 11-135 and 11B-106",
     "source_url": "https://mgaleg.maryland.gov/mgawebsite/Laws/StatuteText?article=grp&section=11-135"},
    # Round 13 grader: "as-is" alone leaves out that a disclaiming seller must still disclose known
    # hidden defects that threaten health or safety.
    {"state": "MD", "readers": {"buyer", "seller"}, "key": "md_disclosure",
     "check": r"(?:Disclaimer|as[- ]is)[\s\S]{0,400}?(?:latent|hidden|health or safety)|(?:latent|hidden)[\s\S]{0,400}?(?:Disclaimer|as[- ]is)",
     "name": "Maryland Residential Property Disclosure and Disclaimer Statement",
     "says": "Maryland sellers of many homes must give the buyer either a Residential Property Disclosure Statement or a Disclaimer Statement (an as-is sale), with some exceptions - and a seller who uses the disclaimer must still disclose known hidden (latent) defects that threaten health or safety.",
     "source_name": "Maryland Code, Real Property Section 10-702",
     "source_url": "https://mgaleg.maryland.gov/mgawebsite/Laws/StatuteText?article=grp&section=10-702"},
    # Round 13 grader: a Baltimore rowhome article never mentioned ground rent. `where` = the article's
    # towns/counties (normalized) it applies to.
    {"state": "MD", "readers": {"buyer", "seller"}, "key": "md_ground_rent", "where": {"baltimore city", "baltimore"},
     "check": r"ground[- ]rent|ground lease",
     "name": "Maryland ground rent",
     "says": "Some Maryland homes - especially in Baltimore City - are subject to ground rent: the owner of the house pays a regular rent to a separate owner of the land under it. Maryland keeps a Ground Rent Registry, so ask the settlement company to check the deed and explain any ongoing obligation before you commit.",
     "source_name": "Maryland State Department of Assessments and Taxation - Ground Rent",
     "source_url": "https://dat.maryland.gov/realproperty/Pages/Ground-Rent.aspx"},
    {"state": "MD", "readers": {"older"}, "key": "md_tax_credits", "check": r"Homestead|Homeowners'? Property Tax Credit",
     "name": "Maryland Homestead and Homeowners' Property Tax Credits",
     "says": "Maryland's Homestead Tax Credit and Homeowners' Property Tax Credit can limit or lower property taxes for qualifying owners who live in the home.",
     "source_name": "Maryland State Department of Assessments and Taxation",
     "source_url": "https://dat.maryland.gov/realproperty/Pages/Maryland-Homestead-Property-Tax-Credit-Program.aspx"},
    # Virginia
    {"state": "VA", "readers": {"condo"}, "key": "va_resale", "check": r"resale certificate|Resale Disclosure|cancel",
     "name": "Virginia Resale Disclosure Act",
     "says": "Under Virginia's Resale Disclosure Act, buyers of a home in a condo or property owners' association receive a resale certificate and can cancel within a short window after receiving it - or before settlement if it never arrives.",
     "source_name": "Code of Virginia Section 55.1-2312",
     "source_url": "https://law.lis.virginia.gov/vacode/title55.1/chapter23.1/section55.1-2312/"},
    {"state": "VA", "readers": {"buyer", "seller"}, "key": "va_disclosure", "check": r"Disclosure Act|buyer beware|caveat emptor|disclosure",
     "name": "Virginia Residential Property Disclosure Act",
     "says": "Virginia is largely a 'buyer beware' state: under the Residential Property Disclosure Act, sellers give buyers a statement that points them to their own inspections and research.",
     "source_name": "Code of Virginia Title 55.1, Chapter 7",
     "source_url": "https://law.lis.virginia.gov/vacode/title55.1/chapter7/"},
    {"state": "VA", "readers": {"seller"}, "key": "va_grantor", "check": r"grantor",
     "name": "Virginia grantor's tax",
     "says": "In Virginia the seller pays the grantor's tax at settlement; the settlement agent itemizes it with the other closing costs.",
     "source_name": "Code of Virginia Section 58.1-802",
     "source_url": "https://law.lis.virginia.gov/vacode/title58.1/chapter8/section58.1-802/"},
    {"state": "VA", "readers": {"first_time"}, "key": "va_housing", "check": r"Virginia Housing",
     "name": "Virginia Housing loans and down payment help",
     "says": "Virginia Housing offers home loans and down payment help for qualifying buyers through participating lenders.",
     "source_name": "Virginia Housing", "source_url": "https://www.virginiahousing.com/"},
    # Delaware
    {"state": "DE", "readers": {"first_time"}, "key": "de_first_time_rtt", "check": r"first-time[^.]{0,160}transfer tax|transfer tax[^.]{0,160}first-time",
     "name": "Delaware first-time homebuyer transfer tax reduction",
     "says": "Delaware lowers the buyer's share of the state realty transfer tax for qualifying first-time homebuyers - ask the settlement attorney to confirm whether it applies to you.",
     "source_name": "Delaware Code Title 30, Chapter 54 (Realty Transfer Tax)",
     "source_url": "https://delcode.delaware.gov/title30/c054/sc01/index.html"},
    {"state": "DE", "readers": {"condo"}, "key": "de_resale", "check": r"resale certificate|cancel",
     "name": "Delaware common-interest resale certificates",
     "says": "In Delaware, a resale condo or a home in an HOA comes with a resale certificate, and a buyer who gets it after signing has a short window to cancel the contract.",
     "source_name": "Delaware Code Title 25, Chapter 81, Subchapter IV",
     "source_url": "https://delcode.delaware.gov/title25/c081/sc04/index.html"},
    {"state": "DE", "readers": {"buyer", "seller"}, "key": "de_disclosure", "check": r"disclosure",
     "name": "Delaware Seller's Disclosure of Real Property Condition Report",
     "says": "Delaware sellers generally must give buyers a Seller's Disclosure of Real Property Condition Report before an agreement of sale is signed.",
     "source_name": "Delaware Code Title 6, Chapter 25, Subchapter VII",
     "source_url": "https://delcode.delaware.gov/title6/c025/sc07/index.html"},
    {"state": "DE", "readers": {"first_time"}, "key": "de_dsha", "check": r"DSHA|Delaware State Housing",
     "name": "DSHA home loans and down payment help",
     "says": "The Delaware State Housing Authority (DSHA) offers home loans and down payment help for qualifying buyers through participating lenders.",
     "source_name": "Delaware State Housing Authority", "source_url": "https://www.destatehousing.com/"},
    # District of Columbia
    {"state": "DC", "readers": {"first_time"}, "key": "dc_first_time_recordation", "check": r"first-time[^.]{0,160}recordation|recordation[^.]{0,160}first-time",
     "name": "DC reduced recordation tax rate for first-time homebuyers",
     "says": "DC offers a reduced recordation tax rate for qualifying first-time DC homebuyers; the title company applies for it at settlement.",
     "source_name": "DC Office of Tax and Revenue",
     "source_url": "https://otr.cfo.dc.gov/"},
    {"state": "DC", "readers": {"condo"}, "key": "dc_condo_resale", "check": r"resale certificate|cancel",
     "name": "DC condominium resale certificate",
     "says": "In DC, a resale condo buyer receives the condominium documents and a resale certificate and gets a short window after receiving them to cancel the contract.",
     "source_name": "DC Code Section 42-1904.11",
     "source_url": "https://code.dccouncil.gov/us/dc/council/code/sections/42-1904.11"},
    {"state": "DC", "readers": {"buyer", "seller"}, "key": "dc_disclosure", "check": r"disclosure",
     "name": "DC Seller's Disclosure Statement",
     "says": "DC sellers generally must give buyers a Seller's Disclosure Statement about the home's condition before the contract is signed.",
     "source_name": "DC Code Section 42-1302",
     "source_url": "https://code.dccouncil.gov/us/dc/council/code/sections/42-1302"},
    # New Jersey
    {"state": "NJ", "readers": {"buyer", "seller"}, "key": "nj_attorney_review", "check": r"attorney review",
     "name": "New Jersey attorney review",
     "says": "In New Jersey, a sales contract prepared by a real estate agent comes with an attorney review period, when either side's attorney can review it, ask for changes or cancel.",
     "source_name": "New Jersey Real Estate Commission rules (N.J.A.C. 11:5-6.2)", "source_url": None},
    {"state": "NJ", "readers": {"seller"}, "key": "nj_rtf", "check": r"Realty Transfer Fee",
     "name": "New Jersey Realty Transfer Fee",
     # 11/02: the Division of Taxation page says the seller is responsible for the RTF AND the graduated
     # fee on higher-priced sales (it was the buyer's "mansion tax" before the 2025 amendment).
     "says": "In New Jersey the seller pays the Realty Transfer Fee at closing, and higher-priced sales carry an additional graduated fee that is also the seller's responsibility; the closing attorney or title company itemizes both.",
     "source_name": "New Jersey Division of Taxation - Realty Transfer Fee",
     "source_url": "https://www.nj.gov/treasury/taxation/realty.shtml"},
    {"state": "NJ", "readers": {"first_time"}, "key": "nj_hmfa", "check": r"NJHMFA|Housing and Mortgage Finance",
     "name": "NJHMFA home loans and down payment help",
     "says": "The New Jersey Housing and Mortgage Finance Agency (NJHMFA) offers home loans and down payment help for qualifying buyers through participating lenders.",
     "source_name": "New Jersey Housing and Mortgage Finance Agency", "source_url": "https://www.njhousing.gov/"},
    {"state": "NJ", "readers": {"older"}, "key": "nj_relief", "check": r"Senior Freeze|ANCHOR|Stay NJ",
     "name": "New Jersey property tax relief (ANCHOR, Senior Freeze, Stay NJ)",
     "says": "New Jersey's ANCHOR, Senior Freeze and Stay NJ programs can lower property taxes for qualifying homeowners, including older owners.",
     "source_name": "New Jersey Division of Taxation - Property Tax Relief Programs",
     "source_url": "https://www.nj.gov/treasury/taxation/relief.shtml"},
    # Federal, every state ("US"). Round 13 grader: an older-rowhome article covered the roof, basement
    # and sewer line but not lead paint. `when` = "older_homes": the topic is about older homes, or
    # most of the town's listings were built before 1978.
    # Round 14 grader nice-to-have: name the Closing Disclosure in buyer articles (federal, every state).
    {"state": "US", "readers": {"buyer", "first_time"}, "key": "us_closing_disclosure", "check": r"Closing Disclosure",
     "name": "The lender's Closing Disclosure",
     "says": "Before closing, a mortgage lender must give you a Closing Disclosure - the final list of your loan terms and closing costs - so compare it with the estimate you were given and ask about anything that changed.",
     "source_name": "CFPB: Closing Disclosure", "source_url": "https://www.consumerfinance.gov/owning-a-home/closing-disclosure/"},
    {"state": "US", "readers": {"buyer", "seller"}, "key": "us_lead_paint", "when": "older_homes",
     "check": r"lead[- ]based paint|lead paint|lead (?:hazard|disclosure|inspection)",
     "name": "Federal lead-based paint disclosure (homes built before 1978)",
     "says": "For most homes built before 1978, federal law requires the seller to disclose known lead-based paint and lead hazards before the contract is signed and gives the buyer a chance to have the home checked for lead - ask about the lead disclosure and whether a lead inspection makes sense.",
     "source_name": "U.S. EPA - Real Estate Disclosures about Potential Lead Hazards",
     "source_url": "https://www.epa.gov/lead/real-estate-disclosure"},
]

_OLDER_HOMES = re.compile(r"\bolder\b|\bold (?:house|home)s?\b|row ?homes?|row ?houses?|historic|pre-?1978|vintage|century[- ]old|victorian|twin homes?", re.I)
OLDER_HOMES_SHARE = 0.4  # listings built before 1978

_READERS = (
    ("first_time", re.compile(r"first[- ]time|first home|starter home", re.I)),
    ("condo", re.compile(r"condo|townho|\bHOA\b|homeowners'? association|association fee", re.I)),
    ("buyer", re.compile(r"\bbuy(er|ers|ing)?\b|\bpurchas", re.I)),
    ("seller", re.compile(r"\bsell(er|ers|ing)?\b|\blist(ing)? (your|my) home", re.I)),
    ("older", re.compile(r"downsiz|empty[- ]?nest|retir|55\+|senior|older (home)?owners?", re.I)),
)


def article_readers(brief: dict) -> set[str]:
    said = " ".join(str(brief.get(k) or "") for k in ("topic", "goal", "key_points")) + " " + " ".join(brief.get("audiences") or [])
    return {name for name, rx in _READERS if rx.search(said)}


def protections_for(states: set[str], brief: dict, investment: bool = False, places: Optional[set[str]] = None,
                    older_share: Optional[float] = None) -> list[dict]:
    """The protections this article's reader should hear about - none of the live-in-the-home
    programs for an investment article. `places`: the article's towns and counties (normalized), for
    place-specific ones (Baltimore ground rent); `older_share`: the share of its listings built before
    1978, for the lead-paint disclosure."""
    readers = article_readers(brief)
    if investment:
        readers -= {"first_time", "older"}
    said = " ".join(str(brief.get(k) or "") for k in ("topic", "goal", "key_points"))
    older = bool(_OLDER_HOMES.search(said)) or (older_share or 0) >= OLDER_HOMES_SHARE
    return [p for p in PROTECTIONS if (p["state"] in states or p["state"] == "US") and p["readers"] & readers
            and (not p.get("where") or p["where"] & (places or set()))
            and (p.get("when") != "older_homes" or older)]


def protection_items(protections: list[dict], as_of: str) -> list[dict]:
    """Evidence-ledger items (origin "protection")."""
    return [{"topic": "regulation", "claim": p["says"], "source_name": p["source_name"], "source_url": p["source_url"],
             "as_of": as_of, "confidence": 0.95, "origin": "protection", "town": "general", "protection": p["key"]} for p in protections]


_LINK = re.compile(r"\[([^\]]+)\]\([^)\s]+\)")


def _plain(text: str) -> str:
    """[anchor](url) -> anchor: a link's web address (full of dots) must not split a sentence
    for the checks (11/01 round 7: a law link inside the Maryland first-time-buyer sentence hid it)."""
    return _LINK.sub(r"\1", text or "")


def missing_protections(text: str, protections: list[dict]) -> list[dict]:
    text = _plain(text)
    return [p for p in protections if not re.search(p["check"], text, re.I)]


# --- 2. Final lint ---------------------------------------------------------------------------
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(*])")

_FINE_PRINT = [
    (re.compile(r"\b\d+(\.\d+)?\s*mills?\b|\bmillage (rate )?of\b|\bmill rate\b", re.I), "a property-tax millage rate"),
    # Maryland/Virginia write tax rates per $100 of assessed value (11/01 Frederick: "$0.7055 per $100").
    (re.compile(r"\$\s?\d*\.\d+\s*(?:per|for each|/)\s*\$?\s?100\b", re.I), "a property-tax rate"),
    # A program's eligibility put in the writer's own words (11/01 round-12 grader, Cherry Hill: NJHMFA's
    # First Generation program described as "for buyers who are the first in their family to buy a home" -
    # it has specific parent/guardian and household conditions). Plain categories ("for buyers who
    # qualify", "for eligible veterans", "income limits apply") are fine.
    (re.compile(r"\bfirst[- ](?:person |one )?in (?:their|your|the|his|her|a) (?:family|household)\b"
                r"|\bfirst[- ]generation\b[^.]{0,120}\b(?:means|is for|are for|for (?:buyers|people|borrowers|those|anyone|homebuyers) (?:who|whose)|if (?:you|your)|whose parents)"
                r"|\bparents?(?: or (?:a )?guardians?)? (?:never|didn'?t|did not|haven'?t|have not|has not|hasn'?t) (?:owned|bought)"
                r"|\b(?:haven'?t|have not|has not|hasn'?t|never) owned[^.]{0,40}\b(?:\d+|two|three|five) years", re.I),
     "a program's eligibility rule"),
    # Which office collects which closing tax is local procedure that's easy to misstate (11/01
    # grader, Frederick: the Treasurer collects recordation tax, not the clerk).
    (re.compile(r"\b(?:clerk|treasurer|register of (?:deeds|wills)|recorder|circuit court|county|state|settlement (?:agent|company)|title company)\b[^.]{0,50}\bcollect(?:s|ed|ing)?\b[^.]{0,60}\b(?:tax|taxes|fees?)\b"
                r"|\b(?:tax|taxes|fees?)\b[^.]{0,40}\b(?:collected|paid|payable|due) (?:by|to|at|with) (?:the )?(?:county|clerk|treasurer|register|recorder|circuit court|state)\b", re.I),
     "which office collects a closing tax"),
    (re.compile(r"(transfer tax|transfer fee|recordation tax|grantor'?s? tax|mansion tax|tax rate)[^.]{0,80}\d+(\.\d+)?\s*%|\d+(\.\d+)?\s*%[^.]{0,60}(transfer tax|transfer fee|recordation tax|grantor'?s? tax|mansion tax)", re.I),
     "a closing-tax or tax rate"),
    (re.compile(r"(income|loan|purchase[- ]price|sales[- ]price|price) (limit|cap)s?[^.]{0,40}\$\s?[\d,]+|\$\s?[\d,]+[^.]{0,40}(income|loan|price) (limit|cap)", re.I), "a program limit"),
    (re.compile(r"\bup to \$\s?[\d,]+[^.]{0,40}(assistance|help|grant|credit|rebate|toward)", re.I), "a program's dollar amount"),
    (re.compile(r"\b(within|after|before)\s+(\d+|three|five|seven|ten)\s+(business |calendar )?days\b", re.I), "a legal deadline"),
    (re.compile(r"\bdeadline (is|of)\b[^.]{0,30}\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d", re.I), "a program deadline"),
]
_MARKET_SHARE = re.compile(r"\beffective\b|of (the )?(list|sale|asking|purchase) price|of (the )?(home'?s? )?(price|value)\b", re.I)
_CLOSING_TAX = re.compile(r"transfer|recordation|grantor|mansion", re.I)
# A property-tax burden as a percent (of list price, value, or bare) reads like an official rate
# (11/01 grader, Princeton "That's about 1.65% of list price. By municipality, the median ran
# about 1.52% of list price in Hopewell Township..."). A bare "% of list price" counts when the
# sentence before was about taxes; sale-to-list figures and loan percents are not tax figures.
_TAX_PCT = re.compile(r"(?:property[- ]tax\w*|tax bills?|\btax(?:es)? (?:ran|run|runs|was|were|is|are|of|at|about|around))[^.%]{0,90}?(?<![\d.])\d+(?:\.\d+)?\s*%(?!\s*(?:down|of (?:the )?(?:homes|listings|sales|buyers)))"
                      r"|(?<![\d.])\d+(?:\.\d+)?\s*%[^.]{0,40}(?:in (?:annual |yearly )?(?:property )?tax|property[- ]tax|of (?:the )?(?:list|listing|asking|sale) price (?:a|per|each) year)"
                      # A tax increase as a percent (11/01 round 11 Doylestown: "a 5.7% school tax increase").
                      r"|(?<![\d.])\d+(?:\.\d+)?\s*%\s+(?:\w+\s+){0,3}?(?:tax|taxes|millage)\s+(?:increase|hike|rise|jump)"
                      r"|\b(?:tax|taxes|millage)\s+(?:increase|hike|rise|jump|went up|rose|grew)\w*\s+(?:of\s+|by\s+)?(?:about\s+|nearly\s+|roughly\s+)?(?<![\d.])\d+(?:\.\d+)?\s*%", re.I)
_TAX_WORDS = re.compile(r"property[- ]tax|tax bills?|\btaxes\b", re.I)
_PCT_OF_PRICE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?\s*%[^.]{0,30}\bof (?:the )?(?:list|listing|asking) price", re.I)
_SALE_WORDS = re.compile(r"\b(?:sold|sale|sales|closed|went for|selling)\b", re.I)
_LOAN_CONTEXT = re.compile(r"mortgage|interest|Freddie|\bdown\b|\bloan|Payment example card", re.I)
_DEADLINE_CONTEXT = re.compile(r"cancel|rescind|review|deadline|apply|file|certificate|attorney|contract|notice", re.I)

_CONDO_TOWNHOME = re.compile(r"(condos? (and|or|/) ?townho\w*|townho\w* (and|or|/) ?condos?)[^.]{0,140}(only covers|part of what|association (maintains|covers|handles|is responsible)|exterior|roof|common (areas|elements))", re.I)

# Round 7 grader: two claims stated too absolutely.
_APPRAISAL_ABSOLUTE = re.compile(r"apprais\w*[^.]{0,90}\b(?:needs? to|must|has to|have to|will need to)\b[^.]{0,40}\b(?:support|match|equal|come in at|meet|cover)\b", re.I)
_MD_DEFAULT_SELLER = re.compile(r"first-time[^.]{0,240}(?:toward the seller|seller pays|seller must pay|paid by the seller)[^.]{0,120}\b(?:unless|by default)\b"
                                r"|first-time[^.]{0,240}\bby default\b[^.]{0,120}seller", re.I)

# A flat jumbo verdict about a price, home or down payment (11/01 round-11 grader, Ashburn: "Even at 20%
# down, the loan is still a jumbo loan" - wrong under Loudoun County's limit). Definitions ("a loan
# above the limit counts as jumbo") and hedged lines ("may be", "depending on") are fine.
_JUMBO = re.compile(r"\bjumbo\b", re.I)
_JUMBO_SPECIFIC = re.compile(r"\$\s?[\d,.]+|\d+\s*%\s*down|\b(?:this|that|the|your)\s+(?:home|house|loan|mortgage|purchase)\b", re.I)
_JUMBO_FLAT = re.compile(r"\b(?:would|will|is|are|still|must|needs?|requires?)\b", re.I)
_JUMBO_HEDGE = re.compile(r"\b(?:may|might|could|depend\w*|whether|if)\b", re.I)

# "49 of the 195 listings" - a part can never be bigger than its whole (11/01 grader, Lancaster).
_PART_OF = re.compile(r"(?<![\$\d.,])(\d[\d,]*)\s+(?:of|out of)\s+(?:the\s+|those\s+|these\s+|its\s+|all\s+|our\s+)?(\d[\d,]*)(?![\d.,]*\s*%)(?!\d)", re.I)

_HALF_LOGIC = re.compile(r"\bhalf (of )?(the |those |these )?(homes |sales )?(sold|went|closed)[^.]{0,40}(above|over|below|under)\b", re.I)

_ANECDOTE = re.compile(r"\bI(?:'ve| have)\s+(?:seen|watched|helped|worked with|sold|had|guided|walked)\b|\bin my experience\b|\bmy (?:past |recent )?(?:clients|buyers|sellers)\b|\bI once\b|\bone of my\b", re.I)


def _sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        out += [s.strip() for s in _SENT.split(line) if s.strip()]
    return out


def final_lint(text: str, brief: dict) -> list[dict]:
    """Sentences that break a hard rule: [{"sentence", "kind", "fix"}]."""
    text = _plain(text)
    personal = (brief.get("personal_take") or "").lower()
    found, seen = [], set()

    def flag(s, kind, fix):
        if s not in seen:
            seen.add(s)
            found.append({"sentence": s, "kind": kind, "fix": fix})

    tax_ctx = False
    for s in _sentences(text):
        if s.startswith(("SEO title:", "Meta description:")):
            continue
        # The percent in a loan sentence is the mortgage rate or down payment (the payment card,
        # "at 7.24%, ... including property tax"), not a tax figure.
        tax_pct = not _LOAN_CONTEXT.search(s) and bool(_TAX_PCT.search(s) or (tax_ctx and _PCT_OF_PRICE.search(s) and not _SALE_WORDS.search(s)))
        tax_ctx = bool(_TAX_WORDS.search(s)) or (tax_ctx and tax_pct)
        if tax_pct:
            flag(s, "rule_detail", "It gives a property tax as a percent, which reads like an official tax rate. Leave the percents out: say property taxes "
                                   "vary by municipality, that two similarly priced homes can have very different tax bills, and to check the actual tax bill "
                                   "and assessment for the home. A dated median tax bill in dollars from the listings can stay.")
        for rx, what in ([] if tax_pct else _FINE_PRINT):
            if not rx.search(s) or (what == "a legal deadline" and not _DEADLINE_CONTEXT.search(s)):
                continue
            # A tax bill as a share of the price is handled above (_TAX_PCT), not as a closing tax.
            if what == "a closing-tax or tax rate" and _MARKET_SHARE.search(s) and not _CLOSING_TAX.search(s):
                continue
            if what == "a program's eligibility rule":
                flag(s, "rule_detail", "It puts a program's eligibility in its own words, which can be wrong and can make readers think they don't qualify. "
                                       "Name the program only and say it's for qualified buyers, then tell the reader to ask a participating lender (or me) "
                                       "whether they meet its requirements - e.g. \"NJHMFA also offers additional assistance through its First Generation "
                                       "Down Payment Assistance Program for qualified buyers. Ask a participating lender whether you meet the program's requirements.\"")
            elif what == "which office collects a closing tax":
                flag(s, "rule_detail", "It says which office collects or receives closing taxes or fees - local procedure that's easy to misstate. Leave the procedure out: "
                                       "say the state and county transfer taxes, recordation taxes and recording fees can affect closing costs, and to ask the "
                                       "settlement company or attorney for an itemized estimate for the specific property.")
            else:
                flag(s, "rule_detail", f"It states {what}. Name the rule or program in general words with no rates, amounts, limits or deadlines, "
                                       "and point the reader to me, the settlement company or the official source for the current figure.")
            break
        for m in _PART_OF.finditer(s):
            part, whole = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
            if part > whole:
                flag(s, "count_mismatch", f"\"{m.group(0)}\" - a part ({part:,}) can't be bigger than its total ({whole:,}). "
                                          "Use counts from the same set of records, or leave the breakdown out.")
                break
        if _JUMBO.search(s) and _JUMBO_SPECIFIC.search(s) and _JUMBO_FLAT.search(s) and not _JUMBO_HEDGE.search(s) and not s.startswith("**[Payment"):
            flag(s, "overstatement", "Too absolute: whether a loan is jumbo depends on the amount borrowed and the conforming loan limit for the property's "
                                     "county. Don't state it for a price or down payment - point to the payment example (it marks each down payment "
                                     "against the county's limit) or say it may be a jumbo loan depending on how much you borrow, and to ask a lender.")
        if _APPRAISAL_ABSOLUTE.search(s):
            flag(s, "overstatement", "Too absolute: a financed purchase can still close after a low appraisal. Say the lender may require an appraisal "
                                     "to help decide the loan amount, and if it comes in below the price the buyer may negotiate, bring more cash "
                                     "or review options with the lender.")
        if _MD_DEFAULT_SELLER.search(s):
            flag(s, "overstatement", "Too blunt about who pays: some of these closing taxes are assigned by law and others can be negotiated. "
                                     "Say the law affects which party pays certain closing taxes, some costs are negotiable and others aren't, "
                                     "and the settlement company can explain and estimate them.")
        if _HALF_LOGIC.search(s):
            flag(s, "stat_logic", "A median sale-to-list ratio doesn't mean half sold above and half below - many sell at exactly asking. "
                                  "Say what the median means (the midpoint of sales was at that share of asking) or use the ledger's above/at/below counts.")
        if _CONDO_TOWNHOME.search(s):
            flag(s, "condo_townhome", "This treats every townhome like a condo. A townhome is a building style: one owned as a condo unit shares "
                                      "the building with the association, one owned with its land doesn't. Say \"with a condo (or a townhome owned as a condo unit)\".")
        m = _ANECDOTE.search(s)
        if m and m.group(0).lower() not in personal:
            flag(s, "invented_experience", "A first-person story or track record the agent never gave. Remove the experience claim; keep any useful advice in general words.")
    return found


# --- 3. The pass list (built-in grader) --------------------------------------------------------
# One fixed list, built from every outside-grader round so far (Radnor/Wayne, the four out-of-state
# tests, Baltimore, Cherry Hill, Ashburn, Wilmington, Lancaster, Annapolis). The engine grades every
# article against it as its last step; the same list sits on the review page for the outside grader.
# Style suggestions are never on it - they go to "nice to have".
PASS_LIST = [
    ("facts", "No wrong or made-up facts", "Every fact matches its source; nothing is invented or stretched."),
    ("place", "Right place", "Right towns, counties and school districts; no other county's rules, fees or sources."),
    ("dates", "Numbers say what they cover", "Every market number says the dates and the homes it covers, and counts that come from different windows aren't mixed up."),
    ("fine_print", "No fine print", "No tax rates, mills, property taxes as a percent, program limits, amounts, legal deadlines or which office collects a closing tax - laws and programs are named only."),
    ("math", "Numbers read correctly", "Statistics are explained correctly (a 100% median sale-to-list ratio doesn't mean half sold above asking)."),
    ("stories", "No invented stories", "No stories, sales, clients or experience the agent didn't give."),
    ("protections", "Reader protections", "The state rules and programs that protect this reader are named in plain words."),
    ("fit", "Fits the reader", "Everything fits this reader: no live-in programs for investors, townhomes not assumed to be condos, nothing that decides a rule applies to them."),
    ("voice", "Agent's own voice", "First person throughout - never \"ask Peter\", \"your agent\" or a third-person sales pitch."),
    ("next_step", "Right next step", "A clear next step that fits the reader, with Ask lines where readers need personal help. The agent's own closing (e.g. \"I'll start your search\") is right in any state."),
    ("complete", "Finished article", "No blanks, placeholders, empty headings or notes to the agent."),
]
PASS_IDS = [p[0] for p in PASS_LIST]
# Failures the engine cuts if its one rewrite didn't fix them - wrong is worse than missing.
CUT_IF_STILL_FAILING = {"facts", "place", "fine_print", "math", "stories"}

_LINT_TO_PASS = {"rule_detail": "fine_print", "stat_logic": "math", "count_mismatch": "math", "overstatement": "facts", "invented_experience": "stories",
                 "condo_townhome": "fit"}
_PLACEHOLDER = re.compile(r"_{3,}|\[(?:insert|add|enter|your|todo|tbd)[^\]]*\]|\bTBD\b|\{\{", re.I)


def code_checks(text: str, brief: dict, protections: list[dict], agent_name: str = "") -> dict[str, list[dict]]:
    """The pass-list items plain code can check: {item id: [{"quote", "why", "fix"}]}."""
    out: dict[str, list[dict]] = {}
    for h in final_lint(text, brief):
        out.setdefault(_LINT_TO_PASS[h["kind"]], []).append({"quote": h["sentence"], "why": h["kind"], "fix": h["fix"]})
    for p in missing_protections(text, protections):
        out.setdefault("protections", []).append({"quote": "", "why": f"never names {p['name']}",
                                                  "fix": f"Add one plain sentence after the most related one: \"{p['says']}\""})
    first = (agent_name or "").split()[0] if agent_name else ""
    third = re.compile(r"\b(?:ask|contact|call|talk to|check with) your (?:real estate )?agent\b" +(rf"|\b(?:ask|contact|call|reach out to|text|email) {re.escape(first)}\b" if first else ""), re.I)
    for s in _sentences(text):
        if third.search(s) and not s.startswith(("SEO title:", "Meta description:")):
            out.setdefault("voice", []).append({"quote": s, "why": "third person", "fix": "Say it in the agent's own first person (\"reach out to me\")."})
        if _PLACEHOLDER.search(s):
            out.setdefault("complete", []).append({"quote": s, "why": "placeholder", "fix": "Remove the unfinished text."})
    return out
