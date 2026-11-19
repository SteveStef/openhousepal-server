"""Security pass 11/18: fetchers of agent/guest-supplied URLs refuse our own network (app.utils.safe_fetch).
The vCard photo, AI chat attachments and CMA photos now go through safe_get like the signature builder."""
import asyncio

import pytest

from app.utils.safe_fetch import UnsafeUrlError, assert_public_url


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:5432/",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://[::1]/",
    "file:///etc/passwd",
    "ftp://example.com/x.jpg",
])
def test_inside_addresses_are_refused(url):
    with pytest.raises(UnsafeUrlError):
        asyncio.run(assert_public_url(url))


def test_vcard_photo_from_inside_is_dropped():
    from app.services.vcard_service import fetch_photo_base64
    assert asyncio.run(fetch_photo_base64("http://169.254.169.254/latest/meta-data/")) is None


def test_ai_attachment_from_inside_is_refused():
    from app.services.ai.ai_attachments import _fetch
    with pytest.raises(Exception):
        asyncio.run(_fetch("http://127.0.0.1:5432/"))


def test_own_api_trusted_by_exact_origin_only(monkeypatch):
    monkeypatch.setenv("API_URL", "http://127.0.0.1:8095")
    asyncio.run(assert_public_url("http://127.0.0.1:8095/uploads/x.jpg"))      # our API: fine
    with pytest.raises(UnsafeUrlError):
        asyncio.run(assert_public_url("http://127.0.0.1:5432/"))               # same machine, other port
    with pytest.raises(UnsafeUrlError):
        asyncio.run(assert_public_url("http://127.0.0.1:8095/x", trust_own=False))  # reached by a redirect


def test_outside_redirect_to_own_api_is_refused(monkeypatch):
    """A public page that redirects to our API's inside address is refused; our API's own files still load."""
    import httpx
    from app.utils import safe_fetch
    monkeypatch.setenv("API_URL", "http://127.0.0.1:8095")

    async def _public(url, trust_own=True):
        if "outside.example" in url:
            return None
        return await real(url, trust_own)
    real = safe_fetch.assert_public_url
    monkeypatch.setattr(safe_fetch, "assert_public_url", _public)

    def handler(request):
        if request.url.host == "outside.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8095/uploads/x.jpg"})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"img")

    async def go(url):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await safe_fetch.safe_get(c, url)
    assert asyncio.run(go("http://127.0.0.1:8095/uploads/x.jpg")).status_code == 200
    with pytest.raises(UnsafeUrlError):
        asyncio.run(go("https://outside.example/photo.jpg"))
