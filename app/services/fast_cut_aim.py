"""Fast Cut's door finder (owner 11/14-15: the opening and closing shots must sit exactly on the front door,
or on the middle of the home when no door shows).

The room-sorting pass only knows roughly where a photo's subject is. Here the front-of-home photo is
shown to the AI with a ruler drawn across the top (0.0 = left edge, 1.0 = right edge, a thin line dropping
from every tenth) and it reads the door's position off the ruler - accurate to a couple of hundredths in
the tests of 11/14, where the rough figure was up to 0.14 out. One small call per front photo, once: the
answer is kept on the photo ("aimX"), so remakes and other shapes never ask again.
"""
from __future__ import annotations

import base64
import io
from typing import Optional

import httpx

from app.config.logging import get_logger

logger = get_logger(__name__)

_TOOL = {"name": "aim", "description": "Where to aim the camera.", "input_schema": {"type": "object", "required": ["door_visible", "door_x", "house_left_x", "house_right_x"], "properties": {
    "door_visible": {"type": "boolean", "description": "the main entry door (or main entrance) can be seen"},
    "door_x": {"type": "number", "description": "ruler reading at the centre of the main entry door, two decimals (0.00-1.00)"},
    "house_left_x": {"type": "number", "description": "ruler reading at the left edge of the main house"},
    "house_right_x": {"type": "number", "description": "ruler reading at the right edge of the main house"},
    "focal_x": {"type": "number", "description": "if no door is visible: ruler reading of the visual centre of the main house"},
}}}
_ASK = ("This listing photo has a yellow ruler across the top (0.0 = left edge, 1.0 = right edge) with thin yellow lines "
        "dropping from every 0.1 mark. A video camera will centre on the home's main entry door. Read off the ruler, as precisely as you can "
        "(two decimals, interpolate between the lines): the centre of the main entry door, and the left and right edges of the main house. "
        "If no entry door is visible (a back view, a drone view), say so and give as focal_x the visual centre of the main house - "
        "the spot a photographer would centre the frame on. Call aim.")


def _ruled(data: bytes) -> str:
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((1400, 1400))
    w, h = im.size
    band = 46
    canvas = Image.new("RGB", (w, h + band), (0, 0, 0))
    canvas.paste(im, (0, band))
    d = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except Exception:  # noqa: BLE001
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 22)
        except Exception:  # noqa: BLE001
            font = ImageFont.load_default()
    for i in range(0, 101, 5):
        x = round(w * i / 100)
        major = i % 10 == 0
        d.line([(x, band - (18 if major else 9)), (x, band)], fill=(255, 255, 0), width=2)
        if major:
            d.line([(x, band), (x, h + band)], fill=(255, 255, 0), width=1)
            d.text((min(w - 40, max(2, x - 16)), 2), "%.1f" % (i / 100), fill=(255, 255, 0), font=font)
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


async def find(url: str, model: str, slow_cap: Optional[float] = None) -> Optional[tuple[float, int, int]]:
    """(where to aim 0-1, input tokens, output tokens) for one front-of-home photo; None if it couldn't be read."""
    from app.services import cma_claude
    from app.services.ai.ai_copywrite_service import _client
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _ruled(r.content)}}, {"type": "text", "text": _ASK}]
        resp = await cma_claude.create(_client(), slow_cap=slow_cap, model=model, max_tokens=600, tools=[_TOOL], tool_choice={"type": "tool", "name": "aim"},
                                       messages=[{"role": "user", "content": content}])
        out = next((b.input for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if not out:
            return None
        if out.get("door_visible"):
            x = float(out["door_x"])
        else:
            x = float(out.get("focal_x", (float(out["house_left_x"]) + float(out["house_right_x"])) / 2))
        return max(0.0, min(1.0, x)), int(resp.usage.input_tokens or 0), int(resp.usage.output_tokens or 0)
    except Exception:  # noqa: BLE001 - the rough figure is used instead
        logger.warning("fast cut door finder failed", exc_info=True)
        return None
