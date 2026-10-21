"""Old links: pages from the website that used to live on an agent's own domain.

When an agent connects their own domain, Google still lists the old site's pages (10/20:
mainlinerealty.co/about, /places/villanova...). Those addresses now reach our site and would be
"page not found". This finds them and stores where each one should go:

  1. collect_old_pages - the old addresses, from the Internet Archive: its capture list (CDX API),
     the old sitemaps it kept, and the links on the old home page. Read-only public GETs.
  2. match_path - pure: the best page on the new site for one old address (agent's own pages,
     built-in sections, blog posts by slug/title, town pages by name, listings by street address).
  3. run_scan - both, stored in site_redirects (an agent's own choices are never overwritten).
  4. lookup - the public renderer asks for one path (GET /public/sites/{id}/redirects).

Queued when a custom domain turns ACTIVE (domain_verification_service.verify_domain) and swept by the
5-minute domain job (sweep) so domains connected before this existed - and failed scans - get done.
Only helps once a request reaches our server: www today, the bare domain only with WEBSITE_APEX_IP.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

CDX_URL = "http://web.archive.org/cdx/search/cdx"
WAYBACK_RAW = "http://web.archive.org/web/{ts}id_/{url}"
_UA = "OpenHousePal old-links scan (+https://openhousepal.com)"
MAX_OLD_PAGES = 1500       # a real agent site has tens to a few hundred
MAX_SITEMAPS = 12
AUTO_ON_CONFIDENCE = 0.6   # matches at least this sure go live without the agent doing anything
MAX_SCAN_ATTEMPTS = 6

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ASSET_EXT = {
    "jpg", "jpeg", "png", "gif", "webp", "svg", "ico", "bmp", "tif", "tiff", "avif", "heic",
    "css", "js", "mjs", "map", "json", "xml", "txt", "rss", "atom", "xsl",
    "woff", "woff2", "ttf", "eot", "otf",
    "mp4", "mov", "webm", "m4v", "mp3", "wav", "m4a", "swf", "flv",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "zip", "gz", "rar", "csv",
}
# Whole first pieces of an address that are never a page (/feed, /wp-admin/...) ...
_SKIP_FIRST = {
    "wp-json", "wp-content", "wp-includes", "wp-admin", "wp-login.php", "xmlrpc.php", "wp-cron.php",
    "static", "assets", "cdn-cgi", "cgi-bin", ".well-known", "feed", "comments", "api", "admin", "login",
    "logout", "cart", "checkout", "my-account", "account", "user", "users", "favicon.ico", "apple-touch-icon.png",
    "trackback", "xmlrpc", "embed",
}
# ... and raw prefixes (framework/build folders, our own internal routes)
_SKIP_RAW_PREFIXES = ("/_", "/ohp-", "/wp-", "/s/files/")
# A trailing piece that's the same page in another wrapper - matched as the page itself.
_TRAILING_NOISE = {"feed", "amp", "embed", "trackback", "rss", "print", "index.html", "index.htm", "index.php"}
_PAGINATION = re.compile(r"/page/\d+$")


def normalize_path(raw: str) -> str:
    """Full URL or path -> '/lower/case/decoded' with no query, fragment or trailing slash."""
    raw = (raw or "").strip()
    if "://" in raw:
        raw = urlsplit(raw).path or "/"
    raw = raw.split("#", 1)[0].split("?", 1)[0]
    try:
        raw = unquote(raw)
    except Exception:
        pass
    raw = re.sub(r"\s+", " ", raw.lower().replace("+", " "))
    raw = re.sub(r"/{2,}", "/", "/" + raw.lstrip("/"))
    return raw.rstrip("/") or "/"


def canonical_old_path(raw: str) -> Optional[str]:
    """The stored/looked-up form of an old address, or None when it isn't a page (images, scripts,
    feeds, WordPress plumbing, the home page itself). Used by both the scan and the lookup."""
    path = normalize_path(raw)
    for _ in range(3):
        last = path.rsplit("/", 1)[-1]
        if last in _TRAILING_NOISE and path != "/":
            path = path.rsplit("/", 1)[0] or "/"
            continue
        if _PAGINATION.search(path):
            path = _PAGINATION.sub("", path) or "/"
            continue
        break
    if path == "/" or len(path) > 300 or path.count("/") > 8:
        return None
    if path.split("/")[1] in _SKIP_FIRST or path.startswith(_SKIP_RAW_PREFIXES):
        return None
    last = path.rsplit("/", 1)[-1]
    if "." in last and last.rsplit(".", 1)[-1] in _ASSET_EXT:
        return None
    return path


# ---------------------------------------------------------------------------
# Matching (pure - see tests/test_old_links_matcher.py)
# ---------------------------------------------------------------------------

@dataclass
class MatchContext:
    pages: list[tuple[str, str]] = field(default_factory=list)       # (slug, title) - the agent's own page-builder pages, not home
    blogs: list[tuple[str, str]] = field(default_factory=list)       # (slug, title) - published, shown on the website
    areas: list[tuple[str, str]] = field(default_factory=list)       # (slug, name) - their town pages (/villanova-pa)
    listings: list[tuple[str, str, str]] = field(default_factory=list)  # (listing_key, street_address, city) - their own listings
    has_reviews: bool = False
    has_hub: bool = False

    def live_paths(self) -> set[str]:
        out = {"/", "/about", "/contact", "/search", "/properties", "/blog", "/home-value", "/neighborhoods"}
        if self.has_reviews:
            out.add("/testimonials")
        if self.has_hub:
            out |= {"/sales", "/reviews"}
        out |= {normalize_path(s) for s, _ in self.pages}
        out |= {f"/blog/{s.lower()}" for s, _ in self.blogs}
        out |= {f"/{s.lower()}" for s, _ in self.areas}
        out |= {f"/properties/{k.lower()}" for k, _, _ in self.listings}
        return out


@dataclass
class Match:
    target: Optional[str]
    confidence: float
    reason: Optional[str]


_STOP = {"the", "a", "an", "and", "or", "of", "in", "on", "for", "to", "at", "by", "with", "is", "are", "your", "my", "our", "us", "me", "page"}
_STREET_SUFFIX = {
    "st", "street", "rd", "road", "ave", "av", "avenue", "ln", "lane", "dr", "drive", "ct", "court", "blvd",
    "boulevard", "way", "pl", "place", "cir", "circle", "pike", "ter", "terrace", "pkwy", "parkway", "hwy",
    "highway", "trl", "trail", "run", "sq", "square", "aly", "alley", "row", "xing", "loop", "path", "walk",
}
_LISTING_SEGS = {
    "listing", "listings", "property", "properties", "homes", "idx", "mls", "home-details", "homedetails",
    "listing-details", "listingdetails", "property-details", "propertydetails", "real-estate", "realestate",
    "for-sale", "details", "detail", "featured-listings", "my-listings", "homes-for-sale", "property-search",
    "idx-search", "search", "mls-search", "listing-detail", "property-detail", "idx-listing",
}
_POST_SEGS = {"blog", "blogs", "news", "posts", "post", "articles", "article", "insights", "media", "stories", "journal", "market-updates", "market-update"}
_TAXONOMY_SEGS = {"category", "categories", "tag", "tags", "author", "topics", "topic", "archive", "archives"}

# Same kinds of page other real estate sites use -> the section here that does that job. Mirrors the
# frontend's LEGACY_PAGE_PATHS (page.tsx), but applied to every piece of the address, not just the first.
_SECTION_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^(about([-_ ]?(me|us|[a-z]+))?|bio|biography|meet[-_ ].*|our[-_ ]?(team|agents?|story)|team|agents?|who[-_ ]we[-_ ]are|profile)$"), "/about", "About page"),
    (re.compile(r"^(contact([-_ ]?(me|us|[a-z]+))?|get[-_ ]in[-_ ]touch|reach[-_ ]out|connect|lets[-_ ]talk|schedule.*)$"), "/contact", "Contact page"),
    (re.compile(r"^(sold|sold[-_ ](listings|homes|properties)|past[-_ ](sales|transactions)|recent[-_ ]sales|closed|transactions)$"), "/sales", "Sold homes"),
    (re.compile(r"^(listings?|my[-_ ]listings|featured([-_ ](listings|properties|homes))?|exclusive[-_ ]listings|current[-_ ]listings|our[-_ ]listings|properties|properties[-_ ]for[-_ ]sale|portfolio)$"), "/properties", "Listings page"),
    (re.compile(r"^(home[-_ ]?search|search([-_ ](homes|properties|listings|mls))?|property[-_ ]search|idx|mls([-_ ]search)?|find[-_ ]a[-_ ]home|homes[-_ ]for[-_ ]sale|for[-_ ]sale|buy|buyers?|buying([-_ ].*)?|map[-_ ]search|advanced[-_ ]search)$"), "/search", "Home search"),
    (re.compile(r"^(blogs?|news|articles|posts|insights|media|videos?|video[-_ ]?library|market[-_ ](updates?|reports?|news)|resources?|resource[-_ ]?library|library|newsletters?)$"), "/blog", "Blog"),
    (re.compile(r"^(home[-_ ]?(value|valuation|worth|evaluation|estimate)([-_ ].*)?|whats?[-_ ]my[-_ ]home[-_ ]worth|sell|sellers?|selling([-_ ].*)?|cma|market[-_ ]?snap[-_ ]?shot)$"), "/home-value", "Home value page"),
    (re.compile(r"^(areas?|communities|community|neighbou?rhoods?([-_ ].*)?|towns?|townships?|places|locations|areas[-_ ]we[-_ ]serve|service[-_ ]areas|schools|region|area[-_ ]info|relocat(e|ion)([-_ ].*)?)$"), "/neighborhoods", "Neighborhoods"),
    (re.compile(r"^(testimonials?([-_ ].*)?|client[-_ ](reviews|testimonials)|reviews([-_ ].*)?|kind[-_ ]words|success[-_ ]stories)$"), "/testimonials", "Reviews page"),
    (re.compile(r"^(index|home|default|main|welcome|site[-_ ]?map)(\.(html?|php|aspx?))?$"), "/", "Home page"),
]


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def _content_tokens(text: str) -> set[str]:
    return {t for t in _tokens(text) if t not in _STOP and not t.isdigit()}


def _similarity(a: str, b: str) -> float:
    """0..1 - character similarity of the two slugs, or word overlap, whichever is higher."""
    a, b = _slugify(a), _slugify(b)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = _content_tokens(a), _content_tokens(b)
    shared = len(ta & tb)
    # word overlap only counts with 3+ shared words ("buying-a-home" isn't "buying-a-home-in-2027")
    jac = shared / len(ta | tb) if ta and tb and shared >= 3 else 0.0
    return max(ratio, jac)


def _segments(path: str) -> list[str]:
    segs = [s for s in path.strip("/").split("/") if s]
    # WordPress dates (/2019/05/12/post-name) and bare ids say nothing about the page
    return [re.sub(r"\.(html?|php|aspx?|cfm|jsp)$", "", s) for s in segs if not re.fullmatch(r"\d{1,4}", s)]


_DIRECTIONS = {"n", "s", "e", "w", "north", "south", "east", "west"}


def _address_of(seg: str) -> Optional[tuple[str, str]]:
    """'123-main-st-wayne-pa-19087' -> ('123', 'main'): street number + first street word. The
    street suffix has to come within 3 words of the number ('5-tips-on-the-path...' isn't one)."""
    toks = _tokens(seg)
    for i, t in enumerate(toks):
        if not re.fullmatch(r"\d{1,6}[a-z]?", t) or i + 2 >= len(toks):
            continue
        rest = toks[i + 1:i + 5]
        if rest[0] in _DIRECTIONS and len(rest) > 2:
            rest = rest[1:]
        if any(r in _STREET_SUFFIX for r in rest[1:4]):
            return re.sub(r"[a-z]$", "", t), rest[0]
    return None


def _street_key(street_address: str) -> Optional[tuple[str, str]]:
    toks = _tokens(street_address)
    if len(toks) < 2 or not toks[0].isdigit():
        return None
    word = toks[2] if toks[1] in _DIRECTIONS and len(toks) > 2 else toks[1]
    return toks[0], word


def _best_blog(segs: list[str], ctx: MatchContext) -> Optional[tuple[str, float]]:
    best: Optional[tuple[str, float]] = None
    for seg in reversed(segs[-2:]):
        if len(_content_tokens(seg)) < 2:
            continue
        for slug, title in ctx.blogs:
            score = max(_similarity(seg, slug), _similarity(seg, title))
            if not best or score > best[1]:
                best = (slug, score)
    return best


def _best_area(segs: list[str], ctx: MatchContext) -> Optional[tuple[str, str, int]]:
    """(area_slug, area_name, how) for the best town name found in the address. how: 3 = the whole
    piece is the town, 2 = the town as words in it, 1 = glued in ('833brynmawr', 'ardmorehomesforsale'),
    0 = a near-spelling of a one-word town ('gladwyn' / 'galdwyne' for Gladwyne)."""
    best: Optional[tuple[int, int, str, str]] = None
    for slug, name in ctx.areas:
        name_slug = _slugify(name)
        state_less = re.sub(r"-[a-z]{2}$", "", slug.lower())
        if not name_slug:
            continue
        glued_name = name_slug.replace("-", "")
        for seg in segs:
            s = _slugify(seg)
            if s in {name_slug, slug.lower(), state_less}:
                how = 3
            elif re.search(rf"(^|-){re.escape(name_slug)}(-|$)", s):
                how = 2
            elif len(glued_name) >= 6 and glued_name in s.replace("-", ""):
                how = 1
            elif "-" not in name_slug and len(name_slug) >= 6 and any(
                    len(t) >= 5 and difflib.SequenceMatcher(None, t, name_slug).ratio() >= 0.85 for t in _tokens(s)):
                how = 0
            else:
                continue
            if not best or (how, len(name_slug)) > (best[0], best[1]):
                best = (how, len(name_slug), slug, name)
    return (best[2], best[3], best[0]) if best else None


# How a page name starts says what it was ('search-main-line-real-estate', 'seller_services',
# 'buyer-services', 'around-the-main-line') - a weaker hint than the whole-name rules above.
_START_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("home-search", "search", "idx", "mls", "buy-", "buyer", "buying", "find-a-home", "homes-for-sale"), "/search", "Home search"),
    (("sell-", "seller", "selling", "home-value", "home-valuation", "what-s-my-home", "whats-my-home", "market-snapshot", "listing-agreement"), "/home-value", "Home value page"),
    (("contact", "call-", "email-"), "/contact", "Contact page"),
    (("testimonial", "client-testimonial", "client-review"), "/testimonials", "Reviews page"),
    (("relocat", "around-the", "area-", "community-", "neighborhood", "township", "schools-", "town-"), "/neighborhoods", "Neighborhoods"),
    (("featured", "coming-soon", "listing", "our-listings", "exclusive"), "/properties", "Listings page"),
    (("resource", "library", "video", "newsletter", "market-update", "market-report"), "/blog", "Blog"),
]


def _section_for(seg: str, ctx: MatchContext, starts: bool = False) -> Optional[tuple[str, str]]:
    s = seg.lower()
    rules = _SECTION_RULES if not starts else [
        (re.compile("^(" + "|".join(re.escape(k) for k in keys) + ")"), target, label) for keys, target, label in _START_RULES]
    if starts:
        s = _slugify(s)
    for rx, target, label in rules:
        if rx.match(s):
            if target == "/testimonials" and not ctx.has_reviews:
                return "/about", "About page"
            if target == "/sales" and not ctx.has_hub:
                return "/properties", "Listings page"
            if target == "/neighborhoods" and not ctx.areas:
                return "/search", "Home search"
            return target, label
    return None


def match_path(path: str, ctx: MatchContext) -> Match:
    """Where one old address should go on the new site. confidence: 0.95 same page, ~0.8 very
    likely, ~0.6 same kind of page, 0 = no idea (left for the agent)."""
    segs = _segments(path)
    if not segs:
        return Match("/", 0.9, "Home page")
    lowered = [s.lower() for s in segs]
    first, last = lowered[0], lowered[-1]
    blog_hint = bool(set(lowered[:-1]) & _POST_SEGS) or bool(re.search(r"/\d{4}/\d{1,2}/", path)) or len(_content_tokens(last)) >= 5

    # 1. A blog post with the same (or nearly the same) name
    blog = _best_blog(segs, ctx)
    if blog and blog[1] >= 0.85:
        return Match(f"/blog/{blog[0]}", 0.95, "Same blog post")
    if blog and blog[1] >= 0.72 and len(_content_tokens(segs[-1])) >= 3:
        return Match(f"/blog/{blog[0]}", 0.75, "Blog post with a similar title")

    # 2. An old listing page - the same home if it's one of theirs, else homes in that town / search
    address = next((a for a in (_address_of(s) for s in reversed(segs)) if a), None)
    area = _best_area(segs, ctx)
    if address is not None or (first in _LISTING_SEGS and len(segs) >= 2):
        if address:
            for key, street, _city in ctx.listings:
                if _street_key(street) == address:
                    return Match(f"/properties/{key}", 0.95, "Same home (your listing)")
        if area:
            return Match(f"/{area[0]}", 0.7, f"Old listing in {area[1]} - goes to its town page")
        return Match("/search", 0.65, "Old listing page - goes to home search")

    # 3. A page the agent built here with the same name
    for slug, title in ctx.pages:
        if not slug:
            continue
        if last == slug.lower() or _slugify(last) == _slugify(title):
            return Match(normalize_path(slug), 0.95, "Same page")
    for slug, title in ctx.pages:
        if slug and max(_similarity(last, slug), _similarity(last, title)) >= 0.8:
            return Match(normalize_path(slug), 0.8, "Page with a similar name")

    # 4. A town page (/places/villanova, /bryn-mawr-homes-for-sale, /communities/wayne-pa)
    if area:
        if blog_hint and area[2] < 3:
            return Match(f"/{area[0]}", 0.6, f"About {area[1]} - goes to its town page")
        return Match(f"/{area[0]}", {3: 0.95, 2: 0.8, 1: 0.75}.get(area[2], 0.7), f"{area[1]} town page")

    # 5. Blog categories/tags/authors and posts we don't have
    if first in _TAXONOMY_SEGS or (first in _POST_SEGS and len(segs) >= 2):
        return Match("/blog", 0.6, "Old blog page - goes to your blog")

    # 6. The same kind of page (about-us, contact-me, home-valuation...) - last piece first
    for seg in reversed(lowered):
        hit = _section_for(seg, ctx)
        if hit:
            exact_piece = seg == last
            return Match(hit[0], 0.8 if exact_piece and len(segs) == 1 else 0.65, hit[1])
    for seg in reversed(lowered):
        hit = _section_for(seg, ctx, starts=True)
        if hit:
            return Match(hit[0], 0.6, hit[1])

    if blog_hint:
        return Match("/blog", 0.4, "Looks like an old blog post")
    return Match(None, 0.0, None)


# ---------------------------------------------------------------------------
# Collecting old addresses (Internet Archive)
# ---------------------------------------------------------------------------

@dataclass
class Collected:
    pages: dict[str, Optional[datetime]] = field(default_factory=dict)  # canonical path -> newest capture
    sources: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _ts(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime((s or "")[:14].ljust(14, "0"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _host_variants(hostname: str) -> list[str]:
    host = hostname.lower().strip(".")
    apex = host[4:] if host.startswith("www.") else host
    return [f"www.{apex}", apex] if apex.count(".") == 1 or host.startswith("www.") else [host]


async def _get(client: httpx.AsyncClient, url: str, params: Optional[dict] = None, tries: int = 3) -> Optional[httpx.Response]:
    """The Archive is often slow or briefly "Temporarily Offline" (a 200 HTML page) - retry a few times."""
    for attempt in range(tries):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 404:
                return resp
            if resp.status_code < 400 and "Temporarily Offline" not in resp.text[:2000]:
                return resp
        except httpx.HTTPError:
            pass
        if attempt + 1 < tries:
            await asyncio.sleep(3 * (attempt + 1))
    return None


async def _cdx(client: httpx.AsyncClient, host: str) -> Optional[list[list[str]]]:
    resp = await _get(client, CDX_URL, params={
        "url": f"{host}/*", "output": "json", "fl": "original,statuscode,timestamp,mimetype",
        "collapse": "urlkey", "filter": "statuscode:200", "limit": "8000",
    })
    if resp is None or resp.status_code >= 400:
        return None
    try:
        rows = resp.json()
    except ValueError:
        return None
    return rows[1:] if rows and rows[0] and rows[0][0] == "original" else rows


_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", re.I)
_HREF = re.compile(r"""href\s*=\s*(?:"([^"#<>]+)"|'([^'#<>]+)')""", re.I)  # spaces allowed ("places/Bryn Mawr")
_SNAPSHOT_TS = re.compile(r"/web/(\d{14})")


def _same_site(url: str, hosts: Iterable[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return not host or host in set(hosts)


async def collect_old_pages(hostname: str, before: Optional[datetime] = None) -> Collected:
    """Every page address the old site had, newest capture per page. `before` = when the domain
    started pointing at us - captures after that are our own site, not the old one."""
    hosts = _host_variants(hostname)
    out = Collected()
    cutoff = before or datetime.now(timezone.utc)

    def add(raw: str, seen: Optional[datetime], source: str) -> None:
        if seen and seen > cutoff:
            return
        path = canonical_old_path(raw)
        if not path or len(out.pages) >= MAX_OLD_PAGES and path not in out.pages:
            return
        prev = out.pages.get(path)
        if path not in out.pages:
            out.sources[source] = out.sources.get(source, 0) + 1
        out.pages[path] = max(filter(None, [prev, seen]), default=None)

    sitemaps: list[tuple[str, str]] = []   # (url, timestamp)
    home_ts: Optional[str] = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True, headers={"User-Agent": _UA}) as client:
        cdx_ok = False
        for host in hosts:
            rows = await _cdx(client, host)
            if rows is None:
                continue
            cdx_ok = True
            for row in rows:
                if len(row) < 3:
                    continue
                original, _status, stamp = row[0], row[1], row[2]
                mime = (row[3] if len(row) > 3 else "") or ""
                seen = _ts(stamp)
                path_only = normalize_path(original)
                if re.search(r"sitemap[^/]*\.xml$", path_only) and len(sitemaps) < MAX_SITEMAPS and (not seen or seen <= cutoff):
                    sitemaps.append((original, stamp))
                    continue
                if path_only == "/" and (not seen or seen <= cutoff):
                    home_ts = max(home_ts or "", stamp)
                    continue
                if mime and not any(m in mime for m in ("html", "unk", "warc/revisit")):
                    continue
                add(original, seen, "archive")
        if not cdx_ok:
            out.errors.append("archive_unavailable")
            # still try the usual sitemap names and the home page directly
            stamp = cutoff.strftime("%Y%m%d%H%M%S")
            sitemaps = [(f"https://{hosts[0]}/{n}", stamp) for n in ("sitemap.xml", "sitemap_index.xml", "wp-sitemap.xml")]
            home_ts = stamp

        # Old sitemaps the Archive kept (and the ones a sitemap index lists)
        done: set[str] = set()
        queue = list(sitemaps)
        while queue and len(done) < MAX_SITEMAPS:
            url, stamp = queue.pop(0)
            if url in done:
                continue
            done.add(url)
            resp = await _get(client, WAYBACK_RAW.format(ts=stamp, url=url), tries=2)
            if resp is None or resp.status_code >= 400 or "<loc" not in resp.text[:20000].lower():
                continue
            seen = _ts(stamp)
            for loc in _LOC.findall(resp.text):
                if not _same_site(loc, hosts):
                    continue
                if normalize_path(loc).endswith(".xml"):
                    queue.append((loc, stamp))
                else:
                    add(loc, seen, "sitemap")

        # Links on the old home page - catches menus the Archive never crawled deeper into. Both its
        # first capture and the last one before the domain moved to us (menus change over the years).
        stamps = {home_ts, cutoff.strftime("%Y%m%d%H%M%S")} if home_ts else set()
        base = f"https://{hosts[0]}/"
        for stamp in sorted(s for s in stamps if s):
            resp = await _get(client, WAYBACK_RAW.format(ts=stamp, url=base), tries=2)
            if resp is None or resp.status_code >= 400:
                continue
            got = _SNAPSHOT_TS.search(str(resp.url))
            seen = _ts(got.group(1)) if got else _ts(stamp)
            if seen and seen > cutoff:
                continue  # the nearest capture is already our own site
            for a, b in _HREF.findall(resp.text[:2_000_000]):
                href = (a or b).strip()
                if href.startswith(("mailto:", "tel:", "javascript:", "data:", "sms:")):
                    continue
                full = urljoin(base, href)
                # the Archive sometimes rewrites links to /web/<ts>/<original>
                m = re.match(r"^https?://web\.archive\.org/web/\d+[a-z_]*/(.+)$", full)
                if m:
                    full = m.group(1)
                if _same_site(full, hosts) and urlsplit(full).scheme in ("http", "https", ""):
                    add(full, seen, "home_page")
    return out


# ---------------------------------------------------------------------------
# The scan job
# ---------------------------------------------------------------------------

_RUNNING: set[str] = set()
_TASKS: set = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def main_custom_domain(db: AsyncSession, site_id: str):
    """The site's connected own domain (the www half of a pair, or a subdomain they chose)."""
    from app.models.database import SiteDomain
    rows = (await db.execute(select(SiteDomain).where(
        SiteDomain.site_id == site_id, SiteDomain.status == "ACTIVE", SiteDomain.redirect_target_domain_id.is_(None),
    ))).scalars().all()
    rows = sorted(rows, key=lambda d: (not d.is_primary, d.created_at or _now()))
    return rows[0] if rows else None


async def build_context(db: AsyncSession, site) -> MatchContext:
    from app.models.database import AgentHub, AgentMarketArea, Blog, MarketArea, Property, User
    from app.services.website import content_adapters
    user = await db.get(User, site.owner_id)
    pages = [(p.get("slug") or "", p.get("title") or "") for p in ((site.published_snapshot or {}).get("pages") or [])
             if isinstance(p, dict) and not p.get("is_home") and p.get("slug")]
    blogs = (await db.execute(select(Blog.slug, Blog.title).where(
        Blog.owner_id == site.owner_id, Blog.status == "PUBLISHED", Blog.idx_visible.is_(True)))).all()
    areas = (await db.execute(select(MarketArea.slug, MarketArea.name).join(
        AgentMarketArea, AgentMarketArea.area_id == MarketArea.id).where(AgentMarketArea.owner_id == site.owner_id))).all()
    listings = []
    if user and user.mls_id:
        from sqlalchemy import or_
        listings = (await db.execute(select(Property.listing_key, Property.street_address, Property.city).where(
            or_(Property.list_agent_mls_id == user.mls_id, Property.co_list_agent_mls_id == user.mls_id),
            Property.home_status.notin_(list(content_adapters._IDX_NOT_SHOWN_STATUSES)),
        ).limit(1000))).all()
    has_hub = (await db.execute(select(AgentHub.id).where(AgentHub.owner_id == site.owner_id))).first() is not None
    has_reviews = False
    if user:
        try:
            from app.utils.auth import REVIEWS_ACCESS_TIERS, owner_has_access
            has_reviews = owner_has_access(user, REVIEWS_ACCESS_TIERS) and bool((await content_adapters.get_site_reviews(db, user, limit=0))["show"])
        except Exception:
            has_reviews = False
    return MatchContext(
        pages=[(s, t) for s, t in pages], blogs=[(b.slug, b.title) for b in blogs],
        areas=[(a.slug, a.name) for a in areas], listings=[(l.listing_key, l.street_address or "", l.city or "") for l in listings],
        has_reviews=has_reviews, has_hub=has_hub,
    )


async def run_scan(site_id: str) -> None:
    """Collect + match + store. Own DB session (runs in the background). Never raises."""
    from app.database import AsyncSessionLocal
    from app.models.database import Site, SiteRedirect
    if site_id in _RUNNING:
        return
    _RUNNING.add(site_id)
    try:
        async with AsyncSessionLocal() as db:
            site = await db.get(Site, site_id)
            domain = await main_custom_domain(db, site_id) if site else None
            if not site or not domain:
                return
            apex = domain.hostname[4:] if domain.hostname.startswith("www.") else domain.hostname
            hostname, before = domain.hostname, domain.verified_at or domain.created_at
            prev = dict(site.old_links_scan or {})
            attempts = (prev.get("attempts") or 0) + 1 if prev.get("domain") == apex and prev.get("status") == "failed" else 1
            site.old_links_scan = {"domain": apex, "status": "running", "started_at": _now().isoformat(), "attempts": attempts,
                                   "found": prev.get("found"), "matched": prev.get("matched")}
            await db.commit()

        try:
            collected = await collect_old_pages(hostname, before)
        except Exception as e:  # network trouble of any kind
            logger.warning("old links scan failed", extra={"site_id": site_id, "error": str(e)})
            collected = Collected(errors=["scan_error"])

        async with AsyncSessionLocal() as db:
            site = await db.get(Site, site_id)
            if not site:
                return
            if not collected.pages and collected.errors:
                site.old_links_scan = {**(site.old_links_scan or {}), "status": "failed", "finished_at": _now().isoformat(),
                                       "error": "The Internet Archive didn't answer - we'll try again soon."}
                await db.commit()
                return
            ctx = await build_context(db, site)
            live = ctx.live_paths()
            existing = {r.source_path: r for r in (await db.execute(select(SiteRedirect).where(SiteRedirect.site_id == site_id))).scalars().all()}
            for path, seen in collected.pages.items():
                if path in live:
                    continue
                row = existing.get(path)
                if row and row.agent_approved:
                    row.last_seen_at = seen or row.last_seen_at
                    continue
                m = match_path(path, ctx)
                if not row:
                    row = SiteRedirect(site_id=site_id, source_path=path)
                    db.add(row)
                    existing[path] = row
                row.target_path, row.confidence, row.match_reason = m.target, m.confidence, m.reason
                row.is_enabled = bool(m.target) and m.confidence >= AUTO_ON_CONFIDENCE
                row.last_seen_at = seen
            await db.flush()
            rows = [r for r in existing.values() if r.source_path not in live]
            site.old_links_scan = {
                **(site.old_links_scan or {}), "status": "done", "finished_at": _now().isoformat(), "error": None,
                "found": len(rows), "matched": sum(1 for r in rows if r.is_enabled and r.target_path),
                "sources": collected.sources, "partial": bool(collected.errors),
            }
            await db.commit()
            logger.info("old links scan done", extra={"site_id": site_id, "domain": site.old_links_scan.get("domain"),
                                                      "found": len(rows), "sources": collected.sources})
    except Exception:
        logger.exception("old links scan crashed", extra={"site_id": site_id})
        try:
            async with AsyncSessionLocal() as db:
                site = await db.get(Site, site_id)
                if site and (site.old_links_scan or {}).get("status") == "running":
                    site.old_links_scan = {**site.old_links_scan, "status": "failed", "finished_at": _now().isoformat(),
                                           "error": "Something went wrong - we'll try again soon."}
                    await db.commit()
        except Exception:
            pass
    finally:
        _RUNNING.discard(site_id)


def queue_scan(site_id: str) -> None:
    """Fire-and-forget (from a request or the scheduler); a reference is kept so it isn't collected."""
    try:
        task = asyncio.get_running_loop().create_task(run_scan(site_id))
    except RuntimeError:
        return
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def is_running(site_id: str) -> bool:
    return site_id in _RUNNING


async def sweep(db: AsyncSession, limit: int = 3) -> int:
    """5-minute job: sites with a connected own domain never scanned for it (connected before this
    existed, or a different domain since), or whose scan failed (retried with growing gaps)."""
    from app.models.database import Site, SiteDomain
    rows = (await db.execute(
        select(Site, SiteDomain).join(SiteDomain, SiteDomain.site_id == Site.id).where(and_(
            SiteDomain.status == "ACTIVE", SiteDomain.redirect_target_domain_id.is_(None)))
    )).all()
    queued, seen = 0, set()
    now = _now()
    for site, domain in rows:
        if site.id in seen or site.id in _RUNNING:
            continue
        seen.add(site.id)
        apex = domain.hostname[4:] if domain.hostname.startswith("www.") else domain.hostname
        state = site.old_links_scan or {}
        due = state.get("domain") != apex
        if not due and state.get("status") == "failed" and (state.get("attempts") or 0) < MAX_SCAN_ATTEMPTS:
            finished = datetime.fromisoformat(state["finished_at"]) if state.get("finished_at") else None
            due = not finished or now - finished > timedelta(minutes=30 * (state.get("attempts") or 1))
        if not due and state.get("status") == "running":
            started = datetime.fromisoformat(state["started_at"]) if state.get("started_at") else None
            due = not started or now - started > timedelta(minutes=20)  # a restart killed it mid-scan
        if due:
            queue_scan(site.id)
            queued += 1
            if queued >= limit:
                break
    return queued


async def lookup(db: AsyncSession, site_id: str, raw_path: str) -> Optional[str]:
    """Where an old address goes now, or None. A town page added since the scan wins over its row."""
    from app.models.database import AgentMarketArea, MarketArea, Site, SiteRedirect
    path = canonical_old_path(raw_path)
    if not path:
        return None
    row = (await db.execute(select(SiteRedirect.target_path).where(
        SiteRedirect.site_id == site_id, SiteRedirect.source_path == path,
        SiteRedirect.is_enabled.is_(True), SiteRedirect.target_path.isnot(None)))).first()
    if not row:
        return None
    if path.count("/") == 1:
        owner_id = (await db.execute(select(Site.owner_id).where(Site.id == site_id))).scalar_one_or_none()
        town = (await db.execute(select(MarketArea.id).join(AgentMarketArea, AgentMarketArea.area_id == MarketArea.id).where(
            AgentMarketArea.owner_id == owner_id, MarketArea.slug == path[1:]))).first() if owner_id else None
        if town:
            return None
    return row[0] if row[0] != path else None


# ---------------------------------------------------------------------------
# The Domain tab's list
# ---------------------------------------------------------------------------

def target_options(ctx: MatchContext) -> list[dict]:
    """Every page an old address can be sent to, grouped for the dropdown."""
    opts = [{"path": "/", "label": "Home page", "group": "Main pages"}]
    main = [("/about", "About"), ("/contact", "Contact"), ("/search", "Home search"), ("/properties", "Your listings"),
            ("/blog", "Blog"), ("/home-value", "What's my home worth"), ("/neighborhoods", "Neighborhoods")]
    if ctx.has_reviews:
        main.append(("/testimonials", "Reviews"))
    if ctx.has_hub:
        main.append(("/sales", "Sold homes"))
    opts += [{"path": p, "label": l, "group": "Main pages"} for p, l in main]
    opts += [{"path": normalize_path(s), "label": t or s, "group": "Your pages"} for s, t in ctx.pages]
    opts += [{"path": f"/{s}", "label": n, "group": "Town pages"} for s, n in sorted(ctx.areas, key=lambda a: a[1])]
    opts += [{"path": f"/blog/{s}", "label": t, "group": "Blog posts"} for s, t in ctx.blogs]
    opts += [{"path": f"/properties/{k}", "label": f"{st}, {c}".strip(", "), "group": "Your listings"} for k, st, c in ctx.listings[:200]]
    seen, out = set(), []
    for o in opts:
        if o["path"] not in seen:
            seen.add(o["path"])
            out.append(o)
    return out
