"""CMA "time machine" - run the normal CMA generation as if it were an earlier date.

Used ONLY by the admin CMA accuracy test (cma_backtest_service). Never reachable from an agent
request: it is switched on by a context variable that only the backtest runner sets, around one
generation at a time. With it on:

  * "today" for every date-relative calculation in the CMA engine is the as-of date (comp recency,
    subject days on market, ages, the proximity-density window);
  * the subject's own MLS record is shown as it stood that day - Active, its list price then,
    its days on market then - with every closing / contract / later-status field removed and any
    "under contract / sold / open house cancelled" sentence stripped from the remarks;
  * the live Bright status re-pull is skipped (it would reveal the sale);
  * comps must have closed on or before the as-of date (the comp search is asked for that window,
    and the engine asserts it again before anything reaches the AI);
  * every AI input (comp-analysis prompt, the retry / guidance prompts and the final-valuation
    evidence packet) is scanned for the subject's real sale price, closing date and contract date.
    A hit is logged and aborts the run (TimeMachineLeak) - a leaky test result is worse than none.
"""
from __future__ import annotations

import contextvars
import copy
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from app.config.logging import get_logger

logger = get_logger(__name__)


class TimeMachineLeak(RuntimeError):
    """A post-as-of fact about the subject reached an AI input."""


@dataclass
class TimeMachine:
    listing_key: str
    as_of: date
    list_price: Optional[float]
    dom: Optional[int]
    # The hidden truth - used ONLY to scan AI inputs for leaks, never shown to the engine.
    close_price: Optional[float] = None
    close_date: Optional[date] = None
    contract_date: Optional[date] = None
    leak_log: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    # DRY RUN (zero cost): stand-ins for the two AI providers. Every request they receive is kept
    # in `captured` so the leak scan covers EVERY model-bound input, including the follow-up passes.
    fake_client: Any = None
    fake_reasoning: Any = None
    captured: list = field(default_factory=list)
    # The subject part of the main comp-analysis prompt, kept for the human review of each test report.
    prompt_excerpt: Optional[str] = None


_CURRENT: contextvars.ContextVar[Optional[TimeMachine]] = contextvars.ContextVar("cma_time_machine", default=None)


def current() -> Optional[TimeMachine]:
    return _CURRENT.get()


def activate(tm: TimeMachine):
    return _CURRENT.set(tm)


def deactivate(token) -> None:
    _CURRENT.reset(token)


def today() -> date:
    """'Today' for the CMA engine - the as-of date while a time-machine run is active."""
    tm = _CURRENT.get()
    return tm.as_of if tm else datetime.now(timezone.utc).date()


def this_year() -> int:
    return today().year


# ---- masking the subject's own record ------------------------------------------------------------

# Raw RESO keys that only exist (or only change) once a listing goes under contract / closes, or that
# describe later status changes. Matched as substrings, case-insensitive.
_POST_DATE_KEY_PARTS = (
    "close", "buyer", "contract", "pending", "offmarket", "statuschange", "majorchange",
    "modification", "previousstandardstatus", "concession", "sold", "withdrawn", "cancel",
    "expiration", "financing", "settlement",
)
_KEEP_KEYS = {"ListingContractDate"}   # the listing agreement date - known on day one

_REMARK_LEAK_RE = re.compile(
    # Only what an agent writes once THIS deal happened. Not "being sold as-is" (a pre-sale term of
    # sale) and not "back on market" (a fallen-through EARLIER deal - known on the as-of date).
    r"[^.!?\n]*\b(?:under\s+(?:contract|agreement|deposit)|pending|(?:has|was|have\s+been|been|just)\s+sold|"
    r"sold\s*!|settled|closed\s+(?:on|at|for)|"
    r"accepted\s+(?:an\s+)?offer|multiple\s+offers|highest\s+and\s+best|offer\s+deadline|"
    r"kick[- ]?out|cancel+ed|no\s+more\s+showings|"
    r"showings?\s+(?:have\s+)?(?:stopped|paused|suspended))(?:\b|(?<=!))[^.!?\n]*[.!?]?",
    re.IGNORECASE,
)


def scrub_remarks(text: Optional[str]) -> tuple[Optional[str], list[str]]:
    """Drop sentences an agent adds AFTER the deal happens ("OPEN HOUSE ... CANCELLED", "UNDER
    CONTRACT", "multiple offers received")."""
    if not text:
        return text, []
    removed = [m.group(0).strip() for m in _REMARK_LEAK_RE.finditer(text) if m.group(0).strip()]
    cleaned = _REMARK_LEAK_RE.sub(" ", text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"^[\s!.,;:|\-]+", "", cleaned)   # leftovers like the 2nd '!' of '...CANCELLED!!'
    return cleaned, removed


def masked_raw(raw: Optional[dict], tm: TimeMachine) -> dict:
    out = {}
    for k, v in (raw or {}).items():
        lk = k.lower()
        if k not in _KEEP_KEYS and any(part in lk for part in _POST_DATE_KEY_PARTS):
            continue
        out[k] = copy.deepcopy(v)
    out["MlsStatus"] = "ACTIVE-BRIGHT"
    out["StandardStatus"] = "Active"
    if tm.list_price:
        out["ListPrice"] = tm.list_price
    if tm.dom is not None:
        out["DaysOnMarket"] = tm.dom
        if "CumulativeDaysOnMarket" in (raw or {}):
            out["CumulativeDaysOnMarket"] = tm.dom
    living = out.get("LivingArea")
    if tm.list_price and living:
        try:
            out["PricePerSquareFoot"] = round(float(tm.list_price) / float(living), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            out.pop("PricePerSquareFoot", None)
    if out.get("PublicRemarks"):
        out["PublicRemarks"], _ = scrub_remarks(out["PublicRemarks"])
    return out


class _MaskedProperty:
    """Read-only stand-in for the subject's Property row as it stood on the as-of date."""

    def __init__(self, prop: Any, tm: TimeMachine):
        self._prop = prop
        self.raw_mls_data = masked_raw(getattr(prop, "raw_mls_data", None), tm)
        self.home_status = "ACTIVE-BRIGHT"
        self.price = tm.list_price or getattr(prop, "price", None)
        self.days_on_market = tm.dom
        self.cumulative_days_on_market = tm.dom
        self.description, _ = scrub_remarks(getattr(prop, "description", None))

    def __getattr__(self, name):
        return getattr(self._prop, name)


def masked_property(prop: Any, tm: TimeMachine) -> Any:
    return _MaskedProperty(prop, tm) if prop is not None else None


def apply_to_subject(subject: dict, tm: TimeMachine) -> tuple[dict, list[str]]:
    """The wizard-built subject snapshot, set back to the as-of date."""
    s = dict(subject or {})
    s["mls_status"] = "Active"
    if tm.list_price:
        s["list_price"] = tm.list_price
    s["days_on_market"] = tm.dom
    s["last_sale_date"] = None
    s["last_sale_price"] = None
    s["public_remarks"], removed = scrub_remarks(s.get("public_remarks"))
    return s, removed


# ---- leak checks ---------------------------------------------------------------------------------

def _money_patterns(v: float) -> list[str]:
    n = int(round(v))
    pats = [f"{n:,}", str(n)]
    if n % 1000 == 0:
        pats += [f"{n // 1000}k", f"{n // 1000}K"]
    if n % 100000 == 0 and n >= 1_000_000:
        m = n / 1_000_000
        pats += [f"{m:g}M", f"{m:g} million"]
    return pats


def _date_patterns(d: date) -> list[str]:
    return [d.isoformat(), d.strftime("%m/%d/%Y"), f"{d.month}/{d.day}/{d.year}",
            d.strftime("%B %d, %Y").replace(" 0", " "), d.strftime("%b %d, %Y").replace(" 0", " ")]


def _strip_binary(obj: Any) -> Any:
    """Drop image blocks / base64 blobs - only words and numbers can leak a sale (and base64 text
    produced a false 'sale price 2M' hit)."""
    if isinstance(obj, dict):
        if obj.get("type") == "image" or "base64" in str(obj.get("type") or ""):
            return None
        return {k: _strip_binary(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_binary(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 2000 and " " not in obj[:2000]:
        return None
    return obj


def _flatten_text(payload: Any) -> str:
    payload = _strip_binary(payload)
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts = []
        for block in payload:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, dict) and block.get("type") == "image":
                continue
            else:
                parts.append(_flatten_text(block))
        return "\n".join(parts)
    try:
        return json.dumps(payload, default=str)
    except Exception:  # noqa: BLE001
        return str(payload)


def check_payload(payload: Any, where: str, *, comp_prices: Optional[list] = None, raise_now: bool = True) -> None:
    """Scan one AI input for the subject's hidden sale facts. Raises TimeMachineLeak on a hit (with
    raise_now=False it only records it - the caller raises once the spend so far is booked)."""
    tm = _CURRENT.get()
    if not tm:
        return
    text = _flatten_text(payload)
    hits = []
    for d, label in ((tm.close_date, "closing date"), (tm.contract_date, "contract date")):
        if not d:
            continue
        for p in _date_patterns(d):
            if p in text:
                hits.append(f"{label} {p}")
    lp = tm.list_price or 0
    cp = tm.close_price or 0
    benign = {int(round(float(x))) for x in (comp_prices or []) if x}
    if cp and abs(cp - lp) > max(1000.0, 0.005 * cp) and int(round(cp)) not in benign:
        for p in _money_patterns(cp):
            for m in re.finditer(r"(?<![\w,.])" + re.escape(p) + r"(?![\w,])", text):
                hits.append(f"sale price {p} ('...{text[max(0, m.start() - 40):m.end() + 20]}...')")
                break
    tm.checks.append({"where": where, "chars": len(text), "hits": len(hits)})
    if where == "comp-analysis prompt" and tm.prompt_excerpt is None:
        tm.prompt_excerpt = text[:6000]
    if hits:
        tm.leak_log.extend(f"{where}: {h}" for h in hits)
        logger.error("CMA time machine LEAK", extra={"where": where, "hits": hits, "listing_key": tm.listing_key})
        if not raise_now:
            return
        raise TimeMachineLeak(f"post-as-of subject facts reached {where}: " + "; ".join(hits[:4]))


def raise_if_leaked(where: str) -> None:
    tm = _CURRENT.get()
    if tm and tm.leak_log:
        raise TimeMachineLeak(f"{where}: " + "; ".join(tm.leak_log[:4]))


def check_structured(subject: dict, timeline: dict, comps: list, where: str) -> None:
    """Structural assertion on what the engine is about to narrate: the subject is an active
    listing with the as-of list price, and every comp closed on or before the as-of date."""
    tm = _CURRENT.get()
    if not tm:
        return
    problems = []
    status = str((timeline or {}).get("current_status") or "").lower()
    if (timeline or {}).get("is_closed") or (timeline or {}).get("is_under_contract") or status in ("closed", "pending"):
        problems.append(f"subject status reads '{timeline.get('current_status')}'")
    for k in ("close_price", "close_date", "sale_price", "last_sale_price"):
        if (subject or {}).get(k):
            problems.append(f"subject carries {k}")
    if tm.list_price and (subject or {}).get("list_price") and abs(float(subject["list_price"]) - tm.list_price) > 1:
        problems.append("subject list price is not the as-of list price")
    for c in comps or []:
        cd = c.get("close_date") or c.get("sale_date")
        try:
            cdd = date.fromisoformat(str(cd)[:10]) if cd else None
        except ValueError:
            cdd = None
        if cdd and cdd > tm.as_of:
            problems.append(f"comp {c.get('street_address')} closed {cdd} after the as-of date")
        if c.get("listing_key") == tm.listing_key:
            problems.append("the subject itself is in the comp set")
    tm.checks.append({"where": where, "structured": True, "problems": len(problems)})
    if problems:
        tm.leak_log.extend(f"{where}: {p}" for p in problems)
        logger.error("CMA time machine LEAK (structured)", extra={"where": where, "problems": problems})
        raise TimeMachineLeak(f"{where}: " + "; ".join(problems[:4]))
