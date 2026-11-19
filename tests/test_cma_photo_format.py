"""One photo the AI can't take must never cost the agent the whole report.

735 Concord Ave, Drexel Hill (11/13): Bright served two of the report's photos labelled "image/jpg" - not a real
type. The label was passed straight to the AI, the provider refused the whole request, and the report failed with
"Couldn't generate the AI analysis right now". The type is now read from the file itself."""
import asyncio
from io import BytesIO

from PIL import Image

from app.services import cma_ai_service as svc


def _img(fmt: str, size=(640, 480)) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, (120, 140, 160)).save(out, format=fmt)
    return out.getvalue()


class _Resp:
    is_redirect = False  # safe_get (11/18 security pass) follows redirects itself

    def __init__(self, content, content_type):
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None


class _Http:
    def __init__(self, resp):
        self.resp = resp

    async def get(self, url, timeout=None, **kwargs):
        return self.resp


def _block(content, content_type, monkeypatch=None):
    return asyncio.run(svc._fetch_image_block(_Http(_Resp(content, content_type)), "http://photos.example/1.jpg"))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _public_address(monkeypatch):
    """The made-up photos.example host doesn't resolve; these tests are about photo formats, not the
    public-address check (app.utils.safe_fetch), so treat it as public."""
    async def _ok(url, trust_own=True):
        return None
    monkeypatch.setattr("app.utils.safe_fetch.assert_public_url", _ok)


def test_mislabelled_jpeg_is_sent_as_a_real_jpeg():
    block, tokens = _block(_img("JPEG"), "image/jpg")
    assert block["source"]["media_type"] == "image/jpeg"
    assert tokens > 0


def test_label_with_charset_and_wrong_type_follows_the_file():
    block, _ = _block(_img("PNG"), "image/jpeg; charset=binary")
    assert block["source"]["media_type"] == "image/png"


def test_small_photo_in_a_format_the_ai_does_not_take_is_resaved_as_jpeg():
    block, _ = _block(_img("BMP"), "image/bmp")
    assert block["source"]["media_type"] == "image/jpeg"
    import base64
    with Image.open(BytesIO(base64.b64decode(block["source"]["data"]))) as im:
        assert im.format == "JPEG"


def test_big_photo_is_still_shrunk_to_a_jpeg():
    edge = svc._MAX_IMAGE_EDGE_PX
    data, ctype, w, h = svc._resize_if_needed(_img("PNG", (edge * 2, edge)), "image/png")
    assert ctype == "image/jpeg" and max(w, h) == edge


def test_unreadable_file_with_a_bad_label_is_skipped_not_sent():
    assert _block(b"not an image at all", "image/jpg") is None


def test_every_type_we_send_is_one_the_ai_accepts():
    assert set(svc._AI_IMAGE_TYPES.values()) == {"image/jpeg", "image/png", "image/gif", "image/webp"}
