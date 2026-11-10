"""Home features typed into the IDX search box ("4 bed with a pool in Malvern", "finished basement",
"no HOA") - owner, 11/09: no AI. Each feature is matched two ways, and a home counts if EITHER says so:

1. the MLS's own checkboxes in raw_mls_data (Bright: PoolFeatures, GarageYN, Basement, LotFeatures,
   InteriorFeatures, ...) - exact, but listing agents don't always tick them;
2. the listing description (PublicRemarks), with a careful phrase ("in-ground pool", "private pool") so a
   "community pool" or a "pool table" never counts as the home having a pool.

Any other words left over ("sunroom", "wine cellar") are looked for in the description as typed
(see keyword_filter) - content_adapters.get_search_results drops them again if no home mentions them.
"""
import re
from typing import Callable, Optional

from sqlalchemy import and_, func, literal_column, not_, or_

from app.models.database import Property

R = Property.raw_mls_data
_REMARKS = R["PublicRemarks"].astext


def _remarks(pattern: str):
    """The listing description matches a Postgres regex (case-insensitive; \\m = start of a word)."""
    return _REMARKS.op("~*")(pattern)


def _has(col: str, *values: str):
    """An MLS checkbox list (a JSON array) holds any of these values."""
    return or_(*[R[col].contains([v]) for v in values])


def _not_said(col: str, *values: str):
    """The MLS box doesn't hold any of these (true when the listing agent left it empty)."""
    box = func.coalesce(R[col], literal_column("'[]'::jsonb"))
    return and_(*[not_(box.contains([v])) for v in values])


def _yes(col: str):
    return R[col].astext == "true"


# (key, label shown on the Search page, words typed (Python regex, lower-case), the database test)
# Order matters: the longer phrase first ("finished basement" before "basement", "community pool" before "pool").
FEATURES: list[tuple[str, str, str, Callable]] = [
    ("community_pool", "a community pool", r"(?:community|neighborhood|association|hoa) pool",
     lambda: or_(R["Pool"].contains(["Yes - Community"]), _has("AssociationAmenities", "Pool - Outdoor", "Pool - Indoor", "Pool"),
                 _remarks(r"\m(community|neighborhood|association|hoa) pool"))),
    ("pool", "a pool", r"(?:in-?ground |inground |heated |private |swimming |salt ?water |above-?ground )?(?:swimming )?pools?",
     # The MLS's Pool box ("Yes - Personal" / "Yes - Community" / "No Pool") wins when the listing agent filled
     # it in: a condo's "heated pool" for residents, or "the community center with swimming pool", never counts.
     lambda: or_(R["Pool"].contains(["Yes - Personal"]), and_(
                 _not_said("Pool", "No Pool", "Yes - Community"),
                 or_(_has("PoolFeatures", "InGround", "AboveGround", "Indoor", "Heated", "Saltwater", "Gunite", "Lap/Exercise",
                      "Pool/Spa Combo", "Negative Edge/Infinity", "Concrete", "Vinyl"),
                 _remarks(r"\m(in-?ground|heated|private|swimming|salt ?water|gunite|backyard|own|resort[- ]style|infinity) pool"))))),
    ("first_floor_primary", "a first-floor primary bedroom",
     r"(?:first|1st|main|ground|entry)[\s-]*(?:floor|level)[\s-]+(?:primary|master|owner'?s?)(?:[\s-]+(?:bedroom|bed|suite|br|bdrm|retreat))?"
     r"|(?:primary|master|owner'?s?)[\s-]+(?:bedroom|bed|suite)?[\s-]*(?:on (?:the )?)?(?:first|1st|main|ground|entry)[\s-]*(?:floor|level)",
     lambda: _remarks(r"\m(first|1st|main|ground|entry)[- ]?(floor|level) (primary|master|owner'?s?)"
                      r"|\m(primary|master|owner'?s?) (bedroom|suite|bed|retreat)?[ ,]*(is )?(located )?on (the )?(first|1st|main|ground|entry)[- ]?(floor|level)")),
    ("first_floor_bedroom", "a first-floor bedroom",
     r"(?:first|1st|main|ground|entry)[\s-]*(?:floor|level)[\s-]+(?:bedroom|bed|br|bdrm)s?",
     lambda: or_(_has("InteriorFeatures", "Entry Level Bedroom"),
                 _remarks(r"\m(first|1st|main|ground|entry)[- ]?(floor|level) (bedroom|bed\M)"))),
    ("finished_basement", "a finished basement", r"(?:fully |partially )?finished (?:lower level|basement)|basement (?:is )?finished",
     lambda: or_(_has("Basement", "Fully Finished", "Partially Finished"), _remarks(r"\mfinished (basement|lower level)"))),
    ("walkout_basement", "a walk-out basement", r"walk[\s-]?out(?: basement| lower level)?",
     lambda: or_(_has("Basement", "Walkout Level", "Walkout Stairs"), _remarks(r"\mwalk[- ]?out"))),
    ("basement", "a basement", r"basements?",
     lambda: or_(_yes("BasementYN"), _remarks(r"\mbasement"))),
    ("garage", "a garage", r"(?:\d[\s-]*car |attached |detached |oversized )?garages?",
     lambda: or_(_yes("GarageYN"), _remarks(r"\m(\d[- ]?car|attached|detached|oversized) garage"))),
    ("fireplace", "a fireplace", r"fire[\s-]?places?|wood[\s-]?burning",
     lambda: or_(_yes("FireplaceYN"), _remarks(r"\mfire ?place"))),
    ("central_air", "central air", r"central (?:air|a/?c|ac)(?: conditioning)?|air[\s-]?condition(?:ing|ed)?|a/c",
     lambda: or_(_yes("CentralAirYN"), _has("Cooling", "Central A/C"))),
    ("waterfront", "waterfront", r"water[\s-]?front|lake[\s-]?front|river[\s-]?front|on the (?:water|lake|river)",
     lambda: or_(_yes("WaterfrontYN"), _remarks(r"\m(water|lake|river)[- ]?front"))),
    ("no_hoa", "no HOA", r"no (?:hoa|association|hoa fees?)|without (?:an? )?hoa|non[\s-]?hoa",
     lambda: R["AssociationYN"].astext == "false"),
    ("cul_de_sac", "a cul-de-sac", r"cul[\s-]*de[\s-]*sacs?|col[\s-]*de[\s-]*sacks?",
     lambda: or_(_has("LotFeatures", "Cul-de-sac"), _remarks(r"\mcul[- ]?de[- ]?sac"))),
    ("corner_lot", "a corner lot", r"corner lots?",
     lambda: _has("LotFeatures", "Corner")),
    ("quiet_street", "a quiet street", r"no (?:thru|through) (?:street|traffic)|dead[\s-]?end(?: street)?|quiet street",
     lambda: or_(_has("LotFeatures", "No Thru Street"), _remarks(r"\m(quiet|dead[- ]?end|no thru|no through) (street|road)"))),
    ("wooded", "a wooded lot", r"wooded(?: lot)?|backs to (?:trees|woods)|private lot",
     lambda: _has("LotFeatures", "Trees/Wooded", "Partly Wooded", "Backs to Trees", "Secluded", "Private")),
    ("pond_creek", "a pond or creek", r"ponds?|creeks?|streams?",
     lambda: or_(_has("LotFeatures", "Pond", "Stream/Creek"), _remarks(r"\m(pond|creek|stream)\M"))),
    ("fenced_yard", "a fenced yard", r"fenced(?:[\s-]+in)?(?:[\s-]+(?:back|rear))?(?:[\s-]*yard)?",
     lambda: _remarks(r"\mfenced[- ]?(in )?(back|rear|front|side)?[- ]?(yard|backyard|lot|property|garden)|\mfully fenced|\mprivacy fenc")),
    ("deck", "a deck", r"decks?",
     lambda: or_(_has("PatioAndPorchFeatures", "Deck"), _remarks(r"\mdeck\M"))),
    ("screened_porch", "a screened porch", r"screened(?:[\s-]+in)?[\s-]+porch(?:es)?|sun[\s-]?porch",
     lambda: or_(_has("PatioAndPorchFeatures", "Screened", "Enclosed"), _remarks(r"\m(screened[- ]?(in )?|sun ?)porch"))),
    ("porch", "a porch", r"(?:front |wrap[\s-]?around |covered )?porch(?:es)?",
     lambda: or_(_has("PatioAndPorchFeatures", "Porch", "WrapAround", "Screened"), _remarks(r"\mporch"))),
    ("patio", "a patio", r"patios?",
     lambda: or_(_has("PatioAndPorchFeatures", "Patio"), _remarks(r"\mpatio"))),
    ("hot_tub", "a hot tub", r"hot[\s-]?tubs?|jacuzzi|spa tub",
     lambda: or_(_has("ExteriorFeatures", "Hot Tub"), _has("PoolFeatures", "Pool/Spa Combo"), _remarks(r"\m(hot ?tub|jacuzzi)"))),
    ("tennis", "a tennis court", r"tennis(?: courts?)?",
     lambda: _has("ExteriorFeatures", "Tennis Court(s)")),
    ("elevator", "an elevator", r"elevators?",
     lambda: or_(_has("AccessibilityFeatures", "Elevator"), _remarks(r"\melevator"))),
    ("in_law", "an in-law suite", r"in[\s-]?law(?:[\s-]+(?:suite|apartment|apt|quarters))?|au[\s-]?pair(?: suite)?|guest (?:suite|house|cottage)|accessory dwelling|adu",
     lambda: or_(_has("OtherStructuresList", "Guest House", "2nd House", "Carriage House", "Tenant House"),
                 _remarks(r"\m(in[- ]?law|au[- ]?pair|guest (suite|house|cottage)|accessory dwelling|adu\M)"))),
    ("home_office", "a home office", r"(?:home )?offices?|study|den",
     lambda: or_(_has("OtherStructuresList", "Office/Studio"), _remarks(r"\m(home office|office\M|study\M|den\M)"))),
    ("hardwood", "hardwood floors", r"hard[\s-]?wood(?:[\s-]+floor(?:s|ing)?)?|wood floors?",
     lambda: or_(_has("Flooring", "Hardwood", "Solid Hardwood", "Wood", "Engineered Wood"), _has("InteriorFeatures", "Wood Floors"))),
    ("open_floor_plan", "an open floor plan", r"open (?:floor[\s-]?plan|concept|layout)",
     lambda: or_(_has("InteriorFeatures", "Floor Plan - Open"), _remarks(r"\mopen (floor ?plan|concept|layout)"))),
    ("gourmet_kitchen", "a gourmet kitchen", r"(?:gourmet|chef'?s?) kitchen",
     lambda: or_(_has("InteriorFeatures", "Kitchen - Gourmet"), _remarks(r"\m(gourmet|chef'?s?) kitchen"))),
    ("kitchen_island", "a kitchen island", r"(?:kitchen )?island",
     lambda: _has("InteriorFeatures", "Kitchen - Island")),
    ("walk_in_closet", "a walk-in closet", r"walk[\s-]?in closets?",
     lambda: _has("InteriorFeatures", "Walk-in Closet(s)")),
    ("barn", "a barn or stable", r"barns?|horses?|equestrian|stables?|horse property",
     lambda: or_(_has("OtherStructuresList", "Barn", "Barn/Stable", "Horse Stable", "Bank Barn", "Pole Barn", "Barn/Farm Building"),
                 _remarks(r"\m(barn|stable|equestrian|horse)"))),
    ("solar", "solar panels", r"solar(?: panels?| power)?",
     lambda: _remarks(r"\msolar")),
    ("generator", "a generator", r"(?:whole[\s-]?house )?generators?",
     lambda: _remarks(r"\mgenerator")),
    ("ev_charger", "an EV charger", r"ev charg(?:er|ing)|electric (?:car|vehicle) charg(?:er|ing)",
     lambda: _remarks(r"\m(ev|electric (car|vehicle)) charg")),
    ("senior", "a 55+ community", r"55\s*\+|55 (?:and|&) (?:over|older)|age[\s-]restricted|senior (?:community|living)|over 55",
     lambda: _yes("SeniorCommunityYN")),
    ("renovated", "a recent renovation", r"renovated|remodeled|updated|move[\s-]?in ready|turn[\s-]?key",
     lambda: _remarks(r"\m(renovat|remodel|updated|move[- ]in ready|turn[- ]?key)")),
    ("sunroom", "a sunroom", r"sun[\s-]?rooms?|florida rooms?",
     lambda: _remarks(r"\m(sun ?room|florida room)")),
    ("mudroom", "a mudroom", r"mud[\s-]?rooms?",
     lambda: _remarks(r"\mmud ?room")),
    ("wine_cellar", "a wine cellar", r"wine (?:cellar|room|fridge|refrigerator)",
     lambda: _remarks(r"\mwine (cellar|room)")),
    ("views", "views", r"(?:mountain|water|lake|river|city|skyline|panoramic) views?|views?",
     lambda: _remarks(r"\m(mountain|water|lake|river|city|skyline|panoramic|sweeping|breathtaking|scenic|valley|pastoral) views?")),
]
# Home styles - the MLS's ArchitecturalStyle box (or the description saying it).
_STYLES = [
    ("colonial", "colonial", r"colonials?", ("Colonial",)),
    ("ranch", "ranch", r"(?:raised )?ranch(?:er| home| style)?s?|rambler|one[\s-]?story|single[\s-]?story|one[\s-]?level living",
     ("Ranch/Rambler", "Raised Ranch/Rambler")),
    ("cape_cod", "Cape Cod", r"cape[\s-]?cods?|cape style", ("Cape Cod",)),   # never bare "cape" - Cape May
    ("split_level", "split-level", r"split[\s-]?(?:level|foyer)|bi[\s-]?level|tri[\s-]?level", ("Split Level", "Split Foyer", "Bi-level")),
    ("contemporary", "contemporary / modern", r"contemporary|modern(?! kitchen)|mid[\s-]?century(?: modern)?", ("Contemporary", "Mid-Century Modern")),
    ("traditional", "traditional", r"traditional", ("Traditional",)),
    ("craftsman", "craftsman", r"craftsman|bungalow", ("Craftsman", "Bungalow")),
    ("farmhouse", "farmhouse", r"farm[\s-]?house", ("Farmhouse/National Folk",)),
    ("victorian", "Victorian", r"victorian", ("Victorian",)),
    ("tudor", "Tudor", r"tudor", ("Tudor",)),
    ("stone", "stone", r"stone (?:home|house)|fieldstone", ()),
    ("cottage", "cottage", r"cottage", ("Cottage",)),
    ("georgian", "Georgian / Federal", r"georgian|federal", ("Georgian", "Federal")),
]
for _k, _l, _p, _vals in _STYLES:
    # the style box only - "colonial" in a description is as often the town's history as the house
    FEATURES.append((_k, _l, _p, (lambda vals=_vals: _has("ArchitecturalStyle", *vals)) if _vals
                     else (lambda: _remarks(r"\m(field)?stone (home|house|colonial|farmhouse|cottage)|\mfieldstone"))))
_BY_KEY = {k: (label, test) for k, label, _p, test in FEATURES}
_PATTERNS = [(k, re.compile(r"(?<![a-z])(?:an? |with |w/ |has |have |having )?(?:" + p + r")(?![a-z])", re.I)) for k, _l, p, _t in FEATURES]


def extract(text: str) -> tuple[list[str], str]:
    """(feature keys found, the text with them blanked out) - lower-case text in, same length out."""
    keys: list[str] = []
    for key, rx in _PATTERNS:
        m = rx.search(text)
        if m and key not in keys:
            keys.append(key)
            text = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]
    return keys, text


def label(key: str) -> str:
    return _BY_KEY.get(key, (key, None))[0]


def feature_filters(keys: list[str]) -> list:
    """The database tests for these features (each must hold)."""
    return [_BY_KEY[k][1]() for k in keys if k in _BY_KEY]


# Words never worth looking for in a description on their own.
_KEYWORD_STOP = {
    "beautiful", "gorgeous", "stunning", "amazing", "lovely", "perfect", "best", "cheap", "affordable", "luxury", "dream",
    "cute", "cozy", "quiet", "safe", "starter", "forever", "updated", "bedroom", "bedrooms", "bathroom", "bathrooms",
    "bath", "baths", "beds", "bed", "room", "rooms", "space", "kitchen", "floor", "floors", "street", "road", "neighborhood",
    "community", "close", "nearby", "walkable", "downtown", "please", "thanks", "today", "now", "soon", "asap",
}


def keyword_words(words: list[str]) -> list[str]:
    """Leftover words worth looking for in listing descriptions: real words of 4+ letters."""
    return [w for w in words if re.fullmatch(r"[a-z][a-z'-]{3,24}", w or "") and w not in _KEYWORD_STOP][:3]


def keyword_filter(words: list[str]) -> Optional[object]:
    """Every word must appear (as the start of a word) in the description."""
    words = keyword_words(words)
    if not words:
        return None
    return and_(*[_remarks(r"\m" + re.escape(w)) for w in words])
