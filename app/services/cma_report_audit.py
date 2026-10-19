"""Free, deterministic word-level audit of a finished CMA report against the data it should match.

Used by the CMA accuracy test to pre-screen every generated report before the human word-for-word
review: each dollar figure, $/sq ft, square footage, year built, acreage, days-on-market count,
"X% above/below asking" claim and date in the client-facing text is checked against the
structured subject / comp / valuation values. Findings are hints for the reviewer, never edits.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

_MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([mMkK](?![a-z]))?(\s*(?:/|per\s+)\s*(?:sq\.?\s*ft|sqft|square\s+f(?:oo|ee)t))?", re.IGNORECASE)
_SQFT_RE = re.compile(r"\b(\d{1,2},\d{3}|\d{3,5})\s*(?:sq\.?\s*ft|sqft|square\s+feet)\b(?!\s*(?:lot|of\s+land))", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(?:built\s+in|built|circa|c\.)\s+(1[6-9]\d\d|20\d\d)\b|\b(1[6-9]\d\d|20[0-2]\d)[- ]built\b", re.IGNORECASE)
_ACRE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:-|\s)?acres?\b", re.IGNORECASE)
_DOM_RE = re.compile(r"\b(\d{1,4})\s+(?:active[- ]market\s+)?days?\b(?:\s+on\s+(?:the\s+)?market)?", re.IGNORECASE)
_PCT_ASK_RE = re.compile(r"(?:about\s+|roughly\s+|~)?(\d{1,2}(?:\.\d)?)%(?:\s*-\s*(\d{1,2})%)?\s+(above|below|over|under)\s+(?:the\s+)?(?:\$[\d,]+\s+)?(?:asking|list)", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b|\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(20\d\d)\b")
_MONTHS = {m: i for i, m in enumerate(("January", "February", "March", "April", "May", "June", "July", "August",
                                          "September", "October", "November", "December"), start=1)}


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and x != 0 else None
    except (TypeError, ValueError):
        return None


def client_text_sections(report: dict) -> dict:
    """Every client-facing prose field, keyed by where it shows."""
    pricing = report.get("pricing") or {}
    est = pricing.get("ai_estimate") or {}
    out = {}
    for key in ("methodology_summary", "offer_strategy_considerations", "inspection_concerns",
                "listing_strategy_considerations", "listing_prep_concerns"):
        if est.get(key):
            out[key] = str(est[key])
    for i, d in enumerate(est.get("value_drivers") or []):
        if isinstance(d, dict) and d.get("note"):
            out[f"why_this_value[{i + 1}] {d.get('comp_label') or ''}".strip()] = str(d["note"])
    for i, c in enumerate(report.get("comparables") or []):
        t = c.get("commentary") or c.get("ai_commentary")
        if t:
            out[f"comp#{i + 1} {c.get('street_address') or ''}".strip()] = str(t)
    for i, imp in enumerate(report.get("improvements") or []):
        t = " ".join(str(imp.get(k) or "") for k in ("description", "estimated_value_context")).strip()
        if t:
            out[f"improvement[{i + 1}]"] = t
    return out


def _props(report: dict) -> list[dict]:
    rows = [dict(report.get("subject") or {}, _role="subject")]
    rows += [dict(c, _role=f"comp#{i + 1}") for i, c in enumerate(report.get("comparables") or [])]
    return rows


def audit_report(report: dict, *, as_of: Optional[date] = None, subject_dom: Optional[int] = None) -> list[dict]:
    pricing = report.get("pricing") or {}
    est = pricing.get("ai_estimate") or {}
    fv = est.get("final_valuation") or {}
    of = est.get("offer_strategy_fields") or {}
    props = _props(report)
    trace = est.get("valuation_trace") or {}
    anchor = (trace.get("anchor") or {})

    money = set()
    for v in (est.get("value"), est.get("range_min"), est.get("range_max"), fv.get("anchor_value"), anchor.get("anchor_value"),
              pricing.get("recommended_list_price"), pricing.get("most_likely_value"), pricing.get("recommended_min"),
              pricing.get("recommended_max"), pricing.get("estimated_fair_value"), *of.values()):
        if _f(v) and _f(v) > 1000:
            money.add(_f(v))
    for p in props:
        for k in ("list_price", "sale_price", "close_price", "original_list_price"):
            if _f(p.get(k)):
                money.add(_f(p.get(k)))
    psf = set()
    sqft = set()
    years = set()
    acres = set()
    doms = set()
    for p in props:
        for k in ("sqft", "above_grade_finished_area", "below_grade_finished_area"):
            if _f(p.get(k)):
                sqft.add(_f(p[k]))
        price = _f(p.get("sale_price")) or _f(p.get("list_price"))
        for k in ("sqft", "above_grade_finished_area"):
            if price and _f(p.get(k)):
                psf.add(price / _f(p[k]))
        if _f(p.get("price_per_sqft")):
            psf.add(_f(p["price_per_sqft"]))
        if p.get("year_built"):
            years.add(int(p["year_built"]))
        if _f(p.get("lot_size")):
            acres.add(_f(p["lot_size"]) / 43560.0)
        if p.get("days_on_market") is not None:
            doms.add(int(p["days_on_market"]))
    for t in (anchor.get("comps") or []):
        if _f(t.get("adjusted_above_grade_psf")):
            psf.add(_f(t["adjusted_above_grade_psf"]))
    if subject_dom is not None:
        doms.add(int(subject_dom))
    stl = (est.get("status") or {}).get("active_market_days")
    if stl is not None:
        doms.add(int(stl))
    comp_close_dates = {str(c.get("close_date") or c.get("sale_date"))[:10] for c in (report.get("comparables") or []) if c.get("close_date") or c.get("sale_date")}
    ask = _f((report.get("subject") or {}).get("list_price"))

    findings = []

    def add(section, severity, msg, excerpt):
        findings.append({"section": section, "severity": severity, "message": msg, "excerpt": excerpt[:220]})

    for section, text in client_text_sections(report).items():
        for m in _MONEY_RE.finditer(text):
            num = float(m.group(1).replace(",", ""))
            suf = (m.group(2) or "").lower()
            num *= 1_000_000 if suf == "m" else 1_000 if suf == "k" else 1
            ctx = text[max(0, m.start() - 60):m.end() + 40]
            if m.group(3):  # $/sq ft
                if psf and not any(abs(num - x) <= max(2.0, 0.025 * x) for x in psf):
                    add(section, "check", f"${num:,.0f}/sq ft doesn't match any subject/comp price per square foot", ctx)
                continue
            if num < 10_000:
                continue
            tol = 0.012 if suf else 0.006
            if not any(abs(num - x) <= max(1500.0, tol * x) for x in money):
                add(section, "check", f"${num:,.0f} doesn't match any price, value, range or offer figure in the data", ctx)
        for m in _SQFT_RE.finditer(text):
            num = float(m.group(1).replace(",", ""))
            if num >= 300 and sqft and not any(abs(num - x) <= max(5.0, 0.006 * x) for x in sqft):
                add(section, "check", f"{num:,.0f} sq ft doesn't match any recorded area", text[max(0, m.start() - 60):m.end() + 30])
        for m in _YEAR_RE.finditer(text):
            y = int(m.group(1) or m.group(2))
            if years and y not in years:
                add(section, "check", f"built {y} doesn't match any recorded year built", text[max(0, m.start() - 50):m.end() + 30])
        for m in _ACRE_RE.finditer(text):
            a = float(m.group(1))
            if acres and a > 0.01 and not any(abs(a - x) <= max(0.02, 0.03 * x) for x in acres):
                add(section, "check", f"{a} acres doesn't match any recorded lot size", text[max(0, m.start() - 50):m.end() + 30])
        for m in _DOM_RE.finditer(text):
            d = int(m.group(1))
            around = text[max(0, m.start() - 40):m.end() + 25].lower()
            if "market" in around or "on market" in around or "active" in around or "listed" in around:
                if doms and d not in doms and not any(w in around for w in ("median", "typical", "average", "within", "under", "less than", "about")):
                    add(section, "check", f"{d} days on market doesn't match the subject's or any comp's recorded DOM", text[max(0, m.start() - 60):m.end() + 30])
        if ask:
            for m in _PCT_ASK_RE.finditer(text):
                claimed = [float(m.group(1))] + ([float(m.group(2))] if m.group(2) else [])
                direction = 1 if m.group(3).lower() in ("above", "over") else -1
                nearby = [float(x.replace(",", "")) for x in re.findall(r"\$\s?(\d{1,3}(?:,\d{3})+)", text[max(0, m.start() - 200):m.start()])]
                figs = [x for x in nearby if abs(x - ask) > 1] or ([_f(est.get("value"))] if _f(est.get("value")) else [])
                actual = sorted(round(abs(x - ask) / ask * 100, 1) for x in figs if (x - ask) * direction > 0)
                if actual and not any(any(abs(c - a) <= 1.0 for a in actual) for c in claimed):
                    add(section, "check", f"'{m.group(0)}' - the nearby figures are actually {', '.join(f'{a}%' for a in actual[:3])} vs the ${ask:,.0f} asking price", text[max(0, m.start() - 80):m.end() + 20])
        for m in _DATE_RE.finditer(text):
            try:
                d = date.fromisoformat(m.group(1)) if m.group(1) else date(int(m.group(4)), _MONTHS[m.group(2)], int(m.group(3)))
            except ValueError:
                continue
            if as_of and d > as_of:
                add(section, "leak", f"mentions {d.isoformat()}, after the as-of date {as_of.isoformat()}", text[max(0, m.start() - 60):m.end() + 20])
            elif m.group(1) and d.isoformat() not in comp_close_dates:
                add(section, "info", f"date {d.isoformat()} isn't a comp closing date - check what it refers to", text[max(0, m.start() - 60):m.end() + 20])
        low = text.lower()
        if section.startswith(("methodology", "offer", "listing")) and re.search(
                r"\b(?:this|the|your)\s+(?:home|property|house|subject)\s+(?:has\s+)?(?:already\s+)?(?:sold|closed|is\s+under\s+contract|went\s+under\s+contract)\b", low):
            add(section, "leak" if as_of else "check", "describes the subject as sold / under contract", text[:200])
    return findings
