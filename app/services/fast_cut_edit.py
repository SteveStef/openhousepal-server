"""Fast Cut's AI editor (owner-approved recipe, 11/14-11/16).

A Fast Cut is edited the way a person would: ONE look at every photo decides which photos are used, in what
order, and how the camera moves on each (this file); the planner (fast_cut_recipe.py) then lays those picks on
the song's beats. Nothing here draws the video.

The passes, all on the agent's usage (the caller records the tokens):
  1. the editor   - every photo at once: kind, a short caption, how striking, where the frame starts and ends,
                    the move, day / dusk, sky / water / flames, skip; and the reel: opening run, the calm address
                    shot, the second-half run, the closing photo.
  2. the subject  - each usable photo with a ruler drawn across it: exactly where its subject is, so every pan
                    ENDS dead centre on it (the editor's own figure was up to 0.3 of the photo out - a cinema
                    screen, a bed, an island missed).
  3. flames       - a tight box round the flames of every photo the editor saw fire in (they flicker).
  4. windows      - only when the closing photo is a daytime front: its windows, so they glow as the ending
                    turns to dusk. Boxes are tightened onto the glass (shutters / wall trimmed off).

The result is kept on the project (options.motion.fast) keyed by the photos' own ids, so remakes, the other
shape and the other length never ask again; it is redone only when the set of photos changes.
"""
from __future__ import annotations

import asyncio
import base64
import io
import statistics
import time
from typing import Any, Optional

import httpx

from app.config.logging import get_logger

logger = get_logger(__name__)

VERSION = 2   # 2: looks made by the older model are redone (11/16)
KINDS = ["exterior", "aerial", "pool", "outdoor", "living", "kitchen", "dining", "bedroom", "bathroom", "office", "interior", "detail"]

_EDIT_TOOL = {
    "name": "edit_reel",
    "description": "The edit: a camera plan for every photo, and which photos go where in the reel.",
    "input_schema": {"type": "object", "required": ["photos", "open", "calm", "drop", "final"], "properties": {
        "photos": {"type": "array", "items": {"type": "object", "required": ["n", "kind", "what", "score", "start_x", "end_x", "move", "light"], "properties": {
            "n": {"type": "integer"},
            "kind": {"type": "string", "enum": KINDS},
            "what": {"type": "string", "description": "room / view and its subject, 3-6 words"},
            "score": {"type": "number", "description": "1-5: how striking it is in a tall video"},
            "skip": {"type": "boolean", "description": "true for floor plans, maps, site plans, logos, photos with drawn-on text or lines, near-duplicates of a better photo, empty or unattractive rooms"},
            "light": {"type": "string", "enum": ["day", "dusk", "inside"]},
            "start_x": {"type": "number"}, "end_x": {"type": "number"},
            "move": {"type": "string", "enum": ["pan", "push", "rise", "orbit"]},
            "sky": {"type": "boolean"}, "water": {"type": "boolean"}, "fire": {"type": "boolean"},
        }}},
        "open": {"type": "array", "items": {"type": "integer"}, "description": "12 photo numbers, in order"},
        "calm": {"type": "integer"},
        "drop": {"type": "array", "items": {"type": "integer"}, "description": "16 photo numbers, in order"},
        "final": {"type": "integer"},
    }},
}
_EDIT_RULES = """You are an expert real-estate video editor cutting a fast, beat-synced VERTICAL (9:16) reel from these listing photos.

PART 1 - every photo. Each photo is a wide landscape picture, but the tall frame shows only about ONE THIRD of its width at a time
(its full height). The camera can turn left or right across the photo during the shot. For every photo give:
- kind, what, score, light, sky (open sky visible), water (pool / pond / river visible), fire (REAL FLAMES visible anywhere in the
  photo, even small or in the background: a lit fireplace, fire pit, fire table, torch), skip.
- end_x: where the centre of the tall frame should END - the money shot. The tall third of the photo around end_x must be a balanced,
  attractive composition with the subject comfortably inside. NEVER end on a blank wall, a door edge, half a piece of furniture,
  a dark corner or empty lawn / sky.
- start_x: where the frame starts. The move should REVEAL: start on a nearby interesting area and travel toward the subject so the
  shot gets better as it goes. Never start on the subject and drift away. The start must also be a decent frame.
- move: "pan" (frame travels sideways start_x -> end_x, typical travel 0.15-0.35; use when there is good content across half the
  photo), "push" (one clear centred subject - symmetrical facade, bed, fireplace, tub, hallway: straight in, start_x ~= end_x),
  "rise" (tall subjects - two-storey room, staircase, tall facade: start_x ~= end_x), "orbit" (aerial / drone and wide landscape
  views: slow arc, small travel ending on the house).
- Bathrooms: only use a bathroom that is genuinely beautiful, and frame it on the vanity, tub, shower or window - the tall frame
  must never be centred on a toilet. Skip plain powder rooms where the toilet is the main thing in view.
- start_x and end_x stay between 0.2 and 0.8 (the frame is about 0.35 of the photo wide).

PART 2 - the reel. Never use a skipped photo. Use each photo at most once across open, calm, drop and final.
(burst may reuse photos only if you run out). Prefer high scores and variety (don't put near-identical angles side by side).
- open: 12 photos for the opening run, in order. Start with the best DAYTIME front exterior, then a second exterior or aerial,
  then walk inside like a tour: entry, main living spaces, kitchen, dining, then the primary suite. Strong, clear rooms only.
- calm: 1 serene, beautiful, uncluttered photo for a slow 3-second shot with the address over it (a view, the pool, a terrace,
  a back exterior). Daytime.
- drop: 16 photos for the high-energy second half, in order. Open with the single most jaw-dropping photo not used yet
  (pool, a huge room, a view), then lifestyle and wow spaces (bar, theatre, gym, wine room, terraces, outdoor living), remaining
  great rooms, and exteriors / aerials. Keep any dusk or night exteriors for the END of this list.
- final: the closing hero held for 3 seconds with the price: the best twilight / dusk front exterior if there is one, otherwise
  the best front exterior not yet used (it may repeat the very first photo only if nothing else works).

Call edit_reel."""

_SUBJ_TOOL = {"name": "subjects", "description": "The subject of each photo.", "input_schema": {"type": "object", "required": ["photos"], "properties": {"photos": {"type": "array", "items": {
    "type": "object", "required": ["photo", "subject", "x"], "properties": {
        "photo": {"type": "integer"},
        "subject": {"type": "string", "description": "the thing this photo is showing off, 2-4 words"},
        "x": {"type": "number", "description": "ruler reading at the CENTRE of that subject, two decimals (0.00-1.00)"}}}}}}}
_SUBJ_ASK = ("Each listing photo above has a yellow ruler across the top (0.0 = left edge, 1.0 = right edge) with thin yellow lines dropping from every 0.1 mark. "
             "A tall video frame, about a third of the photo wide, pans across each photo and must END perfectly centred on what the photo is showing off, so the shot "
             "looks intentional. For each photo name that subject and read the ruler at its centre, as precisely as you can (two decimals, interpolate between the lines). "
             "The subject is the feature a photographer framed the picture around - usually the largest or most central one: the kitchen island with the range wall behind it, "
             "the fireplace, the bed, the dining table and its chandelier, the cinema screen, the bar, the soaking tub or vanity (never a toilet), the staircase, the pool, "
             "the front door of the house, the big window with the view. Where two features share the photo, pick the one that would make the better tall picture and centre on it, "
             "not on the gap between them. Call subjects with one entry per photo.")

_FIRE_TOOL = {"name": "fires", "description": "Lit fires in the photos.", "input_schema": {"type": "object", "required": ["fires"], "properties": {"fires": {"type": "array", "items": {
    "type": "object", "required": ["photo", "left", "top", "right", "bottom"], "properties": {"photo": {"type": "integer"},
        "left": {"type": "number"}, "top": {"type": "number"}, "right": {"type": "number"}, "bottom": {"type": "number"}}}}}}}
_FIRE_ASK = ("For every photo that shows real FLAMES - a lit fireplace (indoors or outdoors), fire pit, fire table, fire bowl, torch or lit gas lantern - "
             "give a tight box around the flames themselves as fractions of the photo: left and right from 0 (left edge) to 1, top and bottom from 0 (top edge) to 1. "
             "Look carefully, also at small fires in the background or seen through a doorway. One entry per separate fire (a photo may have two). "
             "Not an unlit fireplace, not a TV, not a lamp. List nothing for photos with no visible flames. Call fires.")

_WIN_TOOL = {"name": "windows", "description": "Window glass on the house.", "input_schema": {"type": "object", "required": ["windows"], "properties": {"windows": {"type": "array", "items": {
    "type": "object", "required": ["left", "top", "right", "bottom"], "properties": {"left": {"type": "number"}, "top": {"type": "number"}, "right": {"type": "number"}, "bottom": {"type": "number"}}}}}}}
_WIN_ASK = ("This is part of the front of a house in daylight. List every window of the house whose glass is clearly visible (at most 16, the largest first): "
            "a TIGHT box around the glass panes of each window - inside the window frame, never the shutters beside it, the trim or the wall - as fractions of THIS picture: "
            "left and right from 0 (left edge) to 1, top and bottom from 0 (top edge) to 1. Never include a door (only a glass transom or sidelight next to it). "
            "Skip garage doors without glass, windows mostly hidden by trees or bushes, and anything you are not sure is a window. Call windows.")


class _Tokens:
    def __init__(self) -> None:
        self.tin = 0
        self.tout = 0

    def add(self, resp: Any) -> None:
        self.tin += int(getattr(resp.usage, "input_tokens", 0) or 0)
        self.tout += int(getattr(resp.usage, "output_tokens", 0) or 0)


def _b64(im, size: int, quality: int = 82) -> str:
    t = im.copy()
    t.thumbnail((size, size))
    buf = io.BytesIO()
    t.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def _img(data: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _ruled(im, size: int = 1000) -> str:
    """The photo with a yellow ruler across the top (0.0 left edge .. 1.0 right edge), as base64 JPEG."""
    from PIL import Image, ImageDraw, ImageFont
    t = im.copy()
    t.thumbnail((size, size))
    w, h = t.size
    band = 36
    canvas = Image.new("RGB", (w, h + band), (0, 0, 0))
    canvas.paste(t, (0, band))
    d = ImageDraw.Draw(canvas)
    font = None
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(name, 18)
            break
        except Exception:  # noqa: BLE001
            continue
    font = font or ImageFont.load_default()
    for i in range(0, 101, 5):
        x = round(w * i / 100)
        major = i % 10 == 0
        d.line([(x, band - (14 if major else 7)), (x, band)], fill=(255, 255, 0), width=2)
        if major:
            d.line([(x, band), (x, h + band)], fill=(255, 255, 0), width=1)
            d.text((min(w - 34, max(2, x - 13)), 2), "%.1f" % (i / 100), fill=(255, 255, 0), font=font)
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode()


async def _ask(model: str, tool: dict, content: list, max_tokens: int, tok: _Tokens, until: Optional[float] = None) -> dict:
    """until: "Save usage" - the request waits in the half-price queue until then (time.monotonic), after which it is
    sent the normal way. None = the normal way."""
    from app.services import cma_claude
    from app.services.ai.ai_copywrite_service import _client
    left = (until - time.monotonic()) if until else 0
    resp = await cma_claude.create(_client(), slow_cap=left if left > 45 else None, model=model, max_tokens=max_tokens, tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
                                   messages=[{"role": "user", "content": content}])
    tok.add(resp)
    return next((b.input for b in resp.content if getattr(b, "type", None) == "tool_use"), None) or {}


def _cl01(v: Any, d: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return d


# ------------------------------------------------------------------ windows: tighten the AI's boxes onto the glass

def _snap_windows(im, boxes: list[list[float]]) -> list[list[float]]:
    """A window is busy (glazing bars, reflections); the shutters and wall beside it are flat. Flat strips at a
    box's edges move in, edge rows / columns of the wall's own brightness move in, and a box with nothing busy
    in it - or a tall narrow strip (a shutter) - is dropped. Plain Python on small crops (no numpy here)."""
    g = im.convert("L")
    W, H = g.size
    px = g.load()

    def col_std(x: int, y0: int, y1: int) -> float:
        v = [px[x, y] / 255 for y in range(y0, y1)]
        return statistics.pstdev(v) if len(v) > 1 else 0.0

    def med(vals: list[float]) -> float:
        return statistics.median(vals) if vals else 0.5

    out: list[list[float]] = []
    for l, t, r, bt in boxes:
        x0, x1, y0, y1 = int(l * W), int(r * W), int(t * H), int(bt * H)
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        if x1 - x0 < 6 or y1 - y0 < 6:
            continue
        step = max(1, (y1 - y0) // 60)
        cs = [statistics.pstdev([px[x, y] / 255 for y in range(y0, y1, step)]) for x in range(x0, x1)]
        cs = [sum(cs[max(0, i - 1):i + 2]) / len(cs[max(0, i - 1):i + 2]) for i in range(len(cs))]
        lim = int((x1 - x0) * 0.45)
        a = b = 0
        while a < lim and cs[a] < 0.06:
            a += 1
        while b < lim and cs[-1 - b] < 0.06:
            b += 1
        x0, x1 = x0 + a, x1 - b
        mx, my = max(3, int((x1 - x0) * 0.3)), max(3, int((y1 - y0) * 0.25))
        ring = [px[x, y] / 255 for y in range(max(0, y0 - my), y0) for x in range(x0, x1, 2)]
        ring += [px[x, y] / 255 for y in range(y0, y1, 2) for x in range(max(0, x0 - mx - mx // 2), max(0, x0 - mx // 2))]
        ring += [px[x, y] / 255 for y in range(y0, y1, 2) for x in range(min(W, x1 + mx // 2), min(W, x1 + mx + mx // 2))]
        if ring:
            wall = med(ring)
            isw = [[abs(px[x, y] / 255 - wall) < 0.09 for x in range(x0, x1)] for y in range(y0, y1)]
            nw, nh = x1 - x0, y1 - y0
            cols = [sum(isw[j][i] for j in range(nh)) / nh for i in range(nw)]
            rows = [sum(isw[j]) / nw for j in range(nh)]
            qx, qy = max(1, nw // 4), max(1, nh // 4)
            a = b = c = d = 0
            while a < qx and cols[a] > 0.6:
                a += 1
            while b < qx and cols[-1 - b] > 0.6:
                b += 1
            while c < qy and rows[c] > 0.6:
                c += 1
            while d < qy and rows[-1 - d] > 0.6:
                d += 1
            x0, x1, y0, y1 = x0 + a, x1 - b, y0 + c, y1 - d
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        inner = [px[x, y] / 255 for y in range(y0, y1, max(1, (y1 - y0) // 50)) for x in range(x0, x1, max(1, (x1 - x0) // 50))]
        if len(inner) < 4 or statistics.pstdev(inner) < 0.085:
            continue   # flat: a shutter or bare wall
        out.append([round(x0 / W, 4), round(y0 / H, 4), round(x1 / W, 4), round(y1 / H, 4)])
    if out:
        ma = med([(o[2] - o[0]) * (o[3] - o[1]) for o in out])
        out = [o for o in out if (o[2] - o[0]) * (o[3] - o[1]) >= 0.25 * ma]
    if len(out) >= 4:
        mw, mh = med([o[2] - o[0] for o in out]), med([o[3] - o[1] for o in out])
        out = [o for o in out if not ((o[2] - o[0]) < 0.62 * mw and (o[3] - o[1]) > 0.8 * mh)]
    return out[:24]


def wall_gate(im, boxes: list[list[float]]) -> None:
    return None   # (the glow is an even light inside the boxes; no per-pixel gate - it made blotches, 11/16)


async def _windows(im, model: str, tok: _Tokens, until: Optional[float] = None) -> list[list[float]]:
    W, H = im.size
    spans = [(0.0, 0.56), (0.44, 1.0)]

    async def look(a: float, b: float) -> list[list[float]]:
        out = await _ask(model, _WIN_TOOL, [_img(_b64(im.crop((int(a * W), 0, int(b * W), H)), 1568, 88)), {"type": "text", "text": _WIN_ASK}], 2500, tok, until)
        return [[w["left"], w["top"], w["right"], w["bottom"]] for w in out.get("windows") or []
                if all(isinstance(w.get(k), (int, float)) for k in ("left", "top", "right", "bottom")) and w["right"] > w["left"] and w["bottom"] > w["top"]]

    def overlap(p: list[float], q: list[float]) -> float:
        w = max(0.0, min(p[2], q[2]) - max(p[0], q[0]))
        h = max(0.0, min(p[3], q[3]) - max(p[1], q[1]))
        return w * h / max(1e-9, min((p[2] - p[0]) * (p[3] - p[1]), (q[2] - q[0]) * (q[3] - q[1])))

    res = await asyncio.gather(*[look(a, b) for a, b in spans])
    boxes: list[list[float]] = []
    for (a, b), bs in zip(spans, res):
        for l, t, r, bt in bs:
            if (l < 0.02 and a > 0) or (r > 0.98 and b < 1):
                continue   # cut by the seam: the other half has it whole
            bx = [a + l * (b - a), t, a + r * (b - a), bt]
            if all(overlap(bx, o) < 0.35 for o in boxes):
                boxes.append(bx)
    return await asyncio.to_thread(_snap_windows, im, boxes)


# ------------------------------------------------------------------ the whole pass

async def run(photos: list[dict], listing_text: str, model: str, slow_seconds: Optional[float] = None) -> tuple[Optional[dict], int, int]:
    """photos: [{"id", "url"}] in the project's order (at most 110). -> (the edit, tokens in, tokens out).
    The edit: {"v", "ids", "photos": {id: {kind, what, score, start_x, end_x, move, light, sky, water, fire, skip, x}},
    "open": [ids], "calm": id, "drop": [ids], "final": id, "fires": {id: box}, "windows": {id: [boxes]}}.
    None when the photos could not be read at all. A later pass that fails only leaves its part out."""
    from PIL import Image
    tok = _Tokens()
    # slow_seconds ("Save usage"): every request goes through the half-price queue; the whole look has that long
    # before whatever is still waiting is sent the normal way. Tokens of queued answers come back halved.
    until = (time.monotonic() + float(slow_seconds)) if slow_seconds else None
    photos = [p for p in photos if p.get("id") and p.get("url")][:110]
    sem = asyncio.Semaphore(8)

    async def get(client: httpx.AsyncClient, p: dict):
        try:
            async with sem:
                r = await client.get(p["url"])
                r.raise_for_status()
            return await asyncio.to_thread(lambda: Image.open(io.BytesIO(r.content)).convert("RGB"))
        except Exception:  # noqa: BLE001
            return None

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        ims = await asyncio.gather(*[get(client, p) for p in photos])
    got = [(p, im) for p, im in zip(photos, ims) if im is not None]
    if len(got) < 4:
        return None, 0, 0
    ids = [p["id"] for p, _ in got]
    imgs = {p["id"]: im for p, im in got}

    # 1) the editor
    content: list = [{"type": "text", "text": "Listing: %s\n\nIts photos follow, numbered." % (listing_text or "")[:1700]}]
    for n, (p, im) in enumerate(got):
        content.append({"type": "text", "text": f"Photo {n}:"})
        content.append(_img(_b64(im, 640, 80)))
    content.append({"type": "text", "text": _EDIT_RULES})
    raw = await _ask(model, _EDIT_TOOL, content, 14000, tok, until)
    ok = lambda n: isinstance(n, int) and 0 <= n < len(ids)  # noqa: E731
    info: dict[str, dict] = {}
    for q in raw.get("photos") or []:
        if not ok(q.get("n")):
            continue
        info[ids[q["n"]]] = {
            "kind": q.get("kind") if q.get("kind") in KINDS else "interior", "what": str(q.get("what") or "")[:80],
            "score": max(1.0, min(5.0, float(q.get("score") or 3))), "start_x": _cl01(q.get("start_x")), "end_x": _cl01(q.get("end_x")),
            "move": q.get("move") if q.get("move") in ("pan", "push", "rise", "orbit") else "push", "light": q.get("light") if q.get("light") in ("day", "dusk", "inside") else "inside",
            "sky": bool(q.get("sky")), "water": bool(q.get("water")), "fire": bool(q.get("fire")), "skip": bool(q.get("skip")),
        }
    if len(info) < 4:
        return None, tok.tin, tok.tout
    pick = lambda v: [ids[n] for n in (v or []) if ok(n) and ids[n] in info and not info[ids[n]]["skip"]]  # noqa: E731
    one = lambda v: ids[v] if ok(v) and ids[v] in info else None  # noqa: E731
    edit: dict[str, Any] = {"v": VERSION, "ids": ids, "photos": info, "open": list(dict.fromkeys(pick(raw.get("open")))), "calm": one(raw.get("calm")),
                            "drop": list(dict.fromkeys(pick(raw.get("drop")))), "final": one(raw.get("final")), "fires": {}, "windows": {}}
    usable = [i for i in ids if i in info and not info[i]["skip"]]
    num = {i: n for n, i in enumerate(ids)}
    sem3 = asyncio.Semaphore(3 if until is None else 32)   # queued requests wait side by side (three at a time made a Save usage look 6 minutes)

    # 2) where each usable photo's subject is (ruler)
    async def subjects(group: list[str]) -> None:
        c: list = []
        for i in group:
            c.append({"type": "text", "text": "Photo %d (%s):" % (num[i], info[i]["what"])})
            c.append(_img(await asyncio.to_thread(_ruled, imgs[i])))
        c.append({"type": "text", "text": _SUBJ_ASK})
        try:
            async with sem3:
                out = await _ask(model, _SUBJ_TOOL, c, 2000, tok, until)
            for q in out.get("photos") or []:
                if ok(q.get("photo")) and ids[q["photo"]] in group and isinstance(q.get("x"), (int, float)):
                    info[ids[q["photo"]]]["x"] = round(_cl01(q["x"]), 3)
                    info[ids[q["photo"]]]["subject"] = str(q.get("subject") or "")[:60]
        except Exception:  # noqa: BLE001 - the editor's own figure is used for these
            logger.warning("fast cut subject pass failed for a group", exc_info=True)

    # 3) a box round the flames of every photo the editor saw fire in
    async def fires(group: list[str]) -> None:
        c: list = []
        for i in group:
            c.append({"type": "text", "text": f"Photo {num[i]}:"})
            c.append(_img(_b64(imgs[i], 1000)))
        c.append({"type": "text", "text": _FIRE_ASK})
        try:
            async with sem3:
                out = await _ask(model, _FIRE_TOOL, c, 1500, tok, until)
            fs = [f for f in out.get("fires") or [] if ok(f.get("photo")) and ids[f["photo"]] in group
                  and all(isinstance(f.get(k), (int, float)) for k in ("left", "top", "right", "bottom")) and f["right"] > f["left"] and f["bottom"] > f["top"]]
            for f in sorted(fs, key=lambda f: -(f["right"] - f["left"]) * (f["bottom"] - f["top"])):
                edit["fires"].setdefault(ids[f["photo"]], [round(_cl01(f[k]), 4) for k in ("left", "top", "right", "bottom")])
        except Exception:  # noqa: BLE001
            logger.warning("fast cut flame pass failed for a group", exc_info=True)

    # 4) the closing photo's windows, when it is a daytime outside view (it turns to dusk)
    async def windows() -> None:
        f = edit["final"] or (edit["open"][0] if edit["open"] else None)
        cands = [i for i in dict.fromkeys([f] + edit["open"][:1]) if i and info[i]["light"] == "day" and info[i]["kind"] in ("exterior", "aerial", "pool")]
        for i in cands[:1]:
            try:
                bs = await _windows(imgs[i], model, tok, until)
                if bs:
                    edit["windows"][i] = bs
            except Exception:  # noqa: BLE001
                logger.warning("fast cut window pass failed", exc_info=True)

    lit = [i for i in usable if info[i]["fire"]]
    await asyncio.gather(*[subjects(usable[k:k + 8]) for k in range(0, len(usable), 8)], *[fires(lit[k:k + 8]) for k in range(0, len(lit), 8)], windows())
    return edit, tok.tin, tok.tout


def covers(edit: Optional[dict], photo_ids: list[str]) -> bool:
    """True when this edit already looked at every one of these photos (taking photos OUT never needs a new look;
    adding one does)."""
    return bool(edit) and edit.get("v") == VERSION and bool(edit.get("photos")) and set(photo_ids) <= set(edit.get("ids") or [])
