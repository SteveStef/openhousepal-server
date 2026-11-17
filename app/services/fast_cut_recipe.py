"""Fast Cut - the owner-approved edit (reviewed video by video, 11/14-11/16).

fast_cut_edit.py (one AI look at every photo) says which photos are used, in what order, and where each one's
subject is. This file lays those picks on the song's beats and turns them into the shot list the video
template draws (frontend/motion-video/FastFilm.tsx, composition "FastCut"). No AI here, no cost.

The rules, each one asked for by the owner while reviewing test videos:
  - the beat pattern of the first song he approved (Skyline), shared by every 90 bpm song: an opening run that
    starts on the front of the home held under the title, a slow shot with the address, four quick shots, the
    drop, and the closing photo with the price. One minute = that same pattern played through twice.
  - every pan ENDS dead centre on the photo's subject and starts away from it; a one-beat shot barely travels.
  - stats ("11,042 Square feet") only on shots long enough to read (3 beats), never two close together, and that
    shot moves gently. Beds and baths is one stat, always over the nicest bedroom or bathroom.
  - never the same photo, two near-identical angles, or two drone views back to back.
  - a lit fire flickers, and the shot lands with it in view when it is near what the shot is about.
  - a daytime closing photo turns to dusk and its windows glow; a real dusk photo is left as it is.
  - no shade behind the words; the title and the ending are a self-drawing nameplate.
The agent's own choices come through unchanged: what it's for, the look and colours, branding on or off, stats
on or off, which photos are in, and (fast.order / fast.stats) any change they made to the order or the stats.
"""
from __future__ import annotations

from typing import Any, Optional

FPS = 30
MIN_PHOTOS = 8
BPM = 90.0

# shot lengths in beats, section by section (see the module docstring)
SHORT: list[tuple[str, list[int]]] = [("hook", [6, 1, 1, 2, 1, 1, 2, 2]), ("calm", [4]), ("quick", [1, 1, 1, 1]),
                                      ("drop", [2, 1, 1, 2, 2, 2, 1, 1, 1, 1, 2]), ("final", [5])]
LONG: list[tuple[str, list[int]]] = [("hook", [6, 1, 1, 2, 1, 1, 2, 2]), ("calm", [4]), ("quick", [1, 1, 1, 1]), ("drop", [2, 1, 1, 2, 2, 2, 1, 1, 1, 1, 2]),
                                     ("run", [2, 1, 1, 2, 1, 1, 2, 2, 2, 2]), ("calm2", [4]), ("quick", [1, 1, 1, 1]), ("run", [2, 1, 1, 2, 2, 2, 1, 1, 1, 1, 2]),
                                     ("final", [5])]
SONGS = ("skyline", "authority", "velvet", "ivory", "marble", "sunset")   # every one cut to the same pattern; <name>60.wav is its 1-minute version
KICKERS = {"just_listed": "Just Listed", "coming_soon": "Coming Soon", "open_house": "Open House", "price_improved": "Price Improved",
           "under_contract": "Under Contract", "just_sold": "Just Sold", "for_rent": "For Rent", "back_on_market": "Back on Market"}
_GROUP = {"bedroom": "bedbath", "bathroom": "bedbath", "living": "living", "kitchen": "living", "dining": "living",
          "exterior": "exterior", "aerial": "exterior", "outdoor": "exterior", "pool": "exterior"}


def is_long(motion: dict) -> bool:
    try:
        return int(motion.get("length") or 30) >= 45
    except (TypeError, ValueError):
        return False


def song_of(motion: dict) -> str:
    k = motion.get("song") or "skyline"
    return k if k in SONGS else "skyline"


def photos_needed(long: bool) -> int:
    """How many different photos fill every shot (the closing photo may be the opening one again)."""
    return sum(len(l) for s, l in (LONG if long else SHORT) if s != "final")


def seconds(long: bool) -> float:
    return sum(sum(l) for _, l in (LONG if long else SHORT)) * 60.0 / BPM


def _cl(v: Any, lo: float = 0.2, hi: float = 0.8) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return 0.5


def plan(motion: dict, fmt: str) -> dict:
    """options.motion (with motion.fast.edit) -> the FastCut template's props. ValueError when it can't be made."""
    fast = motion.get("fast") or {}
    edit = fast.get("edit") or {}
    einfo: dict[str, dict] = edit.get("photos") or {}
    photos = {p["id"]: p for p in motion.get("photos") or [] if not p.get("hidden") and p.get("id") in einfo}
    info = {i: einfo[i] for i in photos}
    if len(photos) < MIN_PHOTOS:
        raise ValueError(f"A Fast Cut needs at least {MIN_PHOTOS} photos.")
    long = is_long(motion)
    order = LONG if long else SHORT
    sections = motion.get("sections") or {}
    facts_in = motion.get("facts") or {}
    b = 60.0 / BPM
    at = lambda beat: round(beat * b * FPS) / FPS  # noqa: E731
    score = lambda n: float((info.get(n) or {}).get("score") or 0)  # noqa: E731
    usable = lambda n: n in info and not info[n].get("skip")  # noqa: E731
    outside = lambda n: (info.get(n) or {}).get("kind") in ("exterior", "aerial", "pool")  # noqa: E731

    opens = [n for n in edit.get("open") or [] if usable(n)]
    drops = [n for n in edit.get("drop") or [] if usable(n)]
    calm = edit.get("calm") if usable(edit.get("calm")) else None
    final = edit.get("final") if edit.get("final") in info else None
    if not opens:
        opens = sorted([n for n in info if usable(n)], key=lambda n: (not outside(n), -score(n)))[:12]
    if not opens:
        raise ValueError("None of these photos can be used for a Fast Cut.")
    # the closing shot (with the price) is always the outside of the home; an indoor pick falls back to the opener
    if not (final and outside(final)):
        final = opens[0]
    used: set[str] = set()
    given: dict[str, int] = {}   # how many times a photo has been handed out again once all are used
    last_n: list[Optional[str]] = [None]
    words = lambda n: {w for w in str((info.get(n) or {}).get("what") or "").lower().replace("-", " ").split() if len(w) > 3}  # noqa: E731

    def alike(x: Optional[str], y: Optional[str]) -> bool:
        if x is None or y is None:
            return False
        if x == y:
            return True
        kx, ky = (info.get(x) or {}).get("kind"), (info.get(y) or {}).get("kind")
        return kx == ky and (kx == "aerial" or len(words(x) & words(y)) >= 2)   # two drone views in a row look the same

    def apart(seq: list[str]) -> list[str]:
        clash = lambda s, k: (k > 0 and alike(s[k], s[k - 1])) or (k + 1 < len(s) and alike(s[k], s[k + 1]))  # noqa: E731
        for i in range(1, len(seq)):
            if not alike(seq[i], seq[i - 1]):
                continue
            for j in sorted(range(1, len(seq)), key=lambda j: abs(j - i)):
                if j in (i, i - 1):
                    continue
                t = list(seq)
                t[i], t[j] = t[j], t[i]
                if not clash(t, i) and not clash(t, j):
                    seq[:] = t
                    break
        return seq

    def take(pool: list[str], also: tuple = ()) -> str:
        """Next unused photo from the AI's list; then any unused good photo; then (last resort) a repeat."""
        for fussy in (True, False):          # first choice: not a near-copy of the photo just shown
            for src in (pool,) + tuple(also):
                for n in src:
                    if n not in used and n not in (final, calm) and not (fussy and alike(n, last_n[0])):
                        used.add(n)
                        return n
            rest = sorted([n for n in info if usable(n) and n not in used and n not in (final, calm) and not (fussy and alike(n, last_n[0]))], key=lambda n: -score(n))
            if rest:
                used.add(rest[0])
                return rest[0]
        # every photo is used: show one again - the one shown the fewest times so far (the most striking first),
        # never the photo just shown or a near-copy of it
        again = [n for n in info if usable(n) and n not in (final, calm)] or list(opens)
        pick = min(again, key=lambda n: (alike(n, last_n[0]), given.get(n, 1), -score(n)))
        given[pick] = given.get(pick, 1) + 1
        last_n[0] = pick
        return pick

    # ---- the two runs, worked out first
    n_hook = sum(len(l) for s, l in order if s == "hook")
    n_drop = sum(len(l) for s, l in order if s in ("drop", "run"))
    custom = [n for n in (fast.get("order") or []) if usable(n)]   # the agent's own order (the editor screen), if they changed it
    if custom:
        seq_all = [n for n in dict.fromkeys(custom) if n not in (final, calm)]
        used.update(seq_all[:n_hook + n_drop])
        hook_seq = seq_all[:n_hook]
        drop_seq = seq_all[n_hook:n_hook + n_drop]
        while len(hook_seq) < n_hook:
            hook_seq.append(take(opens, (drops,)))
        while len(drop_seq) < n_drop:
            drop_seq.append(take(drops, (opens,)))
    else:
        used.add(opens[0])
        hook_seq = [opens[0]]
        while len(hook_seq) < n_hook:
            hook_seq.append(take(opens[:n_hook + 1], (drops[n_drop:],)))
        base: list[str] = []
        for n in drops:
            if len(base) < n_drop and n not in used and n not in (final, calm):
                used.add(n)
                base.append(n)
        pads = [take([], (opens,)) for _ in range(n_drop - len(base))]
        pads.sort(key=lambda n: -score(n))
        drop_seq = base[:-1] + pads + base[-1:] if len(pads) < 6 else base[:11] + pads + base[11:]   # long video: fillers in the middle
        apart(hook_seq)
        apart(drop_seq)
        # Beds and baths always sits on a bedroom or bathroom: if none is in a shot long enough for a stat, the
        # nicest one (the AI's own score) is moved into one.
        slots, hi, di = [], 0, 0
        for sec, lens in order:
            for i, L in enumerate(lens):
                okk = L >= 3 or (L == 2 and i + 1 < len(lens) and lens[i + 1] == 1)
                if sec == "hook":
                    if okk and i > 0:
                        slots.append(("h", hi))
                    hi += 1
                elif sec in ("drop", "run"):
                    if okk:
                        slots.append(("d", di))
                    di += 1
        seqs = {"h": hook_seq, "d": drop_seq}
        isbb = lambda n: (info.get(n) or {}).get("kind") in ("bedroom", "bathroom")  # noqa: E731
        slots = [(a, i) for a, i in slots if i < len(seqs[a])]
        cands = sorted([n for n in info if usable(n) and isbb(n) and n not in (final, calm)], key=lambda n: -score(n))
        if slots and cands and not any(seqs[a][i] == cands[0] for a, i in slots):
            B = cands[0]
            tgt = next(((a, i) for a, i in slots if isbb(seqs[a][i])), None) or next(((a, i) for a, i in slots if a == "d" and i > 0), slots[-1])
            old = seqs[tgt[0]][tgt[1]]
            was = next(((k, j) for k in ("h", "d") for j, v in enumerate(seqs[k]) if v == B), None)
            if was:
                seqs[was[0]][was[1]] = old
            else:
                used.add(B)
                used.discard(old)
            seqs[tgt[0]][tgt[1]] = B
    hook_it, drop_it = iter(hook_seq), iter(drop_seq)

    shots: list[dict] = []
    beat = 0.0
    marks: dict[str, float] = {}
    fires: dict[str, Any] = edit.get("fires") or {}

    def add(n: str, length: float, section: str, **o: Any) -> dict:
        nonlocal beat
        last_n[0] = n
        p, q = photos[n], info[n]
        a, e = _cl(q.get("start_x", 0.5)), _cl(q.get("end_x", 0.5))
        move = {"pan": "sweep", "push": "push", "rise": "rise", "orbit": "orbit"}.get(q.get("move"), "push")
        if move == "sweep" and abs(e - a) < 0.08:
            a = _cl(e - 0.16 if e >= 0.5 else e + 0.16)
        sx = q.get("x")
        if sx is not None:
            # the pan ENDS dead centre on the photo's subject (read off a ruler), having started away from it on the
            # side with more picture; a push / rise goes straight at it
            e = _cl(sx)
            a = e if move in ("push", "rise") else _cl(e - 0.17 if e >= 0.5 else e + 0.17)
        if length <= 1 and abs(a - e) > 0.06:
            a = _cl(e + (0.06 if a > e else -0.06))   # a one-beat shot barely travels: it starts close to where it lands
        s: dict[str, Any] = {
            "id": f"{n}-{len(shots)}", "photo": n, "src": p.get("sharp") or p.get("clean") or p.get("source"), "depth": p.get("depth"),
            "sharpDepth": bool(p.get("sharpDepth")), "kind": q.get("kind") or "interior", "start": at(beat), "end": at(beat + length),
            "move": move, "fromX": a, "toX": e, "focusX": e, "water": bool(q.get("water") or p.get("water")), "sky": bool(q.get("sky")),
            "_sec": section, "_score": score(n), "_what": q.get("what") or "",
        }
        fb = fires.get(n) or (p.get("fireBox") if p.get("fire") else None)
        if isinstance(fb, (list, tuple)) and len(fb) == 4:
            s["fire"], s["fireBox"] = True, [float(v) for v in fb]   # a lit fire: its flames flicker
            fc = (s["fireBox"][0] + s["fireBox"][2]) / 2
            if abs(e - fc) > 0.08:
                d = (fc + (0.08 if e > fc else -0.08)) - e
                if abs(d) <= 0.15:               # ...and the shot lands with the fire in view, unless that would pull the
                    s["toX"], s["fromX"] = _cl(e + d), _cl(a + d)   # frame off what the shot is about (a bed, a view)
                    s["focusX"] = s["toX"]
        s.update(o)
        shots.append(s)
        beat += length
        return s

    for si, (section, lens) in enumerate(order):
        marks.setdefault(section, beat)
        for i, L in enumerate(lens):
            last = i == len(lens) - 1
            if section == "hook":
                o: dict[str, Any] = {}
                if i == 0:
                    o.update(amt=1.0, ramp="steady", outFx="zoom")   # held calmly under the opening words
                if i == 1:
                    o["inFx"] = "zoom"
                if i == 5:
                    o["outFx"] = "whip"
                if i == 6:
                    o["inFx"] = "whip"
                add(next(hook_it), L, section, slot="open" if i == 0 else "run", **o)
            elif section in ("calm", "calm2"):
                add(calm if section == "calm" and calm else take(drops, (opens,)), L, "calm", ramp="steady", amt=0.9)
            elif section == "quick":
                add(take(drops, (opens,)), L, section)
            elif section in ("drop", "run"):
                o = {}
                if i == 0:
                    o["inFx"] = "flash"
                if last and order[si + 1][0] == "final":
                    o.update(ramp="launch", amt=1.2, outFx="zoom")
                add(next(drop_it), L, section, slot="run", **o)
            else:
                add(final, L, section, inFx="zoom", ramp="settle", amt=0.9)
    # the opening and closing shots go straight at the front door (the door finder's figure when there is one)
    for s in (shots[0], shots[-1]):
        x = photos[s["photo"]].get("aimX")
        x = _cl(x) if x is not None else s["toX"]
        s.update(move="push", fromX=x, toX=x, focusX=x)
    shots[0]["start"] = 0
    dur = shots[-1]["end"]

    # ---- the ending: a daytime closing photo turns to dusk and its windows glow
    # (owner 11/16: the drawn window glow lit whole strips - a garage door, half a window. The AI Twilight version of
    # the photo, made once at Make (studio_motion_service._fast_twilight), is dissolved in instead; the drawn glow is
    # only the fallback when that could not be made.)
    last_id = shots[-1]["photo"]
    wins = (edit.get("windows") or {}).get(last_id)
    twi = photos[last_id].get("twi")
    if info[last_id].get("light") == "day" and (twi or wins):
        shots[-1]["dusk"] = True
        if twi:
            shots[-1]["duskSrc"] = twi
        else:
            shots[-1]["windows"] = wins

    # ---- stats over the longer shots
    show_stats = bool(sections.get("stats", True))
    off = set(fast.get("stats_off") or [])
    facts: list[tuple[str, str, str]] = []
    beds, baths = str(facts_in.get("beds") or ""), str(facts_in.get("baths") or "")
    if beds and baths:
        facts.append(("bedbath", f"{beds} · {baths}", "Beds · Baths"))
    elif beds:
        facts.append(("bedbath", beds, "Bedrooms"))
    elif baths:
        facts.append(("bedbath", baths, "Bathrooms"))
    if facts_in.get("sqft"):
        facts.append(("living", str(facts_in["sqft"]), "Square feet"))
    if facts_in.get("acres"):
        facts.append(("exterior", str(facts_in["acres"]), "Acre lot"))
    facts = [f for f in facts if f[0] not in off] if show_stats else []

    def fits(i: int) -> bool:
        x = shots[i]
        if x["_sec"] == "calm":
            return False
        d = x["end"] - x["start"]
        nx = shots[i + 1]
        return d >= 3 * b - 0.02 or (d >= 2 * b - 0.02 and nx["_sec"] == x["_sec"] and (nx["end"] - nx["start"]) <= b + 0.02 and i + 1 < len(shots) - 1)

    long_shots = [shots[i] for i in range(1, len(shots) - 1) if fits(i)]
    left = list(facts)
    bb = max([y for y in long_shots if _GROUP.get(y["kind"]) == "bedbath"], key=lambda y: y["_score"], default=None)
    for x in long_shots:                      # first: the stat that belongs to this kind of room
        f = next((f for f in left if f[0] == _GROUP.get(x["kind"])), None)
        if f and f[0] == "bedbath" and x is not bb:
            continue                          # beds and baths: only on the nicest bedroom / bathroom
        if f:
            x["fact"] = {"value": f[1], "label": f[2]}
            left.remove(f)
    left = [f for f in left if f[0] not in ("bedbath", "exterior")]   # never over another kind of room
    for x in long_shots:                      # then the rest (square feet), on the long shots still free
        if left and "fact" not in x:
            f = left.pop(0)
            x["fact"] = {"value": f[1], "label": f[2]}
    # never two stat shots close together; beds and baths keeps its place
    fi = [i for i, x in enumerate(shots) if x.get("fact")]
    pin = next((i for i in fi if "Bed" in shots[i]["fact"]["label"] or "Bath" in shots[i]["fact"]["label"]), None)
    kept = [pin] if pin is not None else []
    for i in fi:
        if i == pin:
            continue
        if all(abs(i - k) >= 5 for k in kept):
            kept.append(i)
            continue
        f = shots[i].pop("fact")
        later = next((y for y in long_shots if "fact" not in y and all(abs(shots.index(y) - k) >= 5 for k in kept)), None)
        if later is not None:
            later["fact"] = f
            kept.append(shots.index(later))
    # each stat shot takes over the quick shot after it (2 beats -> 3), so the cuts stay on the beat
    i = 1
    while i < len(shots) - 2:
        x, nx = shots[i], shots[i + 1]
        if x.get("fact") and "fact" not in nx and nx["_sec"] == x["_sec"] and (nx["end"] - nx["start"]) <= b + 0.02:
            x["end"] = nx["end"]
            if nx.get("outFx"):
                x["outFx"] = nx["outFx"]
            del shots[i + 1]
        i += 1
    for x in shots:                           # a stat is only shown where it can be read, and that shot moves gently
        if x.get("fact"):
            if (x["end"] - x["start"]) < 3 * b - 0.02:
                x.pop("fact")
                continue
            e = x["toX"]
            x["fromX"] = _cl(e + (0.05 if x["fromX"] > e else -0.05)) if x["fromX"] != e else e
            x.update(amt=0.6, ramp="steady")

    stats = " · ".join(x for x in [beds and f"{beds} Beds", baths and f"{baths} Baths", facts_in.get("sqft") and f"{facts_in['sqft']} Sq Ft"] if x)
    copy = motion.get("copy") or {}
    situation = motion.get("situation", "just_listed")
    text: dict[str, Any] = {
        "address": facts_in.get("address") or "", "cityLine": facts_in.get("cityLine") or "", "price": facts_in.get("price") or "",
        "stats": stats if show_stats else "", "courtesy": "",   # owner 11/16: no 'listing courtesy of' line on the ending
        "titleAt": [at(marks["calm"] + 0.4), at(marks["calm"] + 3.6)], "endAt": at(marks["final"] + 0.5),
        "titleStyle": "plate", "kicker": (copy.get("kicker") or KICKERS.get(situation) or situation.replace("_", " ").title()),
    }
    if sections.get("opening", True):
        text["openEnd"] = shots[0]["end"]
    accent = motion.get("accent") or "#E8C98A"
    brand = {"theme": motion.get("theme", "classic"), "accent": accent, "metal": motion.get("metal", "gold"), "situation": situation,
             "copy": {k: copy.get(k) or "" for k in ("kicker", "title", "tagline", "cta")},
             "facts": {"address": text["address"], "cityLine": text["cityLine"]}, "agent": motion.get("agent") or {"name": ""}, "sections": sections}
    music = f"motion/fastcut/{song_of(motion)}{'60' if long else ''}.wav"
    # (each shot keeps which part of the song it is in - the editor's "Your shots" board groups them by it)
    out_shots = [{**{k: v for k, v in s.items() if not k.startswith("_")}, "section": s.get("_sec")} for s in shots]
    # "order": the photos of the two runs as they were laid out - what the editor screen reorders (fast.order)
    return {"shots": out_shots, "format": fmt, "music": music, "duration": dur, "text": text, "accent": accent, "brand": brand,
            "order": list(hook_seq) + list(drop_seq)}


def outline(props: dict, motion: dict) -> list[dict]:
    """The plan as the editor screen shows it before the video is made: one row per shot."""
    labels = (motion.get("fast") or {}).get("edit", {}).get("photos") or {}
    rows = []
    for i, s in enumerate(props.get("shots") or []):
        q = labels.get(s.get("photo")) or {}
        rows.append({"photo": s.get("photo"), "start": s["start"], "seconds": round(s["end"] - s["start"], 2), "what": q.get("what") or "",
                     "stat": s.get("fact"), "role": "opening" if i == 0 else "closing" if i == len(props["shots"]) - 1 else "",
                     "movable": s.get("slot") == "run", "kind": s.get("kind"), "section": s.get("section"),
                     "fire": bool(s.get("fire")), "dusk": bool(s.get("dusk"))})
    return rows
