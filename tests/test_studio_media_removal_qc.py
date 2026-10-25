"""Circled object removal: the shadow-covering mask and the free quality check (10/24)."""
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFilter

from app.services.studio_media_service import _apply_shift, _removal_masks, _removal_qc, _shift

W, H = 1200, 800
AREAS = [[[0.30, 0.40], [0.55, 0.40], [0.55, 0.70], [0.30, 0.70]]]


def _png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@lru_cache(maxsize=1)
def _base() -> Image.Image:
    # A textured "photo": noise + a few hard lines (a wall, a floor seam) so both the shift
    # search and the sharpness check have real detail to work with.
    noise = Image.effect_noise((W, H), 60).filter(ImageFilter.GaussianBlur(2)).convert("RGB")
    d = ImageDraw.Draw(noise)
    for x in range(0, W, 90):
        d.line((x, 0, x + 40, H), fill=(30, 30, 30), width=5)
    d.line((0, H * 0.55, W, H * 0.6), fill=(240, 240, 240), width=7)
    # the "car" to remove: a dark block inside the circled area
    d.rectangle((W * 0.34, H * 0.45, W * 0.51, H * 0.66), fill=(10, 10, 12))
    return noise


def _source() -> Image.Image:
    return _base().copy()


def _masks():
    src = _png(_source())
    _api, blend = _removal_masks(src, AREAS)
    return src, blend


def test_mask_reaches_below_the_circle_for_the_shadow():
    src, blend = _masks()
    # straight under the circle's bottom edge (y=0.70) - where a car's shadow falls
    assert blend.getpixel((int(W * 0.42), int(H * 0.73))) > 128
    # well above the circle is untouched
    assert blend.getpixel((int(W * 0.42), int(H * 0.30))) == 0


def test_good_removal_passes():
    src, blend = _masks()
    edit = _source()
    # the block replaced by fresh texture like its surroundings
    patch = Image.effect_noise((int(W * 0.2), int(H * 0.25)), 60).convert("RGB")
    edit.paste(patch, (int(W * 0.33), int(H * 0.44)))
    qc = _removal_qc(_png(edit), src, blend)
    assert qc["bad"] == [], qc


def test_object_left_in_place_is_flagged():
    src, blend = _masks()
    qc = _removal_qc(src, src, blend)
    assert "not_removed" in qc["bad"], qc


def test_smear_is_flagged():
    src, blend = _masks()
    edit = _source()
    region = (int(W * 0.30), int(H * 0.38), int(W * 0.57), int(H * 0.74))
    edit.paste(edit.crop(region).filter(ImageFilter.GaussianBlur(40)), region[:2])
    qc = _removal_qc(_png(edit), src, blend)
    assert "smeared" in qc["bad"], qc


def test_shifted_result_is_lined_back_up():
    src, blend = _masks()
    edit = _source()
    patch = Image.effect_noise((int(W * 0.2), int(H * 0.25)), 60).convert("RGB")
    edit.paste(patch, (int(W * 0.33), int(H * 0.44)))
    moved = _shift(edit, -12, 9)  # the model returned the photo nudged 12px / 9px
    qc = _removal_qc(_png(moved), src, blend)
    assert "misaligned" not in qc["bad"], qc
    assert qc["align_fixed"] < qc["align"]
    dx, dy = qc["shift"]
    assert abs(dx - 12) <= 4 and abs(dy + 9) <= 4, qc
    fixed = Image.open(BytesIO(_apply_shift(_png(moved), src, qc["shift"])))
    assert fixed.size == (W, H)
