"""A white see-through logo vanishes on white (owner, 11/05: Peter's Compass logo). A logo is never
recolored (owner, 11/06), so the logos we draw ourselves follow the website's rule (frontend
lib/logoTone.ts): one that's almost entirely light goes on a dark backing instead of white.
"""
from PIL import Image

DARK_BACKING = (17, 24, 39)
_MIN_CLEAR = 0.08    # under this much see-through it's a logo on its own box
_MIN_LIGHT_SHARE = 0.9   # of the logo itself, so a white badge with dark words isn't taken for white


def is_light_logo(image: Image.Image) -> bool:
    """True for a see-through logo that would vanish on white."""
    small = image.convert("RGBA")
    small.thumbnail((96, 96))
    raw = small.tobytes()
    pixels = [raw[i:i + 4] for i in range(0, len(raw), 4)]
    clear = sum(1 for p in pixels if p[3] < 32)
    solid = [p for p in pixels if p[3] > 128]
    if not solid or clear / len(pixels) < _MIN_CLEAR:
        return False
    light = sum(1 for p in solid if (0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]) / 255 >= 0.72)
    return light / len(solid) >= _MIN_LIGHT_SHARE


def on_dark_backing(image: Image.Image) -> Image.Image:
    """The logo unchanged on a dark square, for a spot that's white behind it."""
    rgba = image.convert("RGBA")
    side = round(max(rgba.size) * 1.24)
    back = Image.new("RGBA", (side, side), (*DARK_BACKING, 255))
    back.alpha_composite(rgba, ((side - rgba.width) // 2, (side - rgba.height) // 2))
    return back
