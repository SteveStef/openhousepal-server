from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import site_routes
from app.services.website import google_search_service as gss


class _FakeDb:
    def __init__(self):
        self.commits = 0

    def add(self, value):
        pass

    async def commit(self):
        self.commits += 1


def _site(**kw):
    base = dict(id="site-1", status="PUBLISHED", is_enabled=True, published_snapshot={"seo": {}}, google_search={}, subdomain_slug="jane")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def google(monkeypatch):
    """Fake Google: records calls; set .fail_verify to make verification fail."""
    calls = SimpleNamespace(log=[], fail_verify=False, host="www.janehomes.com")

    async def canonical(db, site):
        return calls.host

    async def token(url):
        calls.log.append(("token", url))
        return "abcDEF123_-xyzxyzxyz"

    async def verify(url):
        calls.log.append(("verify", url))
        if calls.fail_verify:
            raise gss.GoogleSearchError("POST /webResource -> 400 verification failed")

    async def add(url):
        calls.log.append(("add", url))

    async def sitemap(url, feed):
        calls.log.append(("sitemap", feed))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(gss, "is_configured", lambda: True)
    monkeypatch.setattr(gss.domain_resolution_service, "canonical_hostname", canonical)
    monkeypatch.setattr(gss.domain_resolution_service, "invalidate_site", lambda _id: None)
    monkeypatch.setattr(gss, "get_meta_token", token)
    monkeypatch.setattr(gss, "verify_site", verify)
    monkeypatch.setattr(gss, "add_to_search_console", add)
    monkeypatch.setattr(gss, "submit_sitemap", sitemap)
    monkeypatch.setattr(gss.asyncio, "sleep", no_sleep)
    return calls


def test_parse_code_accepts_whole_tag_or_bare_code():
    tag = '<meta name="google-site-verification" content="Ab12_cd-34EFgh56IJkl78MNop90QRst" />'
    assert gss.parse_verification_code(tag) == "Ab12_cd-34EFgh56IJkl78MNop90QRst"
    assert gss.parse_verification_code("  Ab12_cd-34EFgh56IJkl78  ") == "Ab12_cd-34EFgh56IJkl78"
    assert gss.parse_verification_code("") is None


@pytest.mark.parametrize("bad", ["short", '"><script>alert(1)</script>', "has spaces in it ok"])
def test_parse_code_rejects_junk(bad):
    with pytest.raises(ValueError):
        gss.parse_verification_code(bad)


def test_verification_codes_renders_ours_and_agents_once():
    assert gss.verification_codes({"auto_code": "a" * 20, "manual_code": "b" * 20}) == ["a" * 20, "b" * 20]
    assert gss.verification_codes({"auto_code": "a" * 20, "manual_code": "a" * 20}) == ["a" * 20]
    assert gss.verification_codes(None) == []


def test_indexnow_key_is_stable_and_per_site():
    assert gss.indexnow_key("s1") == gss.indexnow_key("s1")
    assert gss.indexnow_key("s1") != gss.indexnow_key("s2")
    assert len(gss.indexnow_key("s1")) == 32


@pytest.mark.asyncio
async def test_publish_path_does_everything_in_one_go(google):
    site = _site()
    gs = await gss.register_site(_FakeDb(), site, wait_for_tag=True)
    assert [c[0] for c in google.log] == ["token", "verify", "add", "sitemap"]
    assert ("sitemap", "https://www.janehomes.com/sitemap.xml") in google.log
    assert gs["auto_code"] and gs["verified_at"] and gs["sitemap_submitted_at"]
    assert site.google_search["host"] == "www.janehomes.com"


@pytest.mark.asyncio
async def test_sweep_path_saves_code_first_then_finishes_next_time(google):
    site = _site()
    await gss.register_site(_FakeDb(), site)
    assert [c[0] for c in google.log] == ["token"]
    assert site.google_search["auto_code"] and not site.google_search.get("verified_at")
    await gss.register_site(_FakeDb(), site)
    assert [c[0] for c in google.log] == ["token", "verify", "add", "sitemap"]
    # done -> later sweeps don't call Google at all
    await gss.register_site(_FakeDb(), site)
    assert len(google.log) == 4


@pytest.mark.asyncio
async def test_new_own_domain_starts_over_but_keeps_agents_code(google):
    site = _site(google_search={"host": "jane.openhousepal.com", "auto_code": "old" * 7, "manual_code": "mine" * 6,
                                "verified_at": "2026-10-01T00:00:00+00:00", "sitemap_submitted_at": "2026-10-01T00:00:00+00:00"})
    gs = await gss.register_site(_FakeDb(), site, wait_for_tag=True)
    assert gs["host"] == "www.janehomes.com" and gs["auto_code"] != "old" * 7
    assert gs["manual_code"] == "mine" * 6
    assert ("verify", "https://www.janehomes.com/") in google.log


@pytest.mark.asyncio
async def test_failure_is_recorded_and_backs_off(google):
    google.fail_verify = True
    site = _site()
    gs = await gss.register_site(_FakeDb(), site, wait_for_tag=True)
    assert gs["failures"] == 1 and "code on your home page" in gs["last_error"]
    n = len(google.log)
    await gss.register_site(_FakeDb(), site)  # inside the back-off window: no Google calls
    assert len(google.log) == n
    google.fail_verify = False
    site.google_search["last_attempt_at"] = gss._iso(gss._now() - timedelta(minutes=16))
    gs = await gss.register_site(_FakeDb(), site)
    assert gs["sitemap_submitted_at"] and "last_error" not in gs and gs["failures"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("site_kw", [
    {"status": "DRAFT"},
    {"is_enabled": False},
    {"published_snapshot": {"seo": {"allow_indexing": False}}},
])
async def test_never_registers_unpublished_off_or_hidden_sites(google, site_kw):
    await gss.register_site(_FakeDb(), _site(**site_kw), wait_for_tag=True)
    assert google.log == []


@pytest.mark.asyncio
async def test_never_registers_local_hosts(google):
    google.host = "jane.localhost:3000"
    await gss.register_site(_FakeDb(), _site(), wait_for_tag=True)
    assert google.log == []


@pytest.mark.asyncio
async def test_status_states(google, monkeypatch):
    db = _FakeDb()
    assert (await gss.status_for(db, _site(status="DRAFT")))["state"] == "not_live"
    assert (await gss.status_for(db, _site()))["state"] == "working"
    done = _site(google_search={"host": "www.janehomes.com", "sitemap_submitted_at": "2026-10-20T00:00:00+00:00", "verified_at": "x"})
    s = await gss.status_for(db, done)
    assert s["state"] == "done" and s["sitemap_url"] == "https://www.janehomes.com/sitemap.xml"
    # done for an OLD host doesn't count for the new domain
    done.google_search["host"] = "jane.openhousepal.com"
    assert (await gss.status_for(db, done))["state"] == "working"
    monkeypatch.setattr(gss, "is_configured", lambda: False)
    assert (await gss.status_for(db, _site()))["state"] == "unavailable"


def test_indexnow_urls_cover_main_and_custom_pages():
    urls = gss.indexnow_urls("www.janehomes.com", {"pages": [{"slug": "", "is_home": True}, {"slug": "buyers"}, {"slug": "about"}]})
    assert urls[0] == "https://www.janehomes.com/"
    assert "https://www.janehomes.com/buyers" in urls
    assert len(urls) == len(set(urls))


@pytest.mark.asyncio
async def test_set_code_route_saves_and_clears(google, monkeypatch):
    site = _site()

    async def load(db, owner_id):
        return site

    monkeypatch.setattr(site_routes, "_load_owned_site", load)
    monkeypatch.setattr(site_routes.domain_resolution_service, "invalidate_site", lambda _id: None)
    user = SimpleNamespace(id="u1")
    tag = '<meta name="google-site-verification" content="Ab12_cd-34EFgh56IJkl78MNop" />'
    out = await site_routes.set_my_google_code(site_routes.GoogleCodeIn(code=tag), db=_FakeDb(), current_user=user)
    assert out["manual_code"] == "Ab12_cd-34EFgh56IJkl78MNop"
    with pytest.raises(HTTPException) as exc:
        await site_routes.set_my_google_code(site_routes.GoogleCodeIn(code="nope"), db=_FakeDb(), current_user=user)
    assert exc.value.status_code == 400
    out = await site_routes.set_my_google_code(site_routes.GoogleCodeIn(code=""), db=_FakeDb(), current_user=user)
    assert out["manual_code"] is None


def test_build_report_shapes_days_totals_and_change():
    from datetime import datetime, timezone
    today = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    daily = [
        {"keys": ["2026-10-01"], "impressions": 100, "clicks": 4, "position": 10.0},
        {"keys": ["2026-10-28"], "impressions": 300, "clicks": 6, "position": 6.0},
        {"keys": ["2026-09-20"], "impressions": 50, "clicks": 1, "position": 20.0},  # the 28 days before
    ]
    queries = [{"keys": ["jane smith realtor"], "impressions": 80, "clicks": 9, "position": 1.23}]
    pages = [{"keys": ["https://www.janehomes.com/neighborhoods"], "impressions": 40, "clicks": 2}, {"keys": ["https://www.janehomes.com/"], "impressions": 30, "clicks": 1}]
    r = gss.build_report("www.janehomes.com", today, daily, queries, pages)
    assert len(r["days"]) == gss.REPORT_DAYS and r["days"][0]["date"] == "2026-10-01" and r["days"][-1]["impressions"] == 300
    assert r["totals"]["impressions"] == 400 and r["totals"]["clicks"] == 10
    assert r["totals"]["position"] == 7.0  # weighted by impressions: (100*10 + 300*6) / 400
    assert r["previous"]["impressions"] == 50
    assert r["queries"][0] == {"query": "jane smith realtor", "impressions": 80, "clicks": 9, "position": 1.2}
    assert [p["path"] for p in r["pages"]] == ["/neighborhoods", "/"]


@pytest.mark.asyncio
async def test_report_waits_until_google_has_the_site_and_caches(google, monkeypatch):
    calls = []

    async def analytics(site_url, start, end, dims, limit):
        calls.append(dims[0])
        return [{"keys": [end], "impressions": 12, "clicks": 1, "position": 3.0}] if dims == ["date"] else []

    monkeypatch.setattr(gss, "_search_analytics", analytics)
    site = _site()
    assert (await gss.report_for(_FakeDb(), site)) == {"ready": False, "reason": "working"}
    site.google_search = {"host": "www.janehomes.com", "verified_at": "x", "sitemap_submitted_at": "2026-10-20T00:00:00+00:00"}
    r = await gss.report_for(_FakeDb(), site)
    assert r["ready"] and r["totals"]["impressions"] == 12 and sorted(calls) == ["date", "page", "query"]
    await gss.report_for(_FakeDb(), site)  # cached - no new Google calls
    assert len(calls) == 3
