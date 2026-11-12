"""A small line above an email's header - "Your digest: every day at 4:30 PM · Change how often or
when" (owner, 11/11). The design lives in Mailgun (its stored template), and the live Mailgun domain
is at its template limit, so instead of uploading a new design we fetch the live one, render it here
and add the line. Anything going wrong returns None and the email goes out exactly as before."""
from __future__ import annotations

import html as _h
import logging
import os
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TOP_LINE_TEMPLATES = frozenset({"showcase_daily_digest_agent"})
_CACHE: dict[str, tuple[float, object]] = {}
_TTL = 600
_ANCHOR = re.compile(r'<table[^>]*class="container"', re.I)


def _compiled(name: str):
    hit = _CACHE.get(name)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    url = os.getenv("MAILGUN_URL") or ""
    m = re.match(r"(https?://[^/]+)/v3/([^/]+)/", url)
    key = os.getenv("MAILGUN_API_KEY") or ""
    if not m or not key:
        return None
    from app.services.email_service import mailgun_template_name
    r = httpx.get(f"{m.group(1)}/v3/{m.group(2)}/templates/{mailgun_template_name(name)}", params={"active": "yes"},
                  auth=("api", key), timeout=15)
    if r.status_code >= 300:
        return None
    source = ((r.json().get("template") or {}).get("version") or {}).get("template") or ""
    if not source or not _ANCHOR.search(source):
        return None
    from pybars import Compiler
    compiled = Compiler().compile(source)
    _CACHE[name] = (time.monotonic(), compiled)
    return compiled


def line_html(words: str, link_text: str, link: str) -> str:
    return ('<div style="max-width:600px;margin:0 auto 10px auto;text-align:center;font-family:-apple-system,BlinkMacSystemFont,'
            "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:12px;line-height:1.5;color:#8a7f6e;\" class=\"ohp-muted\">"
            f'{_h.escape(words)} &middot; <a href="{_h.escape(link, quote=True)}" style="color:#8b6d2f;font-weight:700;text-decoration:underline;">'
            f'{_h.escape(link_text)}</a></div>')


def inject(html: str, line: str) -> Optional[str]:
    """The line just above the email's main card (inside the page background). None = no safe spot."""
    m = _ANCHOR.search(html or "")
    if not m:
        return None
    return html[:m.start()] + line + html[m.start():]


def _text(html: str) -> str:
    t = re.sub(r"(?is)<(script|style|head).*?</\1>", "", html)
    t = re.sub(r'(?is)<div style="display:none.*?</div>', "", t)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|h\d|li)>", "\n", t)
    t = re.sub(r'(?i)<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"\2 (\1)", t)
    t = _h.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", t)).strip()


def render(template: str, variables: dict) -> Optional[tuple[str, str]]:
    """(html, text) with the top line, or None to send the normal way."""
    words, link_text, link = variables.get("top_line"), variables.get("top_line_link_text"), variables.get("top_line_link")
    if template not in TOP_LINE_TEMPLATES or not (words and link_text and link):
        return None
    try:
        compiled = _compiled(template)
        if compiled is None:
            return None
        html = str(compiled(variables))
        out = inject(html, line_html(str(words), str(link_text), str(link)))
        return (out, _text(out)) if out else None
    except Exception:  # noqa: BLE001 - never stop an email over this
        logger.warning("top line render failed - sending the normal way", exc_info=True, extra={"template": template})
        return None
