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


def _subdivision_key(row: Optional[dict]) -> Optional[str]:
    """None when the MLS 'subdivision' is really the township ('UWCHLAN TWP' on 281 Woodland Dr)."""
    r = row or {}
    k = _place_key(r.get("subdivision_name") or r.get("neighborhood"))
    if not k:
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


def same_subdivision(a: Optional[dict], b: Optional[dict]) -> Optional[bool]:
    """True/False when both homes have a subdivision on record (or share a building), None otherwise."""
    ba, bb = building_key(a), building_key(b)
    if ba and ba == bb and re.search(r"#|\bunit\b|\bapt\b", f"{(a or {}).get('street_address')} {(b or {}).get('street_address')}", re.I):
        return True
    ka, kb = _subdivision_key(a), _subdivision_key(b)
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
    r"\bsubject(?:'s|’s)\s+(?:own\s+)?(?:(?:list|asking)(?:[- ]price)?\s+)?(?:\$/sq\.?\s?ft\s+(?:of\s+)?)?"
    r"\$\s?(?P<n>\d{2,4}(?:\.\d+)?)\s*(?:/\s*sq\.?\s?(?:ft|foot)|per\s+(?:square\s+)?(?:foot|ft))", re.I)


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
    "attic insulation gutters radon mold termite lot comp subject buyer seller sale sales pending listing").split())


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
    t = _MANUAL_RECORD_RE.sub(lambda m: ("The" if m.group(1)[0].isupper() else "the") + " details provided for the home", t)
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
        if m and not _OTHER_HOME_REF_RE.search(s[:m.start()]):
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
        lambda x: fix_community_claims(x, subject, comp),
        lambda x: fix_lower_level_claims(x, comp, who) if comp is not None else (x, []),
        lambda x: (lambda t2, f2: (t2, [f"{who}: {m}" for m in f2]))(*fix_size_claims(x, subject, comp)) if comp is not None else (x, []),
        lambda x: fix_size_psf_logic(x, subject, comp) if comp is not None else (x, []),
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
        p["days"] = max(0, (end - p["start"]).days) if p.get("start") else None
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


def history_sentence(methodology: str, summary: Optional[dict], *, today=None) -> str:
    """One plain sentence on the home's own record when it matters: relisted (one marketing effort, not a
    fresh listing) and/or sold in the last 3 years. Grader 10/24, 4913 Chancellor St: "mentions longer
    cumulative marketing exposure but doesn't clearly show the earlier listing"."""
    from datetime import date
    s = summary or {}
    if not methodology or _HISTORY_MARK in methodology:
        return methodology
    bits = []
    first = (s.get("earlier_listings") or [{}])[0]
    if s.get("relisted") and s.get("marketing_days_total") and first.get("listed"):
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


def prompt_facts_subject(subject: Optional[dict]) -> dict:
    s = subject or {}
    psf = subject_list_psf(s)
    return {
        "subdivision_name": subdivision_of(s) or "not on record",
        "list_price_per_above_grade_sqft": (f"${psf:,.0f} per above-grade sq ft - the ONLY subject $/sq ft to quote"
                                            if psf else None),
        "lower_level_record": _LL_WORDS[lower_level_state(s)],
        "association_fees": fee_line(s),
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
        "subdivision_name": subdivision_of(c) or "not on record",
        "neighborhood_vs_subject": neighborhood_vs_subject(subject, c),
        "price_per_above_grade_sqft_vs_subject": vs,
        "lower_level_record": _LL_WORDS[lower_level_state(c)],
        "association_fees": fee_line(c),
    }
