"""Fact gate for CMA client prose (10/24 grader round: 239 Windham Dr, 417 Dogwood Ln, 102 Steeplewood Dr,
8084 Goshen Rd, 191 Presidential Blvd, 4913 Chancellor St).

The numbers in a report come from the engine; the words around them come from the model. Every check
here compares a claim in the words against the report's own structured data and either rewrites the
claim (when the true version is obvious) or reports the sentence so the repair loop drops it:

  - "same Uwchlan Hunt community" / "same-neighborhood sale" when the two homes' MLS subdivisions differ
    or aren't on record (Windham is in Turnbury; 223 Warren Ct is in Windham, 102 Steeplewood in Lynnbrooke)
  - a subject $/sq ft figure that isn't the report's above-grade list $/sq ft ("the subject's own $256/sqft"
    on a home the cover prices at $369 per above-grade sq ft)
  - "no finished basement" for a home whose MLS record says finished, size not reported
  - internal field names leaking into client text ("no documented_system_updates are listed")
  - the same sentence twice in one section (417 Dogwood Ln: the range-cap sentence, twice in a row)

Pure functions only - no database, no model calls - so every rule has a unit test.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

# ----------------------------------------------------------------------------- sentences

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])(?:[\"'”’)]*)\s+(?=[\"'“(]?[A-Z0-9$#*])")


def sentences(text: str) -> list[str]:
    """Split prose into sentences without breaking '1.02', '$1.2M' or 'St.' followed by a lowercase word."""
    if not text:
        return []
    out: list[str] = []
    for s in _SENT_SPLIT_RE.split(text.strip()):
        if not s or not s.strip():
            continue
        if out and _ABBREV_END_RE.search(out[-1]):
            out[-1] = f"{out[-1]} {s}"
        else:
            out.append(s)
    return out


# "St. Davids", "Mt. Airy", "approx. 2 acres" - a period that doesn't end the sentence
_ABBREV_END_RE = re.compile(r"\b(?:St|Mt|Ft|Dr|Rd|Ave|Ln|Ct|Blvd|No|vs|approx|Jr|Sr|Mr|Mrs|Ms|Inc|Co|e\.g|i\.e)\.$")


def _norm_words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9$%]+", (s or "").lower())


def _place_key(name) -> Optional[str]:
    """MLS subdivision -> a comparable key; None for blanks and Bright's placeholders."""
    s = re.sub(r"[^a-z0-9 ]", " ", str(name or "").lower())
    s = re.sub(r"\b(?:the|subdivision|sub|section|sec|phase|ph|community|estates?|ests?|neighborhood)\b", " ", s)
    s = " ".join(s.split())
    if not s or s in ("none", "none available", "n a", "na", "not applicable", "no", "unknown", "other"):
        return None
    return s


# A "subdivision" that is just the town's own name is not a community (735 Concord Ave, test round 11/13:
# the MLS lists "DREXEL HILL" as the subdivision of every home in Drexel Hill, so the whole town counted as
# the subject's own neighborhood - sales up to two years old joined the comps as "same community", and the
# agent was told three unrelated homes across town were "recent Drexel Hill sales not in this report").
# A switch only so the free past-sales check can run both ways.
CITY_NAME_IS_NOT_A_COMMUNITY = True


def _subdivision_key(row: Optional[dict], *, town_name_counts: bool = False) -> Optional[str]:
    """None when the MLS 'subdivision' is really the township ('UWCHLAN TWP' on 281 Woodland Dr) or the town.
    `town_name_counts` keeps a town's own name (the Match score's "same area" points only - see
    cma_ai_service._REL_TOWN_NAME_SCORES)."""
    r = row or {}
    k = _place_key(r.get("subdivision_name") or r.get("neighborhood"))
    if not k:
        return None
    if CITY_NAME_IS_NOT_A_COMMUNITY and not town_name_counts and k == _place_key(r.get("city")):
        return None
    if re.search(r"\b(?:twp|township|boro|borough|city|county)$", k):
        return None
    town = _place_key(r.get("township"))
    if town and (k == town or k == re.sub(r"\s+(?:twp|township)$", "", town)):
        return None
    return k


def subdivision_of(row: Optional[dict]) -> Optional[str]:
    """The home's MLS subdivision in display form ('UWCHLAN HUNT' -> 'Uwchlan Hunt'), None when not on record."""
    raw = (row or {}).get("subdivision_name") or (row or {}).get("neighborhood")
    if not _subdivision_key(row):
        return None
    return " ".join(w.capitalize() if not re.match(r"^[ivx]+$", w.lower()) else w.upper() for w in str(raw).split())


def building_key(row: Optional[dict]) -> Optional[str]:
    """'191 PRESIDENTIAL BLVD #R-605' and '#805' are one building: house number + street, unit dropped, + ZIP."""
    street = " ".join(str((row or {}).get("street_address") or "").lower().split())
    street = re.split(r"\s*(?:#|\bunit\b|\bapt\b|\bapartment\b|\bste\b|\bsuite\b)", street)[0].strip()
    if not re.match(r"^\d", street):
        return None
    street = re.sub(r"^(\d+)-\d+\b", r"\1", street)
    return f"{street}|{str((row or {}).get('zipcode') or '')[:5]}"


def same_subdivision(a: Optional[dict], b: Optional[dict], *, town_name_counts: bool = False) -> Optional[bool]:
    """True/False when both homes have a subdivision on record (or share a building), None otherwise."""
    ba, bb = building_key(a), building_key(b)
    if ba and ba == bb and re.search(r"#|\bunit\b|\bapt\b", f"{(a or {}).get('street_address')} {(b or {}).get('street_address')}", re.I):
        return True
    ka, kb = _subdivision_key(a, town_name_counts=town_name_counts), _subdivision_key(b, town_name_counts=town_name_counts)
    if not ka or not kb:
        return None
    return ka == kb or ka.startswith(kb + " ") or kb.startswith(ka + " ")


def neighborhood_vs_subject(subject: Optional[dict], comp: Optional[dict]) -> str:
    """One plain line the model can rely on instead of guessing from listing remarks."""
    s, c = subdivision_of(subject), subdivision_of(comp)
    same = same_subdivision(subject, comp)
    if same and building_key(subject) == building_key(comp):
        return "same building as the subject (building amenities are shared - not an advantage of either unit)"
    if same:
        return f"same subdivision as the subject ({c})"
    if same is False:
        return f"different subdivision: {c} (the subject is in {s}) - never call it the same community or neighborhood"
    if c and not s:
        return f"comp subdivision {c}; the subject's subdivision is not on record - do not call them the same community"
    if s and not c:
        return f"comp subdivision not on record (the subject is in {s}) - do not call it the same community"
    return "neither home has a subdivision on record - do not call them the same community or neighborhood"


# "same Uwchlan Hunt community", "the same neighborhood", "same-neighborhood sale", "same subdivision"
_SAME_AREA_RE = re.compile(
    r"\b(?i:same)[- ](?P<name>(?:[A-Z][\w'’.&-]*\s+){1,4}?)?(?P<kind>(?i:community|neighbou?rhood|subdivision|development|enclave|corridor|section|area))\b")
_SUBJECTS_AREA_RE = re.compile(
    r"\b(?i:the\s+subject(?:'s|’s)\s+(?:own\s+)?)(?P<kind>(?i:community|neighbou?rhood|subdivision|development))\b")


def _area_replacement(m: "re.Match", comp: Optional[dict]) -> str:
    kind = m.group("kind").lower()
    name = (m.group("name") or "").strip()
    cs = subdivision_of(comp)
    if name and cs and _place_key(name) == _place_key(cs):
        return f"nearby {name} {kind}"
    if cs and name:
        return f"nearby {cs} {kind}"
    if name:
        return f"nearby {name} {kind}"
    full = m.group(0)
    if full.lower().startswith("same-"):
        return "nearby"
    return f"nearby {kind}" if not cs else f"nearby {cs} {kind}"


def fix_community_claims(text: str, subject: Optional[dict], comp: Optional[dict]) -> tuple[str, list[str]]:
    """Rewrite 'same <name> community' claims the two homes' MLS subdivisions don't back up. `comp` is the
    home the text is about (a comp write-up); None = can't tell which home, so only named claims that
    contradict the subject's own subdivision are touched."""
    if not text:
        return text, []
    flags: list[str] = []
    s_sub = subdivision_of(subject)

    def _ok(m: "re.Match") -> bool:
        name = (m.group("name") or "").strip()
        if m.group("kind").lower() == "area" and not name:
            return True     # "the same area" is a loose phrase, not a subdivision claim
        # "the same Willistown area" names a township/city both homes are in - true, not a subdivision claim
        if name and all(_place_key(name) in {_place_key((r or {}).get(k)) for k in ("township", "city")}
                        for r in ((subject, comp) if comp is not None else (subject,))):
            return True
        if comp is None:
            # no comp in view: a named community must at least be the subject's
            return not name or (s_sub is not None and _place_key(name) == _place_key(s_sub))
        if same_subdivision(subject, comp) is not True:
            return False
        return not name or _place_key(name) == _place_key(s_sub)

    def _sub(m: "re.Match") -> str:
        if _ok(m):
            return m.group(0)
        # a school district's own name ("West Chester Area School District"), or a loosely named area / corridor /
        # section that is neither home's subdivision, is not a subdivision claim (deep dive 11/13: it was replaced
        # with the comp's subdivision name)
        if re.match(r"\s+(?:school|sd\b|district)", text[m.end():], re.I):
            return m.group(0)
        _name = (m.group("name") or "").strip()
        if (_name and m.group("kind").lower() in ("area", "corridor", "section")
                and _place_key(_name) not in {_place_key(s_sub), _place_key(subdivision_of(comp))}):
            return m.group(0)
        new = _area_replacement(m, comp)
        flags.append(f"Fact check: '{m.group(0).strip()}' changed to '{new}' - the MLS subdivisions are "
                     f"{subdivision_of(comp) or 'not on record'} (comp) and {s_sub or 'not on record'} (subject).")
        return new

    out = _SAME_AREA_RE.sub(_sub, text)
    if comp is not None and same_subdivision(subject, comp) is not True:
        def _sub2(m: "re.Match") -> str:
            new = f"a nearby {m.group('kind').lower()}"
            flags.append(f"Fact check: '{m.group(0).strip()}' changed to '{new}' - not the subject's MLS subdivision.")
            return new
        out = re.sub(r"\b(?i:in)\s+" + _SUBJECTS_AREA_RE.pattern, lambda m: "in " + _sub2(m), out)
    out = re.sub(r"\ba nearby (?=[aeiou])", "a nearby ", out)
    return out, flags


# ----------------------------------------------------------------------------- $/sq ft

def above_grade_sqft(row: Optional[dict]) -> Optional[float]:
    r = row or {}
    for k in ("above_grade_finished_area", "sqft"):
        try:
            v = float(r.get(k)) if r.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            v = None
        if v and v > 0:
            return v
    return None


def subject_list_psf(subject: Optional[dict]) -> Optional[float]:
    try:
        lp = float((subject or {}).get("list_price") or 0)
    except (TypeError, ValueError):
        lp = 0
    ag = above_grade_sqft(subject)
    return (lp / ag) if lp and ag else None


_SUBJECT_PSF_RE = re.compile(
    # "the subject's list-price rate of $256/sqft" (11/01 regen, 289 Compass Rd - it is $250): "rate of" too
    r"\bsubject(?:'s|’s)\s+(?:own\s+)?(?:(?:list|asking)(?:[- ]price)?\s+)?"
    r"(?:(?:rate|price\s+per\s+(?:square\s+)?(?:foot|sq\.?\s?ft))\s+(?:of\s+)?)?(?:\$/sq\.?\s?ft\s+(?:of\s+)?)?"
    r"\$\s?(?P<n>\d{2,4}(?:\.\d+)?)\s*(?:/\s*sq\.?\s?(?:ft|foot)|per\s+(?:square\s+)?(?:foot|ft))", re.I)


def fix_subject_psf_numbers(text: str, subject: Optional[dict]) -> tuple[str, list[str]]:
    """A wrong $/sq ft attributed to the subject is set to its real above-grade list $/sq ft (instead of
    dropping the whole sentence)."""
    s_psf = subject_list_psf(subject)
    if not text or not s_psf:
        return text, []
    flags: list[str] = []

    def _fix(m):
        n = float(m.group("n"))
        if abs(n - s_psf) <= max(2.0, 0.01 * s_psf):
            return m.group(0)
        flags.append(f"Fact check: the subject's $/sq ft was given as ${n:,.0f} - set to ${s_psf:,.0f}, its list "
                     "price per above-grade sq ft.")
        a, b = m.start("n") - m.start(), m.end("n") - m.start()
        return m.group(0)[:a] + f"{s_psf:,.0f}" + m.group(0)[b:]
    return _SUBJECT_PSF_RE.sub(_fix, text), flags


def subject_psf_claim_problem(sentence: str, subject: Optional[dict]) -> Optional[str]:
    """A $/sq ft figure attributed to the subject must be its above-grade list $/sq ft (what the cover shows)."""
    s_psf = subject_list_psf(subject)
    if not s_psf:
        return None
    for m in _SUBJECT_PSF_RE.finditer(sentence or ""):
        n = float(m.group("n"))
        if abs(n - s_psf) > max(2.0, 0.01 * s_psf):
            return (f"gives the subject's $/sq ft as ${n:,.0f}, but its list price is ${s_psf:,.0f} per above-grade "
                    "sq ft (the basis used everywhere in this report)")
    return None


# ----------------------------------------------------------------------------- lower level

def lower_level_state(row: Optional[dict]) -> str:
    """'measured' (finished area on record) | 'finished_unknown_size' | 'unfinished' | 'none' | 'unknown'."""
    r = row or {}
    try:
        bg = float(r.get("below_grade_finished_area") or 0)
    except (TypeError, ValueError):
        bg = 0.0
    if bg > 0:
        return "measured"
    b = str(r.get("basement") or "").lower()
    if isinstance(r.get("basement"), (list, tuple)):
        b = ", ".join(str(x) for x in r.get("basement")).lower()
    if re.search(r"(?<!un)finished", b):
        return "finished_unknown_size"
    if "unfinished" in b:
        return "unfinished"
    if b.strip() in ("none", "no", "slab", "crawl space", "crawlspace", "no basement"):
        return "none"
    return "unknown"


_NO_FINISHED_LL_RE = re.compile(
    r"\b(?:no|without(?:\s+a)?|lacks?(?:\s+a)?|lacking(?:\s+a)?)\s+(?:any\s+)?finished\s+"
    r"(?:basement|lower[- ]level|below[- ]grade(?:\s+(?:space|area))?)\b", re.I)


def fix_lower_level_claims(text: str, row: Optional[dict], who: str) -> tuple[str, list[str]]:
    """'no finished basement' for a home the MLS says has one (size unrecorded) -> 'a finished basement of
    unrecorded size'. A home with measured finished area gets the claim removed by the caller (block)."""
    if not text or lower_level_state(row) != "finished_unknown_size":
        return text, []
    flags: list[str] = []

    def _sub(m: "re.Match") -> str:
        # "..., while the subject has no finished basement" is about the subject, not this comp (deep dive 11/13)
        if _about_subject(text, m.start()):
            return m.group(0)
        word = m.group(0).split()[0].lower()
        noun = "lower level" if "lower" in m.group(0).lower() else "basement"
        new = (f"a finished {noun} of unrecorded size" if word in ("no", "without", "lacks", "lack", "lacking")
               else f"a finished {noun} of unrecorded size")
        flags.append(f"Fact check: {who} - '{m.group(0)}' changed: the MLS lists a finished {noun} (size not reported).")
        return ("has " + new) if word in ("lacks", "lack") else new

    out = _NO_FINISHED_LL_RE.sub(_sub, text)
    out = re.sub(r"\bdespite having has\b", "despite having", out)
    out = re.sub(r"\bwith (?:has )?a finished", "with a finished", out)
    return out, flags


def lower_level_claim_problem(sentence: str, row: Optional[dict]) -> Optional[str]:
    if lower_level_state(row) == "measured" and _NO_FINISHED_LL_RE.search(sentence or ""):
        return "says there is no finished lower level, but the MLS records finished below-grade area"
    return None


# ----------------------------------------------------------------------------- field names

_FIELD_NAME_RE = re.compile(r"\b[a-z]{2,}(?:_[a-z0-9]{2,}){1,5}\b")


def fix_field_name_leaks(text: str) -> tuple[str, list[str]]:
    """'(no documented_system_updates are listed)' -> removed; a bare snake_case word -> plain words."""
    if not text or not _FIELD_NAME_RE.search(text):
        return text, []
    flags: list[str] = []

    def _paren(m: "re.Match") -> str:
        if _FIELD_NAME_RE.search(m.group(0)):
            flags.append(f"Fact check: removed an internal field name from client text ({m.group(0)[:60]}).")
            return ""
        return m.group(0)
    out = re.sub(r"\s*\([^()]{0,160}\)", _paren, text)

    def _word(m: "re.Match") -> str:
        flags.append(f"Fact check: '{m.group(0)}' written out in plain words.")
        return m.group(0).replace("_", " ")
    out = _FIELD_NAME_RE.sub(_word, out)
    return re.sub(r"\s+([.,;:])", r"\1", out), flags


# ----------------------------------------------------------------------------- repetition

def _similar(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# Words that make two look-alike sentences about DIFFERENT things ("check the roof's age" / "check the
# furnace's age" overlap 0.78 but are not a repeat - rule audit 10/24)
_DISTINCT_TOPIC_WORDS = set((
    "roof furnace hvac heater boiler ac plumbing electrical wiring panel windows siding stucco foundation basement "
    "deck patio pool spa septic well sewer water chimney fireplace kitchen bath bathroom garage driveway solar "
    "attic insulation gutters radon mold termite lot comp subject buyer seller sale sales pending listing "
    # deep dive 11/13: "Have the oil tank inspected ..." was dropped as a repeat of the roof line
    "oil tank sump pump drainage grading asbestos lead paint cesspool ducts ductwork masonry stairs porch "
    "fence shed barn outbuilding pond dock elevator sprinkler generator propane cistern skylights flashing "
    "joists beams crawlspace crawl slab easement survey permit permits zoning flood association hoa").split())


def _parallel(sentence: str, w: list[str], prev: list[str]) -> bool:
    """True when the words two similar sentences DON'T share name different things - a number, a name
    (capitalised word), a comp number or a house system - so the later one is a parallel point, not a repeat."""
    diff = set(w) - set(prev)
    if any(re.search(r"\d|\$|%", d) or d in _DISTINCT_TOPIC_WORDS for d in diff):
        return True
    caps = {c.lower() for c in re.findall(r"(?<=\s)[A-Z][a-z]+", sentence or "")}
    return bool(caps & diff)


def drop_repeated_sentences(text: str, threshold: float = 0.72) -> tuple[str, list[str]]:
    """Drop a sentence that says what an earlier sentence in the same section already said (word overlap,
    so 'The range tops out at...' and 'The range stops at...' count as the same). Paragraph breaks kept."""
    if not text:
        return text, []
    flags: list[str] = []
    seen: list[list[str]] = []
    paras_out = []
    for para in re.split(r"(\n\s*\n)", text):
        if re.fullmatch(r"\n\s*\n", para or ""):
            paras_out.append(para)
            continue
        kept = []
        for s in sentences(para):
            w = _norm_words(s)
            if len(w) >= 7 and any(_similar(w, p) >= threshold and not _parallel(s, w, p) for p in seen):
                flags.append(f"Fact check: removed a repeated sentence (\"{s[:80]}\").")
                continue
            if len(w) >= 7:
                seen.append(w)
            kept.append(s)
        paras_out.append(" ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "".join(paras_out)).strip(), flags


# ----------------------------------------------------------------------------- lot size sanity

def implausible_lot(acres: Optional[float], price: Optional[float], home_type: Optional[str] = None) -> Optional[str]:
    """A lot no home of this price sits on: 523 Sherbrook Dr (a 2001 house in a subdivision with a $103/month
    HOA) came through Bright as 18,774,360 sq ft = 431 acres. Land alone costs well over $5,000/acre around
    here, so a sale below that per acre on 20+ acres - or anything over 1,000 acres - is a data-entry error."""
    try:
        a = float(acres) if acres not in (None, "") else None
        p = float(price) if price not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if not a or a <= 0:
        return None
    if a > 1000:
        return f"lot size of {a:,.0f} acres is not plausible"
    if a >= 20 and p and p / a < 5000:
        return f"lot size of {a:,.0f} acres is not plausible for a ${p:,.0f} sale (${p / a:,.0f} per acre)"
    if str(home_type or "").upper() in ("CONDO", "CONDOMINIUM", "APARTMENT") and a >= 5:
        return f"a {a:,.0f}-acre lot on a condo unit is the building's land, not the unit's"
    return None


# ----------------------------------------------------------------------------- one pass over a text

# ----------------------------------------------------------------------------- size comparisons

_SIZE_SIMILAR = 0.06    # within 6% above grade = the same size to a buyer
_SIZE_CLAIM_RE = re.compile(
    r"\b(?:(much|considerably|significantly|substantially|far|notably|a\s+good\s+deal)\s+)?(larger|bigger|smaller)"
    r"(?=\s*(?:\([^)]*\)\s*)?,?\s*(?:home|house|residence|colonial|footprint|floor\s*plan|property|newer|older|\w+-?story|\())",
    re.I)
_SIZE_SAKE_RE = re.compile(r"\b[Ii]ts\s+(larger|smaller|bigger)\s+size\b")


def _size_ratio(subject: Optional[dict], comp: Optional[dict]) -> Optional[float]:
    s, c = above_grade_sqft(subject), above_grade_sqft(comp)
    if not (s and c):
        s, c = _as_num((subject or {}).get("sqft")), _as_num((comp or {}).get("sqft"))
    return c / s if s and c else None


def _as_num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def fix_size_claims(text: str, subject: Optional[dict], comp: Optional[dict]) -> tuple[str, list[str]]:
    """'A much larger home' about a comp within 6% of the subject's above-grade size (629 S Warren Ave,
    4,857 vs 4,870 sq ft, 10/24) -> 'similarly sized'; a claim in the wrong direction -> the right one."""
    r = _size_ratio(subject, comp)
    if not text or r is None:
        return text, []
    flags: list[str] = []

    def sub(m: "re.Match") -> str:
        # only a claim ABOUT this comp ("it is a much larger home") - not a general phrase like
        # "relative to a smaller home" or "than a larger house"
        if re.search(r"\b(?:to|than|from|vs\.?|versus|of|like|unlike)\s+(?:an?|the|any)?\s*$", text[max(0, m.start() - 25):m.start()], re.I):
            return m.group(0)
        # "The subject is a larger home" is about the subject (deep dive 11/13: it was flipped to "smaller")
        if _about_subject(text, m.start(), 30):
            return m.group(0)
        said_bigger = m.group(2).lower() in ("larger", "bigger")
        if abs(r - 1) < _SIZE_SIMILAR:
            flags.append(f"called a comp {m.group(0).strip()!r}, but it is about the same size as the subject")
            return "similarly sized"
        if said_bigger != (r > 1) and abs(r - 1) >= 0.10:
            flags.append(f"called a comp {m.group(0).strip()!r}, but it is {'larger' if r > 1 else 'smaller'} than the subject")
            return ("larger" if r > 1 else "smaller")
        return m.group(0)

    out = _SIZE_CLAIM_RE.sub(sub, text)
    return out, flags


# "...on a smaller footprint (1,760 vs 2,010 sqft), which largely explains why its $206/sqft lands below the
# subject's $224" - backwards: smaller homes sell for MORE per square foot (grader 10/24, 4913 Chancellor St;
# the same report's 5146 Sansom St note had it right). The causal link goes; the facts stay.
_CAUSE_RE = re.compile(r",?\s*(?:which|this|that)\s+(?:largely\s+|mostly\s+|partly\s+|likely\s+|probably\s+|mainly\s+)?"
                       r"(?:explains|accounts\s+for)\s+why\b|,?\s*(?:largely\s+|mostly\s+)?explaining\s+why\b", re.I)
_PSF_DIR_RE = re.compile(r"(?:\$\s*[\d,]+\s*(?:per|/)\s*(?:above[- ]grade\s+)?(?:sq\.?\s*ft|sqft|square\s+f(?:oo|ee)t)|"
                         r"per[- ](?:square[- ]f(?:oo|ee)t|sq\.?\s*ft))[^.;]{0,80}?\b(below|under|lower|less|beneath|above|over|higher|more)\b", re.I)


def fix_size_psf_logic(text: str, subject: Optional[dict], comp: Optional[dict]) -> tuple[str, list[str]]:
    r = _size_ratio(subject, comp)
    if not text or r is None or abs(r - 1) < _SIZE_SIMILAR:
        return text, []
    flags: list[str] = []
    out = []
    for s in sentences(text):
        m, d = _CAUSE_RE.search(s), _PSF_DIR_RE.search(s)
        if m and d and d.start() > m.start():
            lower = d.group(1).lower() in ("below", "under", "lower", "less", "beneath")
            if (r < 1 and lower) or (r > 1 and not lower):
                s = s[:m.start()] + ", and" + s[m.end():]
                flags.append(f"Fact check: dropped 'explains why' - a {'smaller' if r < 1 else 'larger'} home usually sells for "
                             f"{'more' if r < 1 else 'less'} per square foot, not {'less' if r < 1 else 'more'}.")
        out.append(s)
    return (" ".join(out), flags) if flags else (text, [])


def size_claim_problem(sentence: str, subject: Optional[dict], comp: Optional[dict]) -> Optional[str]:
    r = _size_ratio(subject, comp)
    m = _SIZE_SAKE_RE.search(sentence or "")
    if r is not None and m and abs(r - 1) < _SIZE_SIMILAR:
        return f"explains a price by the comp's {m.group(1).lower()} size, but it is about the same size as the subject"
    return None


# ----------------------------------------------------------------------------- hand-entered subject

# A home entered by hand has no MLS record - 602 Media Line Rd's report said "The MLS record shows no
# documented updates to the heating, cooling ..." and called the home unrenovated (grader 10/25). A fact
# nobody entered is unknown, never a negative.
_MANUAL_NO_UPDATES_RE = re.compile(
    r"\b(?:the|its|this home's|the home's|the subject's)\s+MLS\s+(?:record|listing|data|remarks|description)s?\s+"
    r"(?:shows?|lists?|documents?|has|notes?|includes?)\s+no\s+(?:documented\s+|recorded\s+|listed\s+)?"
    r"(?:updates?|upgrades?|renovations?|improvements?)\s+(?:to|of|for|on)\s+(?:the\s+)?([^.;]+)", re.IGNORECASE)
_MANUAL_RECORD_RE = re.compile(
    r"\b(the|its|this home's|the home's|the subject's|the subject property's)\s+MLS\s+(record|listing|data|remarks|description)s?\b",
    re.IGNORECASE)


def fix_manual_subject_record(text: str, subject: Optional[dict], comp: Optional[dict] = None) -> tuple[str, list[str]]:
    if not text or (subject or {}).get("source") != "manual" or comp is not None:
        return text, []
    t = _MANUAL_NO_UPDATES_RE.sub(lambda m: f"The details provided don't say when the {m.group(1).strip()} were last updated", text)
    # (not a sentence about a comp: "Comp #1's sale price is verified in the MLS data" stays - deep dive 11/13)
    t = _MANUAL_RECORD_RE.sub(
        lambda m: m.group(0) if re.search(r"\bcomp(?:arable)?s?\b|#\s*\d", t[max(0, m.start() - 60):m.start()], re.I)
        else ("The" if m.group(1)[0].isupper() else "the") + " details provided for the home", t)
    return (t, ["Fact check: this home was entered by hand - 'the MLS record' about it rewritten, and missing update "
                "details stated as unknown, not as none."]) if t != text else (text, [])


# "Presenting the home's strengths - smaller lot, ..." for a 0.48-acre lot bigger than four of five comps.
_SUBJECT_LOT_WORD_RE = re.compile(r"\b(smaller|larger|bigger)\s+(lot|parcel|yard)\b", re.IGNORECASE)
_OTHER_HOME_REF_RE = re.compile(r"\bComp\s*#\s*\d|\b\d{2,5}\s+[A-Z][a-zA-Z]+\s+(?:Rd|Road|St|Street|Ave|Ln|Lane|Dr|Drive|Ct|Cir|Pl|Way)\b")


def fix_subject_lot_words(text: str, subject: Optional[dict], comps: Optional[list] = None) -> tuple[str, list[str]]:
    lot = _money((subject or {}).get("lot_size"))
    others = sorted(v for v in (_money(c.get("lot_size")) for c in comps or [] if c.get("is_included", True)) if v)
    if not text or not lot or len(others) < 2:
        return text, []
    mid = others[len(others) // 2] if len(others) % 2 else (others[len(others) // 2 - 1] + others[len(others) // 2]) / 2
    out, flags = [], []
    for s in sentences(text):
        m = _SUBJECT_LOT_WORD_RE.search(s)
        # ... nor one about the comps as a group ("Most of the comparable sales sit on a larger lot than this
        # home" was flipped to "smaller" - deep dive 11/13)
        if m and not _OTHER_HOME_REF_RE.search(s[:m.start()]) \
                and not re.search(r"\bcomp(?:arable)?s?(?:'|\u2019)?\b|\bsales\b|\bneighbou?rs?\b|\bother homes\b", s[:m.start()], re.I):
            wrong = (m.group(1).lower() == "smaller" and lot >= mid * 0.95) or (m.group(1).lower() != "smaller" and lot <= mid * 1.05)
            if wrong:
                word = "larger" if lot > mid * 1.05 else "smaller" if lot < mid * 0.95 else "similar-size"
                s = s[:m.start()] + f"{word} {m.group(2)}" + s[m.end():]
                flags.append(f"Fact check: dropped '{m.group(0)}' - the home's lot ({lot / 43560:.2f} acres) isn't "
                             f"{m.group(1).lower()} than the comps' typical {mid / 43560:.2f} acres.")
        out.append(s)
    return (" ".join(out), flags) if flags else (text, [])


# 2 Paxon Cir: "once its ~734 sqft of finished lower level is backed out of its blended $315/sqft above grade" -
# the $315 is already per ABOVE-grade foot; nothing is backed out (grader 10/25).
_BACKED_OUT_PSF_RE = re.compile(
    r"\s*once its\s+~?[\d,]+\s*(?:sq\.?\s*ft|sqft|square feet)\s+of finished (?:lower level|basement)\s+(?:space\s+)?is\s+"
    r"(?:backed|taken|stripped)\s+out\s+of its\s+(?:blended\s+)?(\$[\d,]+)\s*/\s*(?:sq\.?\s*ft|sqft)\s+above[- ]grade", re.IGNORECASE)
_BLENDED_ABOVE_RE = re.compile(r"\bblended\s+(\$[\d,]+\s*/\s*(?:sq\.?\s*ft|sqft)\s+above[- ]grade)", re.IGNORECASE)


def fix_above_grade_psf_wording(text: str) -> tuple[str, list[str]]:
    if not text:
        return text, []
    t = _BACKED_OUT_PSF_RE.sub(lambda m: f" at its {m.group(1)} per above-grade sq ft (its finished lower level isn't in that figure)", text)
    t = re.sub(r"(isn't in that figure\))(\s+[A-Z])", r"\1.\2", t)
    t = _BLENDED_ABOVE_RE.sub(r"\1", t)
    return (t, ["Fact check: an above-grade $/sq ft figure was described as needing the lower level backed out - reworded."]) \
        if t != text else (text, [])


# "at $303 per above-grade square foot, well above the subject's $344 list rate" (CMA test 11/13, 418
# Reedville Rd comp #1) - the direction word between a comp's $/sq ft and the subject's must match the numbers.
_PSF_VS_SUBJECT_RE = re.compile(
    r"\$\s?(?P<a>\d{2,4}(?:\.\d+)?)\s*(?:per|/)\s*(?:above[- ]grade\s+)?(?:sq\.?\s*ft|sqft|square\s+f(?:oo|ee)t)"
    r"(?P<mid>[^.;$]{0,70}?)"
    r"\b(?P<adv>(?:well|far|much|meaningfully|significantly|notably|substantially|slightly|just|somewhat|modestly|a\s+bit)\s+)?"
    r"(?P<dir>above|below|under|over|higher\s+than|lower\s+than)\s+the\s+subject(?:'s|’s)?\s+"
    r"(?:(?:own\s+)?(?:above[- ]grade\s+)?\$\s?(?P<b>\d{2,4}(?:\.\d+)?)"
    r"|(?:own\s+)?(?:above[- ]grade\s+)?(?:(?:list|asking)(?:[- ]price)?\s+)?(?:rate|\$/sq\.?\s?ft|price\s+per\s+(?:square\s+foot|sq\.?\s?ft))"
    r"(?:\s+of\s*\$\s?(?P<b2>\d{2,4}(?:\.\d+)?))?)", re.I)
_PSF_DIFF_WORDS_RE = re.compile(r"\b(?:premium|discount|more|less|gap|difference|spread|increase|decrease)\b", re.I)
_BIG_ADV = ("well", "far", "much", "meaningfully", "significantly", "notably", "substantially")
_SMALL_ADV = ("slightly", "just", "somewhat", "modestly", "a bit")


def fix_psf_direction(text: str, subject: Optional[dict]) -> tuple[str, list[str]]:
    """'$303 ... well above the subject's $344' -> 'well below'. The subject's figure is the one in the
    sentence, else its above-grade list $/sq ft. A wrong-size adverb is fixed with it ('well' for a
    gap over 10%, none under 5%); a right direction is left alone."""
    if not text:
        return text, []
    s_psf = subject_list_psf(subject)
    flags: list[str] = []

    def _fix(m):
        a = float(m.group("a"))
        b = float(m.group("b") or m.group("b2") or 0) or s_psf
        if not b:
            return m.group(0)
        # A DIFFERENCE, not the comp's own rate ("closed about $20 per sq ft above the subject's $344",
        # "a $40/sqft premium over the subject's $344") is already right - deep dive 11/13: both were flipped.
        if a < 0.5 * b or _PSF_DIFF_WORDS_RE.search(m.group("mid") or ""):
            return m.group(0)
        d = m.group("dir").lower()
        says_lower = d in ("below", "under", "lower than")
        gap = abs(a - b) / b
        if gap < 0.01 or says_lower == (a < b):
            return m.group(0)
        word = ("below" if d in ("above", "below") else "under" if d in ("over", "under")
                else "lower than") if a < b else ("above" if d in ("above", "below") else "over" if d in ("over", "under") else "higher than")
        adv = (m.group("adv") or "").strip().lower()
        if adv in _BIG_ADV and gap < 0.05:
            adv = ""
        elif adv in _SMALL_ADV and gap > 0.10:
            adv = "well"
        flags.append(f"Fact check: ${a:,.0f}/sq ft was called '{(m.group('adv') or '') + m.group('dir')}' the subject's "
                     f"${b:,.0f} - it is {'lower' if a < b else 'higher'}.")
        start, end = (m.start("adv") if m.group("adv") else m.start("dir")) - m.start(), m.end("dir") - m.start()
        return m.group(0)[:start] + (f"{adv} " if adv else "") + word + m.group(0)[end:]
    out = _PSF_VS_SUBJECT_RE.sub(_fix, text)
    return (out, flags) if flags else (text, [])


# "a quiet cul-de-sac, which is a genuine plus versus the subject's more trafficked road setting" (CMA test
# 11/13, 701 Sturbridge Dr - a subdivision street, and the road lookup had returned nothing). A busier-road
# claim about the subject needs the road data (or a road-type street name) behind it.
_SUBJECT_BUSY_ROAD_RE = re.compile(
    r",?\s*(?:versus|vs\.?|compared\s+(?:with|to)|unlike|over|rather\s+than)\s+the\s+subject(?:'s|’s)\s+"
    r"(?:(?:more|much\s+more|busier|heavily|higher)[- ]?)?(?:trafficked|busy|busier|high[- ]traffic|heavier[- ]traffic|"
    r"well[- ]traveled|through)(?:[- ](?:road|street))?(?:\s+(?:setting|location|frontage|exposure|position))?", re.I)
_THROUGH_STREET_WORDS = ("pike", "highway", "hwy", "route", "rt", "boulevard", "blvd", "road", "rd", "avenue", "ave")


def _subject_road_may_be_busy(subject: Optional[dict]) -> bool:
    verdict = str(((subject or {}).get("road_context") or {}).get("verdict") or "")
    if verdict in ("quiet", "neutral"):
        return False
    if verdict == "through":
        return True
    street = re.split(r"\s*#", str((subject or {}).get("street_address") or ""))[0].strip().lower()
    words = street.split()
    if not words:
        return False
    last = words[-1].rstrip(".")
    # "Main St", "High Street", "Market St" are usually a town's through streets (deep dive 11/13)
    if last in ("street", "st") and any(w in ("main", "high", "market", "broad", "state", "front") for w in words[:-1]):
        return True
    return last in _THROUGH_STREET_WORDS


def fix_subject_busy_road_claim(text: str, subject: Optional[dict]) -> tuple[str, list[str]]:
    """Drops 'versus the subject's more trafficked road' when nothing says the subject's road is busy
    (road lookup quiet/neutral, or none and a Drive/Lane/Court-type street). Only a clause that ENDS
    there is cut (deep dive 11/13: "a plus over the subject's busier Lancaster Avenue frontage" lost its
    middle, and "Unlike the subject's busy road, this comp ..." was left starting with a comma)."""
    if not text or _subject_road_may_be_busy(subject) or not _SUBJECT_BUSY_ROAD_RE.search(text):
        return text, []
    out, last, changed = [], 0, False
    for m in _SUBJECT_BUSY_ROAD_RE.finditer(text):
        after = text[m.end():]
        if not re.match(r"\s*(?:[,.;:)!?]|$|(?:and|but|while|which|so)\b)", after):
            continue                                     # the clause runs on (a road name follows): leave it
        before = text[last:m.start()]
        at_start = not (text[:m.start()].strip()) or bool(re.search(r"[.!?]\s*$", text[:m.start()]))
        if at_start:                                     # "Unlike the subject's busy road, this comp ..."
            lead = re.match(r"\s*,\s*", after)
            if not lead:
                continue
            rest_at = m.end() + lead.end()
            out.append(before + (" " if before and not before.endswith(" ") else ""))
            out.append(text[rest_at:rest_at + 1].upper())
            last = rest_at + 1
        else:
            out.append(before)
            last = m.end()
        changed = True
    if not changed:
        return text, []
    out.append(text[last:])
    return "".join(out), ["Fact check: dropped a claim that the subject is on a busier road - nothing on record says so."]


# "a production new-construction home on a roughly 8.8-acre lot" (CMA test 11/13, 418 Reedville Rd comp #5 -
# no lot size on record; its remarks say the community's lots run .21-.40 acres).
_COMP_ACRE_LOT_RE = re.compile(
    r"\bon\s+an?\s+(?P<q>(?:roughly|about|approximately|approx\.?|nearly|almost|just\s+over|just\s+under|over|under)\s+)?"
    r"(?P<n>\d{1,3}(?:\.\d{1,2})?)[- ]acre\s+(?P<noun>lot|homesite|parcel|property|site)\b", re.I)


def _lot_acres(d: Optional[dict]) -> Optional[float]:
    """The lot in acres from the same fields, with the same plausibility test, as lot_acres_text (deep dive
    11/13: reading lot_size alone wrote a known-bad "431.00-acre" lot into a note and deleted the correct
    figure of a comp whose record only has acres)."""
    d = d or {}
    sqft = _money(d.get("lot_size")) or _money(d.get("lot_size_sqft"))
    acres = sqft / 43560 if sqft else _money(d.get("lot_size_acres")) or _money(d.get("acres"))
    if not acres or implausible_lot(acres, _money(d.get("sale_price")) or _money(d.get("list_price")), d.get("home_type")):
        return None
    return acres


def _about_subject(text: str, pos: int, span: int = 40) -> bool:
    """The words just before `pos` are about the subject ("the subject, which sits on a ...")."""
    return bool(re.search(r"\bsubject\b|\bthis home\b", text[max(0, pos - span):pos], re.I))


def fix_comp_lot_acres(text: str, subject: Optional[dict], comp: Optional[dict]) -> tuple[str, list[str]]:
    """A comp note's 'on a X-acre lot' must be the comp's recorded lot: none on record -> the number goes;
    a different number -> the record's. A figure about the SUBJECT (notes contrast with it) is left alone."""
    if not text or comp is None:
        return text, []
    actual, subj = _lot_acres(comp), _lot_acres(subject)
    flags: list[str] = []

    def _fix(m):
        n = float(m.group("n"))
        if _about_subject(text, m.start()) or (subj and abs(n - subj) <= max(0.05, 0.1 * subj)):
            return m.group(0)
        if actual is None:
            flags.append(f"Fact check: dropped a {n:g}-acre lot size - none is on record for this comparable.")
            return f"on a {m.group('noun')}"
        if abs(n - actual) <= max(0.05, 0.1 * actual):
            return m.group(0)
        flags.append(f"Fact check: a {n:g}-acre lot was set to {actual:.2f} acres, this comparable's recorded lot.")
        return f"on a {(m.group('q') or '')}{actual:.2f}-acre {m.group('noun')}"
    out = _COMP_ACRE_LOT_RE.sub(_fix, text)
    return (out, flags) if flags else (text, [])


# ---- Facts that must agree across the whole report (grader audit 11/13, finding 26) -----------------
# One Center City condo report said "mid-rise" in one section and "high-rise" in another, showed a comp
# on "Public Sewer, Public Septic", and discussed a "one-bedroom-plus-den" under a 2-bedroom cover; a
# Drexel Hill note called a comp's unfinished basement "a step down from the subject's full basement"
# when the subject's own basement has no finished area on record. Four kinds of conflict, four rules.

_UNIT_FLOOR_RE = re.compile(r"(?:#|\bunit\s+|\bapt\.?\s+|\bph)\s*-?\s*[a-z]?-?(\d{3,4})[a-z]?\b", re.IGNORECASE)
_HEIGHT_WORD_RE = re.compile(r"\b(low|mid|high)([- ])rise\b", re.IGNORECASE)
_TALL_BUILDING_WORDS_RE = re.compile(r"high[- ]rise|\btower\b|sky ?line|\b\d{2}(?:st|nd|rd|th)[- ]floor|\b\d{2}[- ]stor(?:y|ies)\b", re.IGNORECASE)
_OTHER_HOME_RE = re.compile(r"\bcomp(?:arable)?s?\b|\bcomp\s*#|#\s*\d", re.IGNORECASE)


def building_height_class(subject: Optional[dict]) -> Optional[str]:
    """'high' when a CONDO is plainly in a tall building, else None. Our MLS copy has no floor-count field,
    so this reads the unit number (#4407 = 44th floor) and is deliberately strict: a garden-style community
    also numbers units "#1305" (building 13), so a floor of 13-19 needs the listing itself to describe a
    tall building, and only 20+ stands alone (deep dive 11/13). Never guesses mid- or low-rise."""
    s = subject or {}
    kind = f"{s.get('property_type') or ''} {s.get('home_type') or ''}".lower()
    if "condo" not in kind:
        return None
    m = _UNIT_FLOOR_RE.search(str(s.get("street_address") or "") + " " + str(s.get("unit_number") or ""))
    floor = int(m.group(1)[:-2]) if m else None
    if floor is None or floor < 13:
        return None
    remarks = str(s.get("public_remarks") or s.get("remarks") or s.get("listing_remarks") or "")
    if floor >= 20 or _TALL_BUILDING_WORDS_RE.search(remarks):
        return "high"
    return None


def fix_building_height_words(text: str, subject: Optional[dict], comp: Optional[dict] = None) -> tuple[str, list[str]]:
    """'mid-rise' -> 'high-rise' for the SUBJECT'S building only. In a comp's note it applies only when the
    comp is in that same building; in report text it skips any sentence that mentions a comparable (deep
    dive 11/13: a comp "in a mid-rise building two blocks away" was being renamed)."""
    cls = building_height_class(subject)
    if not text or not cls:
        return text, []
    if comp is not None and (not building_key(comp) or building_key(comp) != building_key(subject)):
        return text, []
    wrong: list[str] = []

    def swap(m):
        if m.group(1).lower() == cls:
            return m.group(0)
        wrong.append(m.group(0))
        word = cls.capitalize() if m.group(1)[0].isupper() else cls
        return f"{word}{m.group(2)}rise"
    parts = []
    for sent in re.split(r"(?<=[.!?])(\s+)", text):
        if comp is None and _OTHER_HOME_RE.search(sent):
            parts.append(sent)
        else:
            parts.append(_HEIGHT_WORD_RE.sub(swap, sent))
    if not wrong:
        return text, []
    return "".join(parts), [f"Building type made consistent: \"{wrong[0]}\" -> \"{cls}-rise\" (from the unit's floor)."]


# "one-bedroom-plus-den", "1 bedroom + den", "two bedrooms plus a home office" - joined by plus / + only
# ("three bedrooms and a study, plus a first-floor primary suite" on a 4-bedroom home is not this - deep dive 11/13)
_DEN_LAYOUT_RE = re.compile(r"\b(one|two|three|four|1|2|3|4)[- ]bed(?:room)?s?[- ,]*(?:plus|\+)[- ]*(?:a\s+|an\s+)?"
                            r"(den|home office|office)\b", re.IGNORECASE)
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4}


def bedroom_record_note(subject: Optional[dict]) -> Optional[str]:
    """The MLS says N bedrooms and the listing describes N-1 plus a den: one wording for the whole report."""
    s = subject or {}
    beds = _money(s.get("beds") or s.get("bedrooms") or s.get("bedrooms_total"))
    m = _DEN_LAYOUT_RE.search(str(s.get("public_remarks") or s.get("remarks") or s.get("listing_remarks") or ""))
    if not beds or not m:
        return None
    n = _WORD_NUM.get(m.group(1).lower()) or int(m.group(1))
    if int(beds) != n + 1:
        return None
    word = {1: "one", 2: "two", 3: "three", 4: "four"}[n]
    return (f"{int(beds)} bedrooms on the MLS record; the listing describes the layout as {word} bedroom{'s' if n > 1 else ''} plus a "
            f"{m.group(2).lower()}. Say exactly that wherever the bedroom count matters (\"listed as {int(beds)} bedrooms; the listing "
            f"describes {word} bedroom{'s' if n > 1 else ''} plus a {m.group(2).lower()}\") - never state either count alone as the fact, "
            f"and compare bedroom counts with comps on the MLS count ({int(beds)}).")


_BSMT_CMP = (r"(?:step down from|inferior to|worse than|weaker than|lesser than|falls short of|"
             r"a disadvantage (?:to|versus|against|compared (?:with|to)))")
_BSMT_SUBJ = r"\s+the subject(?:'s|’s)\s+(?:own\s+)?(?:full\s+)?basement"
# "Its unfinished basement is a step down from the subject's full basement[, but that is outweighed by ...]"
_BSMT_VERB_RE = re.compile(
    r"\b(its|the comp(?:arable)?(?:'s|’s)|this home(?:'s|’s))\s+unfinished basement\s+(?:is|was)\s+(?:a\s+)?" + _BSMT_CMP + _BSMT_SUBJ
    + r"(?:,\s*but\s+(?:that|this|it)\s+is\s+(?:more than\s+)?(?:outweighed|offset)\s+by\s+(?:[^.]|\.(?=\d))*)?", re.IGNORECASE)
# "... and an unfinished basement, a step down from the subject's full basement"
_BSMT_APPOS_RE = re.compile(r"(\bunfinished basement),\s+(?:which is\s+)?(?:a\s+)?" + _BSMT_CMP + _BSMT_SUBJ, re.IGNORECASE)


def fix_basement_unlike_comparison(text: str, subject: Optional[dict], comp: Optional[dict]) -> tuple[str, list[str]]:
    """'Its unfinished basement is a step down from the subject's full basement' compares finish with size.
    When the subject HAS a basement with no finished area on record, an unfinished comp basement is the same
    thing, not a shortfall. Only that clause is rewritten - the rest of the sentence stays (deep dive 11/13:
    the first version cut sentences at decimals and replaced unrelated facts). Not when the subject's lower
    level is finished, or when it has no basement at all."""
    s = subject or {}
    has_basement = bool(str(s.get("basement") or "").strip()) and lower_level_state(s) in ("unfinished", "unknown")
    if not text or comp is None or not has_basement:
        return text, []
    out = _BSMT_VERB_RE.sub(lambda m: f"{m.group(1)} basement is unfinished, and the subject's has no finished area on record either", text)
    out = _BSMT_APPOS_RE.sub(lambda m: f"{m.group(1)}, and the subject's basement has no finished area on record either", out)
    if out == text:
        return text, []
    return out, ["Comp note: an unfinished basement was called worse than the subject's \"full\" basement, but the subject's has no "
                 "finished area on record - rewritten as no difference."]


def clean_sewer(value):
    """An agent ticked both 'Public Sewer' and 'Public Septic' (50 S 16th St #5203, a 52nd-floor condo): there
    is no such thing as a public septic system, so that one entry is dropped. Any other pair ("Private Sewer"
    + "On Site Septic") is left as the record has it."""
    if isinstance(value, (list, tuple)):
        vals = [str(v) for v in value if v]
        if any(re.search(r"public\s+sewer", v, re.I) for v in vals):
            vals = [v for v in vals if not re.fullmatch(r"\s*public\s+septic\s*", v, re.I)]
        return vals if isinstance(value, list) else tuple(vals)
    return value


# New construction that closed above its list price (owner, 11/13): the list was the builder's base
# price, so the gap is most likely options and upgrades - not "strong demand" or a bidding war
# (370 Wickmere St: $867,620 against a $554,900 base read "a sold/list ratio of over 1.5", "strong demand").
_BUILDER_DEMAND_CLAUSE_RE = re.compile(
    r",?\s*(?:reflecting|showing|a sign of|evidence of|signall?ing|indicating|pointing to|driven by|thanks to)\s+"
    r"(?:very\s+|unusually\s+|exceptionally\s+)?(?:strong|intense|heavy|robust|high|fierce)\s+(?:buyer\s+)?"
    r"(?:demand|competition|interest)(?:[^,.;]|\.(?=\d))*", re.IGNORECASE)
_BUILDER_RATIO_ASIDE_RE = re.compile(r"\s*[-–—]\s*a sold[/-](?:to[- ])?list ratio of(?:[^-–—.]|\.(?=\d))*[-–—]\s*", re.IGNORECASE)
_BUILDER_CLAIM_RE = re.compile(
    r"bidding war|multiple offers|competing (?:offers|buyers|bids)|(?:over|above|against)\s+(?:its\s+|the\s+|a\s+)?(?:\$[\d,]+\s+)?"
    r"(?:asking|list)|sold[/-](?:to[- ])?list|% of (?:its\s+)?(?:asking|list)|strong demand", re.IGNORECASE)
# "reflects an unusually competitive new-construction bidding situation" -> what it most likely was
_BUILDER_BIDDING_PHRASE_RE = re.compile(
    r"\b(?:an?\s+)?(?:(?:unusually|very|highly|intensely|exceptionally)\s+)?(?:competitive\s+)?(?:new[- ]construction\s+)?"
    r"(?:bidding|multiple[- ]offer)\s+(?:situation|war|contest|environment|scenario)\b", re.IGNORECASE)
BUILDER_BASE_PRICE_SENTENCE = ("As new construction, its list price was most likely the builder's base price, so the higher "
                               "sale price most likely reflects options and upgrades the buyer chose rather than a bidding war.")


def fix_builder_base_price_claims(text: str, comp: Optional[dict]) -> tuple[str, list[str]]:
    """Runs on every pass and on saved notes, so it must leave its own sentence and an already-correct
    "not a bidding war" alone (deep dive 11/13: the second pass turned "rather than a bidding war" into
    "rather than upgrades over the builder's base price")."""
    if not text or not comp or not comp.get("builder_base_price"):
        return text, []
    had_sentence = BUILDER_BASE_PRICE_SENTENCE in text
    body = text.replace(BUILDER_BASE_PRICE_SENTENCE, "").rstrip() if had_sentence else text
    t = _BUILDER_RATIO_ASIDE_RE.sub(" ", _BUILDER_DEMAND_CLAUSE_RE.sub("", body))

    def _bid(m):
        if re.search(r"\b(?:not|no|never|rather than|than|isn(?:'|’)t|wasn(?:'|’)t)\s+$", t[:m.start()], re.IGNORECASE):
            return m.group(0)            # "this was not a bidding war" is already the right statement
        return "upgrades over the builder's base price"
    t = _BUILDER_BIDDING_PHRASE_RE.sub(_bid, t)
    flags = []
    if t != body:
        flags.append("Comp note: removed 'strong demand' / sold-to-list wording about a new-construction sale "
                     "(its list price was the builder's base price).")
    if had_sentence:
        t = t.rstrip() + " " + BUILDER_BASE_PRICE_SENTENCE
    elif "base price" not in t.lower() and _BUILDER_CLAIM_RE.search(t):
        t = t.rstrip() + " " + BUILDER_BASE_PRICE_SENTENCE
        flags.append("Comp note: added that a new-construction sale above its list price most likely reflects "
                     "upgrades over the builder's base price, not a bidding war.")
    return (t, flags) if flags else (text, [])


# "closing at 0.986 of list", "sold in 4 days at 1.204 of list" (701 Sturbridge Dr, test round 11/13) - the model
# copying the sale-to-list ratio out of its data. A client reads percentages, not decimals.
_RATIO_OF_LIST_RE = re.compile(
    r"\b(?P<r>[01]\.\d{2,4})(?P<x>x)?\s+(?:of|times)\s+(?P<rest>(?:its\s+|the\s+|their\s+)?(?:final\s+|original\s+|last\s+)?"
    r"(?:list|asking|listing)(?:\s+price)?)\b", re.IGNORECASE)


def fix_ratio_of_list(text: str) -> tuple[str, list[str]]:
    """'0.986 of list' -> '98.6% of list'. The number is the same; only how it is written changes."""
    if not text:
        return text, []
    flags: list[str] = []

    def _sub(m: "re.Match") -> str:
        r = float(m.group("r"))
        if not 0.4 <= r <= 1.9:
            return m.group(0)
        pct = f"{r * 100:.1f}".rstrip("0").rstrip(".")
        flags.append(f"Fact check: '{m.group(0)}' written as a percentage ({pct}%).")
        return f"{pct}% of {m.group('rest')}"

    return _RATIO_OF_LIST_RE.sub(_sub, text), flags


# "a 0.14 acres lot", "same 0.14 acres lot" (735 Concord Ave, test rounds 11/13-11/14) -> "a 0.14-acre lot"
_ACRES_LOT_RE = re.compile(r"\b(\d+(?:\.\d+)?)[ -]acres\s+(lot|parcel|site|property)\b", re.IGNORECASE)


def fix_acres_lot_grammar(text: str) -> tuple[str, list[str]]:
    if not text:
        return text, []
    out = _ACRES_LOT_RE.sub(lambda m: f"{m.group(1)}-acre {m.group(2)}", text)
    # "a 8.50-acre" / "a 0.14-acre" read aloud: "an eight...", "a zero..." - only 8, 11 and 18 take "an"
    out = re.sub(r"\ba (?=(?:8|11|18)(?:\.\d+)?-acre\b)", "an ", out)
    return out, ([] if out == text else ["Fact check: 'N acres lot' written as 'N-acre lot'."])


# "It is with a Villanova mailing address" - left in notes saved before the mailing-town rewrite learned
# "is in <Town>" (701 Sturbridge Dr, test round 11/13). The home HAS the address.
_IS_WITH_MAILING_RE = re.compile(r"\b(?:is|sits|lies)(?:\s+located)?\s+with\s+an?\s+((?:[A-Z][\w.'’-]*\s+){1,4})mailing address\b")


def fix_mailing_address_grammar(text: str) -> tuple[str, list[str]]:
    if not text:
        return text, []
    out = _IS_WITH_MAILING_RE.sub(lambda m: f"has a {m.group(1)}mailing address", text)
    return out, ([] if out == text else ["Fact check: 'is with a ... mailing address' reworded."])


def fix_text(text: str, *, subject: Optional[dict], comp: Optional[dict] = None, who: str = "Text",
             comps: Optional[list] = None) -> tuple[str, list[str]]:
    """Every deterministic rewrite above, in order. `comp` = the home a comp write-up is about; `comps` =
    the report's comps (report-level text only - the lot-size word check)."""
    flags: list[str] = []
    t = text or ""
    for fn in (
        lambda x: fix_manual_subject_record(x, subject, comp),
        lambda x: fix_subject_lot_words(x, subject, comps) if comp is None and comps else (x, []),
        lambda x: fix_above_grade_psf_wording(x),
        lambda x: fix_subject_psf_numbers(x, subject),
        lambda x: fix_psf_direction(x, subject),
        lambda x: fix_subject_busy_road_claim(x, subject),
        lambda x: fix_comp_lot_acres(x, subject, comp),
        lambda x: fix_building_height_words(x, subject, comp),
        lambda x: fix_basement_unlike_comparison(x, subject, comp),
        lambda x: fix_builder_base_price_claims(x, comp),
        lambda x: fix_community_claims(x, subject, comp),
        lambda x: fix_lower_level_claims(x, comp, who) if comp is not None else (x, []),
        lambda x: (lambda t2, f2: (t2, [f"{who}: {m}" for m in f2]))(*fix_size_claims(x, subject, comp)) if comp is not None else (x, []),
        lambda x: fix_size_psf_logic(x, subject, comp) if comp is not None else (x, []),
        lambda x: fix_ratio_of_list(x),
        lambda x: fix_acres_lot_grammar(x),
        lambda x: fix_mailing_address_grammar(x),
        lambda x: fix_field_name_leaks(x),
        lambda x: drop_repeated_sentences(x),
    ):
        t, f = fn(t)
        flags += f
    return t, flags


def sentence_problems(sentence: str, *, subject: Optional[dict], comp: Optional[dict] = None) -> list[str]:
    """Claims in one sentence that can't be rewritten safely - the caller drops the sentence."""
    out = []
    p = subject_psf_claim_problem(sentence, subject)
    if p:
        out.append(p)
    if comp is not None:
        p = lower_level_claim_problem(sentence, comp)
        if p:
            out.append(p)
        p = size_claim_problem(sentence, subject, comp)
        if p:
            out.append(p)
    return out


def text_problems(text: str, *, subject: Optional[dict], comp: Optional[dict] = None,
                  label: str = "Text", sentence_fn: Optional[Callable[[str], list[str]]] = None) -> list[str]:
    out = []
    for s in sentences(text or ""):
        for p in (sentence_fn or (lambda x: sentence_problems(x, subject=subject, comp=comp)))(s):
            out.append(f"{label}: fact check - {p} (\"{s[:80]}\")")
    return out


# ----------------------------------------------------------------------------- HOA / condo fees

_FREQ_PER_MONTH = {"monthly": 1.0, "month": 1.0, "annually": 1 / 12, "annual": 1 / 12, "yearly": 1 / 12,
                   "quarterly": 1 / 3, "semi-annually": 1 / 6, "semiannually": 1 / 6, "semi annually": 1 / 6,
                   "bi-monthly": 0.5, "bimonthly": 0.5, "weekly": 52 / 12}


def _money(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def monthly_fees(row: Optional[dict]) -> list[dict]:
    """Every recurring association fee on the record, per month. Bright files a condo's fee in
    AssociationFee2 (191 Presidential Blvd: $2,405/month) and an HOA's in AssociationFee - the report used
    to read only the first, so a $2,400/month condo fee never appeared. Unknown frequency = not shown."""
    r = row or {}
    out = []
    for amt_k, freq_k, label in (("association_fee", "association_fee_frequency", "HOA fee"),
                                 ("condo_fee", "condo_fee_frequency", "Condo fee")):
        amt = _money(r.get(amt_k))
        if not amt:
            continue
        freq = str(r.get(freq_k) or "").strip().lower()
        per = _FREQ_PER_MONTH.get(freq)
        if per is None:
            continue
        if label == "HOA fee" and str(r.get("home_type") or r.get("property_type") or "").upper().startswith("CONDO") \
                and not _money(r.get("condo_fee")):
            label = "Condo fee"
        out.append({"label": label, "amount": round(amt), "frequency": freq, "monthly": round(amt * per)})
    return out


def fee_includes(row: Optional[dict]) -> list[str]:
    inc = (row or {}).get("association_fee_includes") or []
    if isinstance(inc, str):
        inc = [x.strip() for x in inc.split(",")]
    return [str(x) for x in inc if x and str(x).strip()]


def fee_line(row: Optional[dict]) -> Optional[str]:
    fees = monthly_fees(row)
    if not fees:
        return None
    bits = [f"{f['label']} ${f['monthly']:,.0f}/month" + (f" (${f['amount']:,.0f} {f['frequency']})" if f["frequency"] != "monthly" else "")
            for f in fees]
    inc = fee_includes(row)
    return "; ".join(bits) + (f"; includes {', '.join(inc[:14])}" if inc else "")


# ----------------------------------------------------------------------------- listing / sale history

_START_EVENTS = ("Listed for sale", "Coming soon")
_END_EVENTS = ("Sold", "Taken off market")
_RELIST_GAP_DAYS = 90     # off the market this briefly and back = one marketing effort, not a fresh start


def _d(s):
    from datetime import date
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def summarize_history(events: list, *, current_list_date=None, today=None) -> dict:
    """The home's earlier listings and sales from price_history_service.get_history (newest first).

    4913 Chancellor St: bought Mar 13 2026 for $200,000, listed Aug 13 2026 (price changed Oct 6), taken off
    the market and relisted Oct 9 at $450,000 - the report said '15 days on market' as if it were new.
    191 Presidential Blvd #R-605: sold Nov 13 2025 for $280,000, relisted Oct 2026 at $399,999."""
    from datetime import date
    today = today or date.today()
    cur = _d(current_list_date)
    # same day: the old listing ends before the new one starts, then that day's price change
    evs = sorted((e for e in (events or []) if _d(e.get("date"))),
                 key=lambda e: (_d(e["date"]), 0 if e.get("event") in _END_EVENTS else 1 if e.get("event") in _START_EVENTS else 2))
    periods: list[dict] = []
    sales: list[dict] = []
    open_p = None
    for e in evs:
        ev, day, price = e.get("event"), _d(e["date"]), _money(e.get("price"))
        if ev in _START_EVENTS:
            if open_p and open_p.get("start") == day:
                continue
            if open_p:
                open_p["end"], open_p["end_event"] = day, "relisted"
                periods.append(open_p)
            open_p = {"start": day, "list_price": price, "last_price": price, "changes": []}
        # Under contract: the home is no longer being marketed, so the count stops there (and resumes if it comes
        # back). Listed Aug 13, relisted Oct 9, under contract Oct 20, report Nov 13 read "35 days on this
        # listing, about 89 days in total" - true active time 11 and 65 (deep dive 11/13).
        elif ev == "Under contract" and open_p and not open_p.get("contract"):
            open_p["contract"] = day
        elif ev == "Back on market" and open_p and open_p.get("contract"):
            open_p["paused_days"] = open_p.get("paused_days", 0) + max(0, (day - open_p.pop("contract")).days)
        elif ev in ("Price reduced", "Price increased", "Price changed") and open_p:
            open_p["changes"].append({"date": day, "price": price, "change": e.get("change")})
            if price:
                open_p["last_price"] = price
        elif ev in _END_EVENTS:
            if ev == "Sold":
                sales.append({"date": day, "price": price})
            if open_p:
                open_p["end"], open_p["end_event"] = day, ("sold" if ev == "Sold" else "off_market")
                periods.append(open_p)
                open_p = None
    if open_p:
        periods.append(open_p)
    for p in periods:
        end = p.get("end") or today
        if p.get("contract") and p["contract"] < end:
            end = p["contract"]
        p["days"] = max(0, (end - p["start"]).days - p.get("paused_days", 0)) if p.get("start") else None
    current = None
    if cur:
        current = next((p for p in periods if p.get("start") and abs((p["start"] - cur).days) <= 3), None)
    if current is None and periods and not periods[-1].get("end"):
        current = periods[-1]
    earlier = [p for p in periods if p is not current and p.get("start") and (not cur or p["start"] < cur)]
    prior_sales = [s for s in sales if not cur or (s["date"] and s["date"] < cur)]
    # one marketing effort: chain back through listings that ended (off market, not sold) shortly before the next
    chain_days, chain = 0, []
    nxt_start = (current or {}).get("start") or cur
    for p in sorted(earlier, key=lambda p: p["start"], reverse=True):
        if p.get("end_event") == "sold" or not p.get("end") or not nxt_start:
            break
        if (nxt_start - p["end"]).days > _RELIST_GAP_DAYS:
            break
        chain.append(p)
        chain_days += p.get("days") or 0
        nxt_start = p["start"]
    cur_days = (current or {}).get("days")
    lines: list[str] = []
    for s in prior_sales[-2:]:
        lines.append(f"Sold {s['date'].isoformat()}" + (f" for ${s['price']:,.0f}" if s.get("price") else " (price not in the MLS record)"))
    for p in sorted(chain, key=lambda p: p["start"]):
        bit = f"Earlier listing {p['start'].isoformat()}" + (f" at ${p['list_price']:,.0f}" if p.get("list_price") else "")
        if p["changes"]:
            ch = p["changes"][-1]
            bit += (f", price changed {ch['date'].isoformat()}" + (f" to ${ch['price']:,.0f}" if ch.get("price") else "")
                    + ("" if ch.get("change") else " (the earlier price is not in our MLS feed)"))
        bit += f", off the market {p['end'].isoformat()} after {p['days']} days"
        lines.append(bit)
    if chain and current:
        lines.append(f"Relisted {current['start'].isoformat()}" + (f" at ${current['list_price']:,.0f}" if current.get("list_price") else "")
                     + f": {cur_days} days on this listing, about {chain_days + (cur_days or 0)} days on the market in total "
                       "since the earlier listing began")
    last_sale = prior_sales[-1] if prior_sales else None
    return {
        "prior_sales": [{"date": s["date"].isoformat(), "price": s.get("price")} for s in prior_sales],
        "last_sale": {"date": last_sale["date"].isoformat(), "price": last_sale.get("price")} if last_sale else None,
        "earlier_listings": [{"listed": p["start"].isoformat(), "list_price": p.get("list_price"), "last_price": p.get("last_price"),
                              "ended": p["end"].isoformat() if p.get("end") else None, "end_event": p.get("end_event"),
                              "days": p.get("days")} for p in sorted(chain, key=lambda p: p["start"])],
        "relisted": bool(chain),
        "current_listed": current["start"].isoformat() if current and current.get("start") else None,
        "current_listing_days": cur_days,
        "marketing_days_total": (chain_days + (cur_days or 0)) if chain else None,
        "lines": lines,
    }


_HISTORY_MARK = "days in total"


def _nice_date(s) -> str:
    d = _d(s)
    return f"{d.strftime('%b')} {d.day}, {d.year}" if d else str(s)


def history_sentence(methodology: str, summary: Optional[dict], *, today=None, listed: bool = True) -> str:
    """One plain sentence on the home's own record when it matters: relisted (one marketing effort, not a
    fresh listing) and/or sold in the last 3 years. Grader 10/24, 4913 Chancellor St: "mentions longer
    cumulative marketing exposure but doesn't clearly show the earlier listing"."""
    from datetime import date
    s = summary or {}
    if not methodology or _HISTORY_MARK in methodology:
        return methodology
    bits = []
    first = (s.get("earlier_listings") or [{}])[0]
    # only for a home that is on the market now - an expired 2024 relisting read "has been on the market about
    # 206 days in total" in a 2026 report (deep dive 11/13)
    if listed and s.get("relisted") and s.get("marketing_days_total") and first.get("listed"):
        ended = first.get("ended")
        rel = s.get("current_listed")
        bits.append(f"This home has been on the market about {s['marketing_days_total']} {_HISTORY_MARK}: first listed "
                    f"{_nice_date(first['listed'])}" + (f", taken off the market {_nice_date(ended)}" if ended else "")
                    + (f" and relisted {_nice_date(rel)}" if rel else " and relisted")
                    + " - one marketing effort, not a new listing.")
    ls = s.get("last_sale") or {}
    sold = _d(ls.get("date"))
    if sold and ls.get("price") and ((today or date.today()) - sold).days <= 3 * 365:
        bits.append(f"It last sold on {_nice_date(sold.isoformat())} for ${float(ls['price']):,.0f}; what has changed "
                    "since then should be confirmed with the seller.")
    return (methodology.rstrip() + " " + " ".join(bits)) if bits else methodology


# ----------------------------------------------------------------------------- high-impact features

def _blob(row: Optional[dict], keys) -> str:
    parts = []
    for k in keys:
        v = (row or {}).get(k)
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        if v:
            parts.append(str(v))
    return " | ".join(parts).lower()


_FEATURE_KEYS = ("pool", "pool_features", "public_remarks", "other_structures", "interior_features",
                 "exterior_features", "lot_features", "accessibility_features")
# (key, words for prose, how to find it, how to tell the notes already talk about it)
_HIGH_IMPACT = (
    ("indoor_pool", "indoor pool", r"\bindoor\s+(?:lap\s+|swimming\s+|heated\s+)?pool|\bpool\s*(?:house|room)\b.{0,30}\bindoor|\bnatatorium",
     r"indoor\s+(?:lap\s+)?pool|natatorium"),
    ("pool", "pool", r"\b(?:in-?ground|inground|saltwater|heated|gunite)?\s*pool\b(?!\s*table)", r"\bpool\b"),
    ("guest_house", "guest house / in-law suite", r"\b(?:guest\s+(?:house|cottage)|carriage\s+house|in-?law\s+(?:suite|quarters)|accessory\s+dwelling)\b",
     r"guest|carriage\s+house|in-?law|accessory\s+(?:dwelling|space)"),
    ("solar", "solar panels", r"\bsolar\b", r"\bsolar\b|\benergy\b"),
    ("elevator", "elevator", r"\belevator\b", r"\belevator\b"),
    ("sport_court", "sport / tennis court", r"\b(?:tennis|sport|basketball|pickleball)\s+court\b", r"\bcourt\b"),
    ("barn", "barn / outbuilding", r"\b(?:barn|stable|equestrian)\b", r"\b(?:barn|stable|equestrian)\b"),
)
_NO_POOL_RE = re.compile(r"\bno\s+pool\b|\bpool:\s*no\b", re.I)
# "community pool", "clubhouse, pool and tennis courts", "pool and courts for residents" - shared
# amenities, not the home's own (523 Sherbrook Dr's community pool read as a private pool, 10/24)
_SHARED_AMENITY_RE = re.compile(
    r"\b(?:community|neighbou?rhood|hoa|association|clubhouse|club\s*house|amenit(?:y|ies)|shared|residents?'?)\b"
    r"[^.;]{0,60}?\b(?:pools?|courts?)\b"
    r"|\b(?:pools?|courts?)\b[^.;]{0,30}\b(?:community|clubhouse|hoa|association|residents?)\b", re.I)


def high_impact_features(row: Optional[dict], *, condo: bool = False) -> list[tuple[str, str]]:
    """Features buyers pay (or pay to maintain) a lot for. A condo's shared building pool is not the unit's."""
    blob = _blob(row, _FEATURE_KEYS)
    out = []
    for key, words, find, _talk in _HIGH_IMPACT:
        if key in ("pool", "indoor_pool"):
            pool_field = _blob(row, ("pool",))
            if condo or "community" in pool_field or (_NO_POOL_RE.search(pool_field) and not re.search(find, _blob(row, ("public_remarks",)))):
                continue
            if key == "pool" and any(k == "indoor_pool" for k, _ in out):
                continue
        if key in ("barn", "elevator", "sport_court") and condo:
            continue
        if re.search(find, _SHARED_AMENITY_RE.sub(" ", blob) if key in ("pool", "sport_court") else blob, re.I):
            out.append((key, words))
    return out


def feature_coverage_sentence(methodology: str, subject: Optional[dict], comps: list, *, condo: bool = False) -> str:
    """'' when the notes already discuss every high-impact feature; else one sentence per missing one
    (8084 Goshen Rd, grader 10/23: the listing highlights an indoor lap pool and the valuation never said a word)."""
    text = (methodology or "").lower()
    parts = []
    unmatched: list[str] = []   # features no comp has: one sentence for all of them (two near-identical ones read as a repeat)
    for key, words in high_impact_features(subject, condo=condo):
        talk = next(t for k, _w, _f, t in _HIGH_IMPACT if k == key)
        if re.search(talk, text, re.I):
            continue
        same_find = next(f for k, _w, f, _t in _HIGH_IMPACT if k == key)
        with_it = [c for c in comps or [] if c.get("is_included", True)
                   and re.search(same_find, _blob(c, _FEATURE_KEYS), re.I)
                   and not (key in ("pool", "indoor_pool") and _NO_POOL_RE.search(_blob(c, ("pool",))))]
        names = " and ".join(str(c.get("street_address") or "").title() for c in with_it[:2])
        verb = "are" if words.endswith("s") else "is"
        if key == "indoor_pool" and not with_it:
            pool_find = next(f for k, _w, f, _t in _HIGH_IMPACT if k == "pool")
            outdoor = [c for c in comps or [] if c.get("is_included", True) and re.search(pool_find, _blob(c, _FEATURE_KEYS), re.I)
                       and not _NO_POOL_RE.search(_blob(c, ("pool",)))]
            if outdoor:
                on = " and ".join(str(c.get("street_address") or "").title() for c in outdoor[:2])
                parts.append(f"The indoor pool is not priced as a separate adjustment: none of the comparable sales has one, and "
                             f"the outdoor {'pools' if len(outdoor) > 1 else 'pool'} at {on} {'are' if len(outdoor) > 1 else 'is'} a different feature to buyers (year-round use, but higher upkeep, "
                             "heating and humidity-control costs) - so its value is uncertain and part of why the range is as wide as it is.")
                continue
        if with_it:
            parts.append(f"The {words} {verb} not priced as a separate adjustment: {len(with_it)} of the comparable sales "
                         f"also {'has' if len(with_it) == 1 else 'have'} one ({names}), so it is reflected in how those sales "
                         "compare rather than as its own dollar figure - its appeal and its upkeep cost both belong in a "
                         "buyer's own judgment.")
        else:
            unmatched.append(words)
    if unmatched:
        many = len(unmatched) > 1 or unmatched[0].endswith("s")
        what = unmatched[0] if len(unmatched) == 1 else ", ".join(unmatched[:-1]) + " and " + unmatched[-1]
        parts.append(f"The {what} {'are' if many else 'is'} not priced as {'separate adjustments' if len(unmatched) > 1 else 'a separate adjustment'}"
                     f" - none of the comparable sales has {'them' if len(unmatched) > 1 else 'one'}, so "
                     f"{'their' if many else 'its'} value to buyers (and upkeep cost) is uncertain and part of why the range is as wide as it is.")
    return " ".join(parts)


# ----------------------------------------------------------------------------- facts handed to the model

_LL_WORDS = {"measured": "finished below-grade area on record",
             "finished_unknown_size": "finished lower level, size not reported - never call it unfinished or absent",
             "unfinished": "unfinished basement", "none": "no basement on record", "unknown": None}


def lot_acres_text(d: Optional[dict]) -> Optional[str]:
    """The lot in acres, two decimals - the one figure the report prints everywhere (cover, comp pages,
    write-ups). The model rounded 1.15 acres to "1.1" in the text while the cover said 1.2 (grader 11/01)."""
    d = d or {}
    sqft = _money(d.get("lot_size")) or _money(d.get("lot_size_sqft"))
    acres = sqft / 43560 if sqft else _money(d.get("lot_size_acres")) or _money(d.get("acres"))
    if not acres or implausible_lot(acres, _money(d.get("sale_price")) or _money(d.get("list_price")), d.get("home_type")):
        return None
    return f"{acres:,.2f} acres - quote exactly this figure for its lot size"


def prompt_facts_subject(subject: Optional[dict]) -> dict:
    s = subject or {}
    psf = subject_list_psf(s)
    return {
        "lot_acres": lot_acres_text(s),
        "subdivision_name": subdivision_of(s) or "not on record",
        "list_price_per_above_grade_sqft": (f"${psf:,.0f} per above-grade sq ft - the ONLY subject $/sq ft to quote"
                                            if psf else None),
        "lower_level_record": _LL_WORDS[lower_level_state(s)],
        "association_fees": fee_line(s),
        "bedroom_record": bedroom_record_note(s),
        "building_type": ({"high": "high-rise building", "mid": "mid-rise building", "low": "low-rise building"}[building_height_class(s)]
                          + " - use exactly this term" if building_height_class(s) else None),
    }


def prompt_facts_comp(subject: Optional[dict], comp: Optional[dict]) -> dict:
    c = comp or {}
    ag = above_grade_sqft(c)
    price = _money(c.get("sale_price")) or _money(c.get("close_price"))
    psf = (price / ag) if price and ag else None
    s_psf = subject_list_psf(subject)
    vs = None
    if psf and s_psf:
        rel = "above" if psf > s_psf * 1.01 else "below" if psf < s_psf * 0.99 else "about the same as"
        vs = f"${psf:,.0f} vs the subject's ${s_psf:,.0f} list price per above-grade sq ft - {rel}"
    return {
        "lot_acres": lot_acres_text(c),
        "subdivision_name": subdivision_of(c) or "not on record",
        "neighborhood_vs_subject": neighborhood_vs_subject(subject, c),
        "price_per_above_grade_sqft_vs_subject": vs,
        "lower_level_record": _LL_WORDS[lower_level_state(c)],
        "association_fees": fee_line(c),
    }
