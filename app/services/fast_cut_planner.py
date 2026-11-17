"""Fast Cut - the beat-synced kind of AI Video (owner 11/14).

Same project, same photos, same choices as a Cinematic Video (what it's for, the look, the agent's words
and branding, which photos); the difference is the edit: many short shots cut exactly on a song's beats.
This file turns a project's saved settings (options.motion) into the shot list the video template
(frontend/motion-video/FastFilm.tsx, composition "FastCut") draws. No AI here - only the settings, what
the photo-sorting pass already said about every photo (its room, how striking it is, where its subject
sits, water / sky / fire) and the song's beat map. So being smart costs nothing extra.

How it edits (owner 11/14: "like someone did it"):
  - the front of the home alone under the opening title, then a walk-through in CHAPTERS - living spaces,
    bedrooms, baths, lower level, outdoors - then the front again for the closing;
  - every chapter opens on its most striking photo, held longer, with what the chapter is about on screen
    (a fact from the listing - "5,024 Square feet", "4 Bedrooms" - or a name - "Lower Level"), followed by
    the rest of that chapter's photos as quick shots; a special room that is really in a photo is named
    on its own shot ("Home Theater", "Pool");
  - the calmest outdoor photo carries the slow address shot;
  - a daytime front opens and a dusk front closes when the home has both;
  - with more photos than the song has room for, the least striking are left out.
"""
from __future__ import annotations

from typing import Any, Optional

FPS = 30
MIN_PHOTOS = 8

# Measured from the songs themselves (tempo, first beat, where the slow part sits) - made once on
# ElevenLabs Music and kept in frontend/public/motion/fastcut/. Beats: the opening photo, the beats before
# the slow address shot, the address shot, the beats after it, the closing.
SONGS: dict[str, dict[str, Any]] = {
    # "drops": the beats where the song hits - a cut always lands exactly there (with a flash)
    "skyline": {"music": "motion/fastcut/skyline.wav", "bpm": 90.0, "offset": 0.0, "open": 6, "pre": 10, "calm": 4, "post": 18, "final": 7, "words_at": 2, "drops": [24]},
    "golden": {"music": "motion/fastcut/golden.wav", "bpm": 100.0, "offset": 0.03, "open": 4, "pre": 28, "calm": 4, "post": 8, "final": 5.2, "drops": [16]},
    # the 1-minute version of Skyline (owner 11/15: "I like the last song way more"): the same song extended
    # on its own bar lines - groove x3, the quiet part (address shot), the drop part twice, the same ending
    "skyline60": {"music": "motion/fastcut/skyline60.wav", "bpm": 90.0, "offset": 0.0, "open": 6, "pre": 42, "calm": 4, "post": 30, "final": 7, "words_at": 2, "drops": [56, 68]},
}
# Four more songs at the same 90 bpm, each cut to the exact Skyline pattern (11/15) - same edit, different sound.
for _n in ('velvet', 'ivory', 'authority', 'marble', 'sunset'):
    SONGS[_n] = {**SONGS["skyline"], "music": f"motion/fastcut/{_n}.wav"}
LONG_SONG = "skyline60"
# The chapters, in walk-through order: (key, the rooms in it in the order they are shown, weight).
CHAPTERS: list[tuple[str, tuple[str, ...], float]] = [
    ("living", ("entry", "living", "family", "kitchen", "dining", "office", "detail"), 2.2),
    ("bedrooms", ("primary", "bedroom"), 1.2),
    ("baths", ("primary_bath", "bath"), 0.9),
    ("lower", ("basement", "bonus"), 1.0),
    ("outdoors", ("back", "yard", "pool", "view", "aerial"), 1.3),
]
# A special room is named only when the photo's own caption says it is one (never from the listing text).
FEATURES: list[tuple[tuple[str, ...], str]] = [
    (("theater", "theatre", "cinema", "screening"), "Home Theater"), (("gym", "fitness", "exercise"), "Home Gym"),
    (("wine",), "Wine Cellar"), (("sauna", "steam room"), "Sauna"), (("wet bar", " bar", "bar "), "Bar"),
    (("pool",), "Pool"), (("hot tub", "spa "), "Spa"), (("fire pit", "firepit"), "Fire Pit"), (("dock", "pier"), "Private Dock"),
    (("tennis", "pickleball", "sport court", "basketball"), "Sport Court"), (("game room", "billiard", "pool table"), "Game Room"),
]


def song_for(motion: dict) -> str:
    """The 1-minute song for a 45 / 60 second video; else the 30-second song the agent chose."""
    try:
        if int(motion.get("length") or 30) >= 45:
            return LONG_SONG
    except (TypeError, ValueError):
        pass
    k = motion.get("song") or "skyline"
    return k if k in SONGS and k != LONG_SONG else "skyline"


def photos_needed(song: str) -> int:
    """About how many photos fill every shot of this song."""
    sg = SONGS.get(song) or SONGS["skyline"]
    return int((sg["pre"] + sg["post"]) * 0.75) + 2


def _cl(v: Any, lo: float = 0.2, hi: float = 0.8) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return 0.5


def _feature(p: dict) -> Optional[str]:
    text = f" {p.get('label') or ''} {p.get('kicker') or ''} ".lower()
    if p.get("room") == "pool":
        return "Pool"
    for words, name in FEATURES:
        if any(w in text for w in words):
            return name
    return None


def _lead_words(key: str, facts: dict, lead: dict) -> Optional[dict]:
    """What a chapter's opening shot says."""
    if key == "living" and facts.get("sqft"):
        return {"value": str(facts["sqft"]), "label": "Square feet"}
    if key == "bedrooms" and facts.get("beds"):
        return {"value": str(facts["beds"]), "label": "Bedrooms"}
    if key == "baths" and facts.get("baths"):
        return {"value": str(facts["baths"]), "label": "Bathrooms"}
    if key == "lower":
        return {"value": _feature(lead) or "Lower Level", "label": ""}
    if key == "outdoors":
        if facts.get("acres"):
            return {"value": str(facts["acres"]), "label": "Acres"}
        return {"value": _feature(lead) or "Outdoors", "label": ""}
    return None


def _split_beats(total: int, chapters: list[dict]) -> None:
    """Share `total` beats between chapters by weight, never more than a chapter's photos can fill
    (a chapter of n photos fills at most 2n beats) and never fewer than 3."""
    for c in chapters:
        c["beats"] = 0
    left = total
    live = [c for c in chapters]
    while left > 0 and live:
        wsum = sum(c["w"] for c in live)
        gave = 0
        for c in sorted(live, key=lambda c: -c["w"]):
            want = max(1, round(left * c["w"] / wsum))
            room = 2 * len(c["photos"]) + 1 - c["beats"]
            g = max(0, min(want, room, left - gave))
            c["beats"] += g
            gave += g
        live = [c for c in live if c["beats"] < 2 * len(c["photos"]) + 1]
        if gave == 0:
            break
        left -= gave
    i = 0
    while left > 0 and chapters:   # every chapter is full: their shots simply hold a little longer, shared out
        chapters[i % len(chapters)]["beats"] += 1
        left -= 1
        i += 1


def _shot_beats(beats: int, n_photos: int) -> list[int]:
    """Shot lengths for one chapter: the opening shot is held like a title (3 beats when the chapter has
    room, owner 11/15: "the stat slides should be longer"), then quick 1-beat shots, with a 2-beat shot
    wherever there are fewer photos than beats."""
    lead = 3 if beats >= 4 else min(2, beats)   # owner 11/15 (twice): hold the stat title longer
    n = max(1, min(n_photos, beats - lead + 1))
    lens = [lead] + [1] * (n - 1)
    spare = beats - sum(lens)
    # every other quick shot first (so long and short alternate), then the ones in between
    order = list(range(len(lens) - 1, 0, -2)) + list(range(len(lens) - 2, 0, -2))
    for i in order:
        if spare <= 0:
            break
        lens[i] += 1
        spare -= 1
    i = len(lens) - 1
    while spare > 0 and len(lens) > 1:   # still beats to fill: the quick shots hold longer, in turn
        lens[i] += 1
        spare -= 1
        i = i - 1 if i > 1 else len(lens) - 1
    lens[0] += max(0, spare)
    return lens


def plan(motion: dict, fmt: str) -> dict:
    """options.motion -> the FastCut template's props. Raises ValueError when there are too few photos."""
    sg = SONGS[song_for(motion)]
    photos = [dict(p) for p in motion.get("photos") or [] if not p.get("hidden")]
    if len(photos) < MIN_PHOTOS:
        raise ValueError(f"A Fast Cut needs at least {MIN_PHOTOS} photos.")
    pool = {it.get("source"): it for it in motion.get("pool") or []}
    for i, p in enumerate(photos):
        it = pool.get(p.get("source")) or {}
        p["_score"] = float(it.get("score") or p.get("score") or 5)
        p["_i"] = i
        p["room"] = p.get("room") or it.get("room") or "detail"
        p["_dusk"] = bool(p.get("dusk", it.get("dusk")))
        p["_altx"] = p.get("altX", it.get("altX"))
        if p.get("toilet", it.get("toilet")):
            p["_score"] -= 3.0   # a toilet front and centre: last to be picked, first to be left out
    facts = motion.get("facts") or {}
    sections = motion.get("sections") or {}
    beat_s = 60.0 / sg["bpm"]
    at = lambda beat: round((sg["offset"] + beat * beat_s) * FPS) / FPS  # noqa: E731
    tall = fmt in ("9:16", "4:5")

    # ---- the front of the home: a daytime one opens, a dusk one closes (the same photo when there's one)
    fronts = sorted([p for p in photos if p["room"] == "front"], key=lambda p: -p["_score"])
    day = [p for p in fronts if not p["_dusk"]]
    opener = (day or fronts or [photos[0]])[0]
    dusk = [p for p in fronts if p["_dusk"] and p is not opener]
    closer = dusk[0] if dusk else opener
    used = {id(opener), id(closer)}

    # ---- the slow address shot: the calmest outdoor photo (water or open sky first)
    outside = [p for p in photos if id(p) not in used and p["room"] in ("view", "yard", "pool", "back", "aerial")]
    outside.sort(key=lambda p: (-(1 if p.get("water") else 0) - (0.5 if p.get("sky") else 0), -p["_score"]))
    calm = outside[0] if outside else None
    if calm is None:
        rest_best = sorted([p for p in photos if id(p) not in used], key=lambda p: -p["_score"])
        calm = rest_best[0] if rest_best else opener
    used.add(id(calm))

    # ---- chapters (a spare front photo joins the outdoors)
    chapters: list[dict] = []
    for key, rooms, w in CHAPTERS:
        ps = [p for p in photos if id(p) not in used and (p["room"] in rooms or (key == "outdoors" and p["room"] == "front"))]
        if not ps:
            continue
        # the living chapter opens on a real living space (not the foyer or a detail) when there is one
        main = [p for p in ps if p["room"] in ("living", "family", "kitchen")] if key == "living" else []
        lead = max(main or ps, key=lambda p: p["_score"])
        others = sorted([p for p in ps if p is not lead], key=lambda p: (rooms.index(p["room"]) if p["room"] in rooms else 99, -p["_score"]))
        chapters.append({"key": key, "photos": [lead] + others, "w": w * min(1.0, 0.4 + len(ps) / 5.0)})
    if sg["pre"] + sg["post"] <= 40 and len(chapters) > 3:
        keep_c = [chapters[0]] + sorted(chapters[1:], key=lambda c: -c["w"])[:2]
        dropped = [c for c in chapters if c not in keep_c]
        chapters = [c for c in chapters if c in keep_c]
        used |= {id(p) for c in dropped for p in c["photos"]}
    placed = {id(p) for c in chapters for p in c["photos"]}
    stray = [p for p in photos if id(p) not in used and id(p) not in placed]
    if stray:
        (chapters[0]["photos"] if chapters else stray).extend(stray if chapters else [])
    if not chapters:
        chapters = [{"key": "living", "photos": [p for p in photos if id(p) not in used] or [opener], "w": 1.0}]

    # ---- which chapters go before the address shot, which after (closest fit to the song)
    total = sg["pre"] + sg["post"]
    wsum = sum(c["w"] for c in chapters)
    acc, split, best = 0.0, 1, None
    for i, c in enumerate(chapters, 1):
        acc += c["w"] / wsum * total
        d = abs(acc - sg["pre"])
        if best is None or d < best:
            best, split = d, i
    before, after = chapters[:split], chapters[split:]
    if not after:
        before, after = chapters, []
    _split_beats(int(sg["pre"]) + (0 if after else int(sg["post"])), before)
    if after:
        _split_beats(int(sg["post"]), after)

    shots: list[dict] = []
    beat = 0.0

    def add(p: dict, length: float, **o: Any) -> dict:
        nonlocal beat
        kind = p.get("kind") or "interior"
        x = _cl(p.get("aimX") if p.get("aimX") is not None else p.get("focusX", 0.5))
        mv = p.get("move") or "auto"
        head_on = bool(o.pop("head_on", False))
        if o.pop("alt", False):   # the second angle: the far side of the photo from its subject
            x = _cl(p.get("_altx"))
            mv = "auto"
        if head_on:
            move, a, e = "push", x, x          # straight at the front door / the middle of the home
        else:
            move = mv if mv in ("push", "rise", "orbit") else "orbit" if (mv == "drone" or kind == "aerial") else "sweep"
            # from the side INTO the subject (never from the subject out to the side), starting on the side
            # with more room - or the side the agent chose
            side = -1 if mv == "slide_right" else 1 if mv == "slide_left" else (-1 if x >= 0.5 else 1)
            a = _cl(x + side * 0.16)
            if abs(a - x) < 0.08:
                a = _cl(x - side * 0.16)
            e = x
            if move in ("push", "rise"):
                a = e
        s = {"id": f"{len(shots)}-{p.get('id')}", "src": p.get("sharp") or p.get("clean") or p.get("source"), "depth": p.get("depth"),
             "sharpDepth": bool(p.get("sharpDepth")), "kind": kind, "start": at(beat), "end": at(beat + length), "move": move,
             "fromX": a, "toX": e, "focusX": e, "water": bool(p.get("water")), "sky": bool(p.get("sky")),
             "fire": bool(p.get("fire")), "fireBox": p.get("fireBox") if p.get("fire") else None, "lights": bool(p.get("lights"))}
        s.update(o)
        shots.append(s)
        beat += length
        return s

    add(opener, sg["open"], head_on=True, amt=1.0, ramp="steady", outFx="zoom")
    show_words = bool(sections.get("stats", True))

    def run(cs: list[dict], first_fx: str) -> None:
        for ci, c in enumerate(cs):
            others = c["photos"][1:]
            # Tall videos show about a third of each photo's width, so when a chapter has fewer photos than
            # beats its best photos come back once more as a SECOND ANGLE (the other side of the photo) -
            # the shots stay one beat long instead of each one dragging (owner 11/15).
            # ONLY photos where the photo pass found a second thing worth showing (altX) - never a blind crop.
            again = [dict(q, _alt=True) for q in sorted(others, key=lambda q: -q["_score"]) if q.get("_altx") is not None] if tall else []
            lens = _shot_beats(int(c["beats"]), len(c["photos"]) + len(again))
            n_first = min(len(others), max(0, len(lens) - 1))
            keep = sorted(sorted(others, key=lambda q: -q["_score"])[:n_first], key=others.index)
            keep += again[:max(0, len(lens) - 1 - len(keep))]
            for i, (p, L) in enumerate(zip([c["photos"][0]] + keep, lens)):
                o: dict[str, Any] = {}
                if p.get("_alt"):
                    o["alt"] = True
                if i == 0:
                    o["inFx"] = first_fx if ci == 0 else "whip" if ci % 2 else "zoom"
                    words = _lead_words(c["key"], facts, p) if show_words else None
                    if words:
                        o["fact"] = words
                elif show_words and L >= 1 and not p.get("_alt"):
                    name = _feature(p)
                    if name and c["key"] in ("lower", "outdoors") and not any((s.get("fact") or {}).get("value") == name for s in shots):
                        o["fact"] = {"value": name, "label": ""}
                if i == len(lens) - 1 and ci + 1 < len(cs):
                    o["outFx"] = "whip" if (ci + 1) % 2 else "zoom"   # hands over to the next chapter
                add(p, L, **o)

    run(before, "zoom")
    calm_at = beat
    calm_shot = add(calm, sg["calm"], ramp="steady", amt=0.9)
    if after:
        run(after, "zoom")
    shots[-1].update(ramp="launch", amt=1.2, outFx="zoom")
    final_at = beat
    add(closer, sg["final"], head_on=True, inFx="zoom", ramp="settle", amt=0.9)
    # A cut lands exactly on every drop of the song: the nearest cut moves onto it (never the opening,
    # the address shot or the closing), and that shot arrives with a flash.
    inner = [i for i in range(2, len(shots) - 1)]   # cuts INTO shots[i] that may move
    bstart = lambda s_: round(((s_["start"] if s_ is not shots[0] else 0) - sg["offset"]) / beat_s)  # noqa: E731
    for d in sg.get("drops") or []:
        cand = [i for i in inner if shots[i] is not calm_shot and shots[i - 1] is not calm_shot
                and bstart(shots[i - 1]) < d < round((shots[i]["end"] - sg["offset"]) / beat_s)]
        hit = [i for i in range(1, len(shots)) if bstart(shots[i]) == d]
        if hit:
            if shots[hit[0]] is not calm_shot and hit[0] != len(shots) - 1:
                shots[hit[0]]["inFx"] = "flash"
            continue
        if not cand:
            continue
        i = min(cand, key=lambda i: abs(bstart(shots[i]) - d))
        shots[i - 1]["end"] = at(d)
        shots[i]["start"] = at(d)
        shots[i]["inFx"] = "flash"
    shots[0]["start"] = 0
    duration = shots[-1]["end"]

    stats = " · ".join(x for x in [facts.get("beds") and f"{facts['beds']} Beds", facts.get("baths") and f"{facts['baths']} Baths",
                                   facts.get("sqft") and f"{facts['sqft']} Sq Ft"] if x)
    text: dict[str, Any] = {
        "address": facts.get("address") or "", "cityLine": facts.get("cityLine") or "", "price": facts.get("price") or "",
        "stats": stats if sections.get("stats", True) else "", "courtesy": facts.get("courtesy") or "",
        "titleAt": [at(calm_at + 0.4), at(calm_at + sg["calm"] - 0.4)], "endAt": at(final_at + sg.get("words_at", 0) + 0.5), "endTop": tall,
    }
    if sections.get("opening", True):
        text["openEnd"] = shots[0]["end"]
    accent = motion.get("accent") or "#E8C98A"
    brand = {"theme": motion.get("theme", "classic"), "accent": accent, "metal": motion.get("metal", "gold"),
             "situation": motion.get("situation", "just_listed"),
             "copy": {k: (motion.get("copy") or {}).get(k) or "" for k in ("kicker", "title", "tagline", "cta")},
             "facts": facts, "agent": motion.get("agent") or {"name": ""}, "sections": sections}
    return {"shots": shots, "format": fmt, "music": sg["music"], "duration": duration, "text": text, "accent": accent, "brand": brand}


def recommend_length(motion: dict) -> int:
    """30 or 60: a minute when the home has enough to fill it without rushing - at least 4 sections' worth
    of rooms and 30 usable photos to choose from; otherwise 30 seconds (3 sections)."""
    items = [p for p in (motion.get("pool") or motion.get("photos") or []) if (p.get("room") or "skip") != "skip" and not p.get("hidden")]
    rooms = {p.get("room") for p in items}
    sections = sum(1 for _, rs, _w in CHAPTERS if rooms & set(rs))
    return 60 if sections >= 4 and len(items) >= 36 else 30


def is_fast(motion: Optional[dict]) -> bool:
    return (motion or {}).get("kind") == "fastcut"
