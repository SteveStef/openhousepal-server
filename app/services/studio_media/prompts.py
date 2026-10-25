"""Prompt builders for Studio AI Property Media.

Photo edits can STACK effects, so each effect is a plain instruction body (no preservation
clause of its own); `build_stacked_prompt` composes the selected ones in a sensible order and
appends the shared PRESERVATION_CLAUSE once. AI-video prompts pair a single camera-move
instruction with the video preservation text. Bump PROMPT_VERSION on any wording change.
"""

PROMPT_VERSION = "v23"  # v23 (10/24): removals also take shadows/reflections; masked removal keeps non-targets sharp

# Appended after PRESERVATION_CLAUSE on every image edit. A watermark / logo / overlay is NOT a
# property feature, so removing it doesn't conflict with "preserve everything" above - call it
# out explicitly so the model actually cleans it instead of faithfully copying it through.
DEBRAND_CLAUSE = (
    "The source photo may carry an MLS watermark, brokerage or camera logo, a date/time stamp, "
    "or other overlaid text - most often a small tag in the lower-left corner. Remove any such "
    "watermark or overlay text completely and realistically reconstruct whatever it was "
    "covering, matching the surrounding texture, lighting and perspective. The finished image "
    "must contain no watermark, logo, border, or overlaid text of any kind."
)

# Applied once to every still-image edit (stacked or single). Imperative and specific because
# vague "keep it realistic" language does not stop image models redrawing rooflines, moving
# windows, or inventing landscaping.
PRESERVATION_CLAUSE = (
    "CRITICAL - this is a real, for-sale property and the edit must not misrepresent it. "
    "Preserve exactly, with no change to shape, size, position, count, material, colour or "
    "proportion: the architecture and overall structure; every exterior and interior wall; "
    "every window and its panes, frames and grilles; every door and garage door; roof lines, "
    "gables, dormers, chimneys and gutters; room dimensions, ceiling height and ceiling shape; "
    "floors and flooring material; cabinetry, countertops, islands and backsplashes; all "
    "built-ins, shelving and trim; fireplaces and mantels; all permanent light fixtures, fans, "
    "plumbing fixtures, vents and outlets; stairs and railings; the driveway and walkway shape "
    "and material; patios, decks and porches; the pool shape and coping; fences and retaining "
    "walls; and the position and extent of trees, beds and hardscape (landscaping boundaries). "
    "Do NOT add, remove, resize or relocate any permanent feature. Do NOT invent rooms, "
    "additions, square footage, views or amenities. Keep the camera angle, lens and framing "
    "identical to the source photo. The result must look like a photograph of this exact "
    "property, not a render or a different house."
)

_ROOM_TYPE_LABELS = {
    "living_room": "living room",
    "family_room": "family room",
    "primary_bedroom": "primary (owner's) bedroom",
    "bedroom": "bedroom",
    "nursery": "nursery",
    "kitchen": "kitchen (add only styling / decor - counter styling, stools, a runner, plants; do NOT remodel or change cabinetry, counters or appliances)",
    "dining_room": "dining room",
    "office": "home office",
    "basement": "finished basement / lower-level living area",
    "bonus_room": "bonus / flex room",
    "home_gym": "home gym",
    "sunroom": "sunroom / enclosed porch",
    "entryway": "entryway / foyer",
    "patio": "covered patio / outdoor living area",
}

_STAGING_STYLE_HINTS = {
    "modern": "modern style: clean lines, low-profile furniture, neutral palette with a few "
              "restrained accents, minimal clutter",
    "transitional": "transitional style: a blend of contemporary and classic, soft neutral "
                    "palette, comfortable tailored furniture, subtle texture",
    "luxury": "high-end luxury style: designer furniture, layered lighting, rich but tasteful "
              "materials (stone, brass, velvet, oak), gallery-quality art, nothing gaudy",
    "traditional": "traditional style: classic wood furniture, symmetrical arrangement, warm "
                   "palette, tailored upholstery, timeless decor",
}

_ENHANCEMENT_HINTS = {
    "grass": "make dull or patchy lawn look healthy, green and evenly mown, within the existing "
             "lawn boundaries",
    "sky": "replace a dull, blown-out or overcast sky with a natural, gently clouded blue sky, "
           "matching the property lighting to it",
    "brighten_exterior": "lift the overall exterior exposure and shadows so the facade reads "
                         "clearly, without blowing out highlights",
    "lighting": "even out and warm the lighting, recover shadow detail, correct colour casts",
    # Rewritten 2026-10-07: the old "remove only SMALL distractions" wording made the model keep a
    # car that filled half the frame (user report). Removal is now explicit about size.
    "declutter": "remove every vehicle (cars, trucks, vans, motorcycles, boats, trailers) and all "
                 "movable clutter (trash and recycling bins, hoses, cones, toys, bikes, loose tools lying on the ground, "
                 "ladders, boxes, bags, signs on stakes) - REGARDLESS OF SIZE, even an object that "
                 "covers a large part of the photo",
    "remove_furniture": "remove all movable furniture and loose decor - chairs, sofas, tables, "
                        "beds, ottomans, rugs, lamps, shelving units, outdoor furniture, umbrellas, "
                        "grills and loose planters - REGARDLESS OF SIZE, leaving the space empty; "
                        "keep everything built in (cabinetry, built-in shelving, appliances that "
                        "are built in, light fixtures, fireplaces)",
    # "Clean up" - fallen yard debris only. Spelled out so the model clears what's LYING on
    # surfaces and rebuilds the real surface underneath, never strips living plants or changes
    # the season on the trees (a fall photo keeps its fall foliage).
    "yard_cleanup": "clean up the yard: remove fallen leaves, sticks, twigs, small branches, "
                    "pine needles, acorns and loose debris lying on the lawn, driveway, walkway, "
                    "steps, patio, deck, porch and in the garden beds, and show the existing "
                    "surface underneath as it looks when freshly cleaned - the same lawn, the same "
                    "paving, decking or stone with its real pattern and colour, the same mulch or "
                    "soil in the beds. Remove ONLY loose debris on surfaces: keep every tree, "
                    "shrub, plant, flower, bed edge and planter exactly as and where it is, and "
                    "do not change the foliage on the trees or the season of the photo",
    "polish": "general professional listing-photo polish: straighten verticals, clean white "
              "balance, gentle contrast and clarity, tasteful saturation",
}

# The "Clean up" picks - removals get their own emphatic block (see build_stacked_prompt) and
# this fill instruction, so the model rebuilds what was hidden instead of leaving a smudge.
_REMOVAL_HINTS = {"declutter", "remove_furniture", "yard_cleanup"}
_REMOVAL_FILL = (
    "Where each removed object was, show what would naturally be there, continuing the surrounding "
    "surfaces seamlessly - the same driveway, road, lawn, floor, rug-free flooring, wall, siding "
    "and trim, with matching texture, pattern, perspective, lighting and shadows - so no outline, "
    "blur, smudge or ghost of the object remains. Remove each object's shadow with it - the shadow "
    "it casts on the ground, floor or wall, the dark contact shadow right under it - and its "
    "reflection and any stain or flattened patch it leaves, so that spot is lit exactly like the "
    "surface around it. Where an object hid part of the house, continue "
    "only what the visible parts clearly show; never invent new windows, doors or features. "
    "These are movable, non-permanent items, so removing them does not conflict with the "
    "preservation rules below."
)

# --- effect instruction bodies (order below = the order they're applied when stacked) ---
_STACK_ORDER = ["virtual_staging", "enhance", "lights_on", "twilight"]

_EFFECT_BODIES = {
    # Reworded 2026-09-06 (direct feedback): the old wording named specific exterior fixture
    # types (porch, entry, path, soffit, landscape uplights) unconditionally, on every photo
    # regardless of whether that photo actually shows any such fixture - the same failure mode
    # already fixed elsewhere in this file (_VIDEO_PRESERVATION, _VIDEO_FINE_TEXTURE): naming a
    # thing at all raises the odds a generative model invents it. Here that meant the model could
    # add a porch light or landscape uplight to a house that doesn't have one. Now explicit in
    # both directions: light up whatever fixture is really there, never invent one that isn't.
    "twilight": (
        # Brightened 2026-10-07 (user: "a bit dark - more like sunrise, a tad brighter, but you
        # can still tell it's dark out"): early twilight just after sunset, not deep blue hour.
        "Convert this to a high-end early-twilight photograph, taken in the bright minutes just "
        "after sunset: a luminous sky with a warm peach-and-gold glow low on the horizon fading "
        "into soft, light blue above - like the glow of a sunrise, with no sun visible. Keep the "
        "house and grounds clearly lit by soft ambient light (about one stop brighter than a deep "
        "blue-hour shot) with every detail readable, yet unmistakably dusk, not daytime. Turn ON "
        "only the light fixtures "
        "already visible in this exact photo - interior lights so their windows glow warm, and "
        "any exterior light fixture already visible in the photo. Do not add, invent, or place "
        "any light fixture that is not already present in the source photo - if no exterior "
        "fixtures are visible, do not add any exterior lighting. Warm luxury real-estate look, "
        "balanced exposure between sky and house - bright and inviting, never murky or dark."
    ),
    "lights_on": (
        "Turn ON the visible lights - interior, exterior, and any lighting seen through windows "
        "- using only fixtures already present. Balance the exposure so lit areas look natural "
        "and inviting, not blown out. Do not otherwise change the time of day."
    ),
    "virtual_staging": (
        "Virtually stage the {room} with tasteful, realistic, REMOVABLE furniture and decor in "
        "a {style} - seating, tables, rugs, lamps, bed and linens, art, plants, window "
        "treatments as fits the room. Scale and place everything believably for the real "
        "dimensions and light, with correct perspective and soft grounded contact shadows. It "
        "must read as professional real-estate virtual staging, not obviously AI-generated. The "
        "room itself stays completely unchanged."
    ),
    "enhance": (
        "Improve the photo to professional real-estate quality, doing ONLY: {lines}. Stay "
        "realistic and true to the property."
    ),
}

# Runway's API rejects promptText over 1000 characters, so callers can keep that limit when
# using Runway. Hailuo/fal accepts a longer prompt, and benefits from the explicit fine-texture
# instruction below.
_VIDEO_MOTION_HINTS = {
    # push_in is the safe move - it only ever shows a subset of the real photo. The others
    # expose the frame edges, so they're deliberately described as tiny.
    # Slowed on 2026-09-05 (was rendering as a noticeably fast zoom) - then walked back up ~35%
    # the same day once "barely-perceptible" over-corrected into feeling static. The request was
    # specifically for a "glide," not a zoom: framed as walking calmly forward through the space
    # rather than a lens move, since that reads as smoother/more intentional motion at a similar
    # speed instead of a camera "zooming."
    # Reworded again 2026-09-05 (later the same day) after TWO separate real generations - one on
    # Fast Pro, one on plain Pro, so this isn't a model-tier issue - both morphed a tight interior
    # room photo into an entirely different, fabricated house partway through the clip. Confirmed
    # by pulling the actual video frames: the room stays correct for ~1-1.5s, then dissolves into
    # invented content for the rest of the clip. Root cause: asking for continuous forward motion
    # for the WHOLE clip duration works fine for a deep exterior/aerial shot (plenty of real depth
    # to reveal) but a small enclosed room has almost none - once the model runs out of real depth
    # to reveal, it doesn't just stop, it starts inventing new space, and that can cascade into a
    # wholesale scene change. Old wording only asked for "modest progress by the end of the clip",
    # a qualitative target with no actual stopping point, so the model kept trying to move for the
    # full duration regardless of how little real depth the photo actually had. New wording gives
    # an explicit, early stopping point instead: move a little at the very start, then hold the
    # frame steady - not "less motion throughout," a SHORTER window of motion up front.
    # Kept under ~420 chars (see build_video_motion_prompt's budget for the hint) so it can never
    # push the assembled fal-path prompt over 2000 chars and risk truncating _VIDEO_PRESERVATION.
    # 2026-09-06: dropped the named examples ("dense trees, foliage, railings, siding, brick")
    # from the tail clause below, same reasoning as _VIDEO_FINE_TEXTURE/_VIDEO_PRESERVATION -
    # those are exterior-only nouns sent unconditionally even on interior photos, and naming a
    # thing at all raises the odds a generative model renders it. "Any dense, fine-detail area"
    # says the same thing without assuming what kind of detail is actually in the photo.
    # Nudged "very slow, gentle" -> "slow, smooth" 2026-09-06 (a little faster, per direct
    # feedback) - keep this a SMALL nudge if tuning further; this exact wording has already been
    # walked back and forth once before (see the 2026-09-05 history above) after overshooting in
    # both directions.
    # Added explicit "never a zoom" framing same day: a real test batch came back with one clip
    # (out of three, same prompt) reading as a lens zoom instead of the intended walk-forward
    # glide - inconsistent per-clip results are expected (separate independent generations, see
    # the "why does every clip look different" conversation), but this specific failure mode
    # (zoom vs. glide) has fought back before (2026-09-04 pan history) and is worth being as
    # explicit about as possible: naming the camera MOVEMENT vs. a lens/focal-length change
    # directly, not just implying it via "glide". Found the first draft of this addition ran
    # 61 chars over the ~453-char hint budget (base assembled prompt is 1547 chars at max_chars
    # =2000) and got silently word-truncated mid-sentence by build_video_motion_prompt's own
    # truncation safety net - it did its job (never touched _VIDEO_PRESERVATION), but losing the
    # tail of the hint itself wasn't intended either. Trimmed to fit with real margin instead of
    # relying on truncation to save it.
    # Doubled 2026-09-06 (direct feedback: "glide should be double") - covers roughly twice the
    # distance in the same 1-1.5s window as before. Kept the hard stopping point unchanged - that
    # part is the anti-hallucination fix, not a speed/distance setting, so it wasn't touched.
    # Quickened further 2026-09-06 (direct feedback: "a little quicker, doesn't seem to have
    # changed") - found the real cause of the doubled distance not visibly landing: the shared
    # assemble() wrapper above this dict unconditionally appended "Keep the motion slow and
    # smooth" after every hint, which was fighting this hint's own "double distance" wording on
    # every single generation. Removed "slow" from that wrapper (now just "smooth and cinematic")
    # and reworded this hint from "smooth" to "brisk/quick" so nothing upstream is still telling
    # the model to go slow.
    # Reverted the "move 1-1.5s then hold completely still" stopping point 2026-09-06 (direct
    # feedback: a real generation visibly froze mid-glide for the clip's last ~2 seconds and the
    # user wants continuous motion for the full clip, no stop, full stop). This was originally
    # added the same day as an anti-hallucination fix (see the long history below) for
    # shallow-depth interior photos that had nothing left to reveal once the glide kept going -
    # removing it reopens that specific risk in principle, but the user was explicit and direct
    # that the freeze itself is the worse problem. _VIDEO_PRESERVATION's existing "never invent
    # what isn't visible; make the move smaller rather than push past the photo's edges" clause is
    # the remaining safety net for that case, not a per-motion stopping point.
    # Also dropped every speed adjective (quick/brisk/faster-paced/slow/rush) per the same
    # feedback - describe the glide only as "very smooth" and "continuous", nothing about pace.
    "push_in": "a continuous, very smooth forward glide - pure camera movement, never a zoom or "
               "lens change - lasting the entire clip with no pause, stop, or hold at any point. "
               "Keep the movement smooth and even throughout, never pushing into any dense, "
               "fine-detail area of the frame",
    "slow_push_in": "a very slow, gentle forward glide with minimal depth movement, preserving "
                    "fine texture stability in whatever detail is already in the frame - "
                    "cinematic but restrained, never a zoom or rush toward the scene",
    "push_out": "a very slight, slow pull-back that still keeps only the photographed area on screen",  # retired - not in the rotation
    # Tightened 2026-09-04: worded for Hailuo 2.3 FAST Pro's behavior, but the non-fast Pro model
    # (now the default) took the same wording far more literally/dramatically on at least one
    # real generation - a "swoosh"-style hard pan instead of the intended barely-there drift.
    # Reworded more conservatively and explicitly capped in degrees rather than left to
    # the model's own sense of "subtle", which is exactly what varied between the two models.
    "pan_left": "an extremely slow, barely-perceptible parallax drift left, well under 5 degrees of apparent camera rotation - never a pan or sweep, just a faint sense of depth",
    "pan_right": "an extremely slow, barely-perceptible parallax drift right, well under 5 degrees of apparent camera rotation - never a pan or sweep, just a faint sense of depth",
    "drone_glide": "a slow, smooth dolly push-in toward the property",  # retired - alias push_in
}

_VIDEO_PRESERVATION = (
    # Rewritten 2026-09-06 to drop exterior-only container nouns (fireplace, fountain, foliage)
    # unconditionally sent on every clip regardless of whether the photo was interior or exterior
    # - naming a thing at all raises the odds a generative model renders it, and this was one
    # concrete, checkable piece of wording that could be feeding the mid-clip hallucination.
    # Revised again same day: that first rewrite dropped ALL example nouns, including the
    # PHENOMENON words (flame, water, steam) - a real test came back with fireplaces in-frame no
    # longer animating at all, because the exception clause gave the model nothing concrete to
    # recognize as "this counts as naturally-moving." flame / flowing water / steam-or-smoke are
    # kept back in below since they're symmetric - equally at home in an interior shot (fireplace,
    # sink, stove steam) or exterior one (fountain, pool, chimney smoke) - unlike the
    # container/exterior-biased nouns (fireplace, fountain, foliage, leaves/branches) that stay
    # gone for good.
    # Revised a third time same day: "it MAY move" still came back with a real fireplace fire
    # sitting static - a real once-observed result, not a fix, but a "may" gives the model an easy
    # out. Now a direct instruction to animate it when present ("animate that exact element... do
    # not leave it static") rather than a passive permission - still gated to only-if-visible, so
    # this doesn't reopen the invent-what-isn't-there risk, just removes the model's ability to
    # shrug off something that IS there.
    "Preserve the property exactly: do not morph, warp, restyle, add or remove anything. "
    "Windows, doors, rooflines, fixtures, furniture, landscaping and structure stay identical "
    "frame to frame, in the same position and shape - no new objects, people, animals, text or "
    "reflections. The ONE exception: if the photo already shows a flame, flowing or spraying "
    "water, or rising steam or smoke, animate that exact element with its natural motion - do "
    "not leave it static. This only ever applies to an element already visible in the source "
    "photo, and only its motion may change, never its shape, size or position. Never introduce a "
    "moving element that isn't already there. "
    "This is a flat 2D listing photo, not a 3D scene: the camera move must never reveal or "
    "invent anything not already visible in the photo. Do not add anything beyond exactly what "
    "the original shows. If the move would push past the photo's edges, make it smaller - a "
    "tighter, safer move is always better than a hallucinated one. Photorealistic and stable: "
    "no warping, no AI artefacts."
)

_VIDEO_FINE_TEXTURE = (
    # Same rewrite as _VIDEO_PRESERVATION above - the old version named exterior-only textures
    # (foliage, roof shingles, brick, siding) unconditionally, even on interior photos. Now
    # describes the principle (whatever is actually there should look crisp and stunning) with
    # no named nouns, so it applies correctly to any photo without needing to classify it first.
    "Render every surface and texture already visible in this exact photo with crisp, stunning, "
    "individually readable detail throughout the clip - sharp and true to the source, never "
    "dissolving into a soft blur or painterly smear as the camera moves. Only sharpen what is "
    "already there; never invent or add texture, material or detail that isn't in the original "
    "photo."
)


# Removals aimed with "circle what to remove" - big things (a car, a chair). Leaves are small and
# everywhere, so yard clean-up is never circle-limited: it always cleans the whole photo (user,
# 2026-10-07: nobody should have to circle leaves).
MASKABLE_REMOVALS = ("declutter", "remove_furniture")


# The whole-photo step that runs AFTER a circled removal (twilight, staging, lighting...): the
# photo is already cleaned exactly as the agent wants, so this step must not "tidy" anything
# else away. When leaf clean-up is part of that step, KEEP_EXCEPT_DEBRIS_CLAUSE is used instead.
KEEP_EXCEPT_DEBRIS_CLAUSE = (
    "The big objects in this photo have already been removed exactly as the owner wants. In this "
    "step the ONLY things that may be removed are loose fallen leaves, sticks, twigs, pine "
    "needles and similar debris lying on surfaces. Every other item visible in the photo (tools, "
    "stands, shelves, bins, fixtures, drains, outlets, decor, plants) must still be there, "
    "unchanged in shape, size and position."
)
KEEP_EVERYTHING_CLAUSE = (
    "This photo has already been cleaned up exactly as the owner wants. In this step do NOT "
    "remove, add, move or replace ANY object - every item visible in the photo (tools, stands, "
    "shelves, bins, fixtures, drains, outlets, decor, vehicles, furniture) must still be there, "
    "unchanged in shape, size and position. Only the requested change to the look happens."
)


def split_masked_removal(effects: list[str], options: dict | None):
    """For a photo with circled areas: (prompt for the removal-only pass, effects left after it,
    options left after it). (None, effects, options) when there's no maskable removal picked."""
    options = options or {}
    enh = list(options.get("enhancements") or [])
    picks = [x for x in enh if x in MASKABLE_REMOVALS]
    if "enhance" not in effects or not picks:
        return None, effects, options
    removal = (
        "OBJECT REMOVAL - the only job of this edit: "
        + "; ".join(_ENHANCEMENT_HINTS[x] for x in picks) + ". "
        + "Remove ONLY the objects inside the area marked for editing; everything outside that "
        "area must stay exactly as it is. The marked area is drawn loosely and may also take in "
        "things that are NOT to be removed - walls, anything mounted on a wall or ceiling "
        "(pegboards and the tools hanging on them, shelves, cabinets, outlets, switches, "
        "windows, doors, trim), floor drains and built-in features: redraw those exactly as "
        "they are, sharp, unblurred and in line with the parts outside the area, never cut off "
        "or blended into the wall. " + _REMOVAL_FILL
    )
    prompt = f"{removal}\n\n{PRESERVATION_CLAUSE}\n\n{DEBRAND_CLAUSE}"
    rest_enh = [x for x in enh if x not in MASKABLE_REMOVALS]
    rest_effects = [e for e in effects if e != "enhance" or rest_enh]
    rest_options = {**options, "enhancements": rest_enh}
    return prompt, rest_effects, rest_options


def build_stacked_prompt(effects: list[str], options: dict | None) -> str:
    """Compose one prompt for the selected photo effects (applied in _STACK_ORDER) plus the
    shared preservation clause. If both twilight and lights_on are picked, twilight already
    covers the lights so lights_on is dropped."""
    options = options or {}
    sel = [e for e in _STACK_ORDER if e in effects]
    if "twilight" in sel and "lights_on" in sel:
        sel.remove("lights_on")
    # Empty the room first, THEN stage it - otherwise the removal would take the new staging out.
    if "virtual_staging" in sel and "enhance" in sel and "remove_furniture" in (options.get("enhancements") or []):
        sel.remove("enhance")
        sel.insert(0, "enhance")

    parts: list[str] = []
    for e in sel:
        body = _EFFECT_BODIES[e]
        if e == "virtual_staging":
            body = body.format(
                room=_ROOM_TYPE_LABELS.get(options.get("room_type"), "living room"),
                style=_STAGING_STYLE_HINTS.get(options.get("style"), _STAGING_STYLE_HINTS["transitional"]),
            )
        elif e == "enhance":
            picked = [x for x in (options.get("enhancements") or []) if x in _ENHANCEMENT_HINTS] or ["polish"]
            removals = [x for x in picked if x in _REMOVAL_HINTS]
            looks = [x for x in picked if x not in _REMOVAL_HINTS]
            body = body.format(lines="; ".join(_ENHANCEMENT_HINTS[x] for x in looks)) if looks else ""
            if removals:
                # Removal first and loudest - it's the part a model most often quietly skips.
                body = (
                    "OBJECT REMOVAL - the most important part of this edit, do not skip it: "
                    + "; ".join(_ENHANCEMENT_HINTS[x] for x in removals) + ". "
                    + _REMOVAL_FILL + (" Then: " + body if body else "")
                )
        parts.append(body)

    if len(parts) > 1:
        instruction = ("Apply ALL of the following edits to this real-estate photo, in this "
                       "order, as one coherent professional result:\n- " + "\n- ".join(parts))
    else:
        instruction = parts[0]
    return f"{instruction}\n\n{PRESERVATION_CLAUSE}\n\n{DEBRAND_CLAUSE}"


def build_video_motion_prompt(
    motion: str,
    prompt_suffix: str | None = None,
    *,
    max_chars: int = 1000,
) -> str:
    """Found 2026-09-05 while lengthening the push_in hint: the OLD version of this function
    built the full string then did `prompt[:max_chars]` - a blind tail-cut. The old push_in hint
    already put the assembled fal-path prompt at 1911/2000 chars, and the new hint pushed it over,
    silently truncating off the ENTIRE _VIDEO_PRESERVATION clause - the one paragraph that tells
    the model not to invent content it can't see, i.e. exactly the anti-hallucination guardrail
    this hint change was trying to strengthen. Now the motion hint (pure flavor text - which
    camera move, how fast) is the ONLY part that ever gets shortened; _VIDEO_FINE_TEXTURE and
    _VIDEO_PRESERVATION are never truncated regardless of max_chars."""
    hint = _VIDEO_MOTION_HINTS.get(motion, _VIDEO_MOTION_HINTS["push_in"])
    suffix = (prompt_suffix or "").strip()
    suffix_block = f"\n\nAdditional quality instruction: {suffix}" if suffix else ""

    def assemble(h: str) -> str:
        return (
            f"Animate this still real-estate listing photo as a short, cinematic marketing clip "
            f"with {h}. Keep the motion smooth and cinematic, for social media.\n\n"
            f"{_VIDEO_FINE_TEXTURE}\n\n"
            f"{_VIDEO_PRESERVATION}"
            f"{suffix_block}"
        )

    budget = max_chars - len(assemble(""))
    if budget < 20:
        # Pathologically small max_chars (shouldn't happen with real callers, but never crash or
        # silently drop the safety clause over it) - shrink the hint as far as it'll go.
        budget = max(0, max_chars - len(assemble("")))
    if len(hint) > budget:
        hint = hint[:budget].rsplit(" ", 1)[0] if budget > 0 else ""
    return assemble(hint)


# Day-to-twilight AI Video style (2026-09-15) - not a camera move at all, unlike everything in
# _VIDEO_MOTION_HINTS above: the frame holds completely still while the SKY/AMBIENT LIGHT
# transitions from full daylight to dusk over the clip. Reads best on an exterior photo (a real
# sky, and real exterior fixtures to light up) - an interior still runs through this path but has
# less to sell the transition with (no sky, no windows-glowing-warm payoff), so the frontend
# nudges toward exterior photos rather than hard-blocking interior ones.
#
# Same anti-hallucination discipline as the twilight IMAGE effect above (_EFFECT_BODIES
# ["twilight"]): only turn on fixtures already visible in the photo, never invent one that isn't
# there - a generative video model asked for "twilight" with no such guard will happily paint a
# porch light onto a house that never had one.
#
# Kept intentionally tight (measured at 483 chars) so the assembled prompt (intro + _VIDEO_
# FINE_TEXTURE + this clause) totals ~953/1000 chars even before an optional suffix - Runway's
# hard 1000-char cap on promptText, same constraint build_video_motion_prompt budgets against.
_VIDEO_TIMELAPSE_PRESERVATION = (
    "Preserve the property exactly: no morphing, warping, adding or removing objects, people, "
    "vehicles or text; structure, windows, doors and landscaping stay identical, camera "
    "completely still, no pan or zoom. Only the light changes: sky and ambient light shift "
    "smoothly from day to a bright early dusk - warm glow low, house still clear. Turn on only fixtures already "
    "visible so windows glow warm - never invent one. Shadows lengthen and soften as light "
    "fades. Photorealistic, no flicker or artefacts."
)


def build_video_timelapse_prompt(prompt_suffix: str | None = None, *, max_chars: int = 1000) -> str:
    """Day-to-twilight lighting timelapse for AI Video - a sibling to build_video_motion_prompt,
    not a variant of it: there is no camera-move hint here, the frame holds still and only the
    sky/light changes. Same truncation discipline: _VIDEO_TIMELAPSE_PRESERVATION and
    _VIDEO_FINE_TEXTURE never get cut - if the optional suffix would push the assembled prompt
    over max_chars, the suffix is dropped instead (the only piece here that's ever optional)."""
    suffix = (prompt_suffix or "").strip()

    def assemble(with_suffix: bool) -> str:
        suffix_block = f"\n\nAdditional quality instruction: {suffix}" if (with_suffix and suffix) else ""
        return (
            "Animate this still real-estate photo as a day-to-twilight timelapse - the camera "
            "stays completely still.\n\n"
            f"{_VIDEO_FINE_TEXTURE}\n\n"
            f"{_VIDEO_TIMELAPSE_PRESERVATION}"
            f"{suffix_block}"
        )

    prompt = assemble(True)
    return prompt if len(prompt) <= max_chars else assemble(False)


# Refine pass (image pass 2+) - the input is already the desired edit, just a rough draft.
REFINE_PROMPT = (
    "This is a draft edit of a real for-sale property photo. Improve only its photographic "
    "quality - sharpness, fine detail, realistic lighting balance, natural colour, removal of "
    "AI artefacts or noise. Do NOT change the composition, framing, time of day, staging, or "
    "any property feature; keep the scene exactly as it already is."
    f"\n\n{PRESERVATION_CLAUSE}"
)
