"""Blog / design / invite / open house photos (POST /studio/media): any JPG, PNG, WebP or iPhone photo is accepted
and stored as an upright JPEG with the phone's GPS location gone - these photos go on public pages (Day 12 audit)."""
import io

import pytest
from PIL import Image

from app.api.studio_routes import PHOTO_MAX_SIDE
from app.utils.image_validation import headshot_jpeg

GPS = {1: "N", 2: (40.0, 2.0, 30.0), 3: "W", 4: (75.0, 23.0, 10.0)}


def _photo(fmt, size=(4000, 3000), orientation=None, gps=False):
    im = Image.new("RGB", size, (90, 140, 200))
    ex = Image.Exif()
    if orientation:
        ex[0x0112] = orientation
    if gps:
        ex[0x8825] = GPS
    b = io.BytesIO()
    im.save(b, fmt, **({"exif": ex} if fmt == "JPEG" else {}))
    return b.getvalue()


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_every_photo_type_becomes_a_small_enough_jpeg(fmt):
    out = Image.open(io.BytesIO(headshot_jpeg(_photo(fmt), PHOTO_MAX_SIDE)))
    assert out.format == "JPEG"
    assert max(out.size) == PHOTO_MAX_SIDE


def test_location_is_removed_and_photo_turned_upright():
    out = Image.open(io.BytesIO(headshot_jpeg(_photo("JPEG", orientation=6, gps=True), PHOTO_MAX_SIDE)))
    assert not out.getexif().get_ifd(0x8825)  # no GPS
    assert out.size[1] > out.size[0]           # taken holding the phone upright -> portrait


def test_small_photo_is_not_blown_up():
    out = Image.open(io.BytesIO(headshot_jpeg(_photo("PNG", size=(800, 600)), PHOTO_MAX_SIDE)))
    assert out.size == (800, 600)


def test_not_a_photo_is_refused():
    with pytest.raises(ValueError):
        headshot_jpeg(b"hello, not an image" * 50, PHOTO_MAX_SIDE)
