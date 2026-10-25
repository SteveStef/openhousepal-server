"""Builds the OpenHousePal Email Signature - a condensed, email-safe "mini Agent Hub" the agent
installs once in Gmail/Outlook/Apple Mail. There is a single design, "The Gallery": a left icon
rail, a headshot + identity block, a recent-sale/featured-listing strip, and an optional stats
column, all inside one card. (Earlier versions of this feature shipped 4 template choices; the
product decision was to standardize on one polished design instead of spreading effort thin across
four, so TEMPLATES below is deliberately a single entry, kept as a list so the config/resolve shape
doesn't need to change if that ever reverses.)

Every output shares the same resolved data:

  - build_signature_html(): the actual install HTML (table-based, inline-styled, no flexbox/grid -
    the subset every major email client renders the same way).
  - render_listing_bar_jpeg()/render_listing_bar_gif(): the ENTIRE recent-sale/featured-listing
    strip - photo, status pill, address, price, beds/baths/acres/sqft - flattened into one image,
    not photo-plus-separate-HTML-text. This is deliberate: the image is fetched live by URL every
    time the signature is viewed (see pick_featured_items' day-based rotation), so baking
    everything into it is what keeps an already-installed, never-touched-again signature showing
    whichever listing/sale is CURRENT on the agent's Agent Hub, with no re-copy-paste ever
    required. Real separate HTML text would freeze at whatever was true the moment the agent
    copied the signature, while only the photo silently rotated underneath it - a mismatch.
  - render_photo_gif(): a Ken-Burns settle of the agent's headshot into a squared, alternating-
    corner-radius frame (an editorial "one sharp corner" look, not a plain circle/rectangle), with
    a pair of offset outline accent lines baked in behind it.
  - render_badge_gif(): a small pop-in-and-glow social icon badge for the left icon rail.

Real, true CSS/JS animation does not render inside an email body in any major client (no
@keyframes, no transitions, no JS). Animated GIF is the only format that actually moves once the
signature is installed - that's why every "animated" surface here is a server-rendered GIF, not a
CSS animation.

Every animation here is a ONE-SHOT ENTRANCE, not an infinite loop: it plays once when the image
first renders and then rests on its final frame, which is always the same framing/composition a
static screenshot of the design would show. GIF's default (omitting the Netscape loop extension
entirely - see _frames_to_gif) already means "play once, hold last frame," which is both the
visually correct behavior a reader expects from a signature (subtle motion on open, not a
distracting perpetual loop) and cheaper to encode.

GIF frames share one quantized palette (see _frames_to_gif) so gradients don't flicker
frame-to-frame the way independent per-frame quantization would, and use disposal=1 ("leave the
previous frame in place, draw the next one directly over it") rather than disposal=2 ("clear to
background first") - every frame here is a complete, full-canvas image, so disposal=2 would insert
a flash of the logical-screen background color between frames on any client that honors it
literally, which reads as flicker/jank; disposal=1 is the correct, standard choice for sequential
full-frame replacement.

Like vcard_service, fields that already exist elsewhere on the Agent Hub (name, title, brokerage,
phone/email, website, social links, headshot) are include/exclude toggles here - the value is read
live from AgentHub.config so nothing is re-entered or goes stale. There is no brokerage "logo
upload" field anywhere in the product, so brokerage is always rendered as styled text, never an
image - the same choice every other Agent Hub surface already makes.
"""
import logging
import os
import re
from datetime import date, datetime, timezone
from functools import lru_cache
from io import BytesIO
from typing import Any, Optional

from app.models.database import User

logger = logging.getLogger(__name__)

_FONT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts", "Inter-Variable.ttf")
_FONT_OPSZ_MAX = 32.0

DEFAULT_ACCENT = "#B87943"

# Same id->stack map (and same ids) as FONT_OPTIONS in the frontend's studio/email-signature/
# page.tsx - email-safe stacks only (every one preinstalled on Windows/Mac/mobile; @font-face gets
# stripped by every major email client anyway, so a "custom font" option would silently do nothing
# once actually installed).
# Single-quoted font names, not double - every one of these gets embedded raw into a
# double-quoted HTML style="..." attribute all over this file (never HTML-escaped first), so a
# literal " here previously terminated that attribute early at "Segoe/"Times/"Trebuchet, silently
# dropping every style declared after it (background, border-radius, padding, ...) in real inboxes
# too, not just this repo's own preview tooling.
FONT_STACKS = {
    "system": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif",
    "arial": "Arial, Helvetica, sans-serif",
    "georgia": "Georgia, 'Times New Roman', Times, serif",
    "trebuchet": "'Trebuchet MS', Tahoma, sans-serif",
}
_DEFAULT_FONT_STACK = FONT_STACKS["system"]


def _font_stack(cfg: dict) -> str:
    return FONT_STACKS.get(cfg.get("fontFamily"), _DEFAULT_FONT_STACK)

# Fixed pixel dimensions for every server-rendered asset - the HTML declares the exact same
# width/height on the <img>, which keeps layout stable across clients while it loads.
#
# Static/cheap assets (the badge icons, the listing JPEG fallback) render at 2x their display
# size for crisp retina rendering - JPEG and small flat-icon GIFs compress cheaply enough that
# the extra resolution costs almost nothing.
#
# The two PHOTOGRAPHIC animated GIFs (photo.gif, listing.gif - see render_photo_gif/
# render_listing_photo_gif) render at 1x (native display size) instead. GIF has no inter-frame
# delta compression, so a retina photographic animation is disproportionately expensive; at this
# display size (well under 150px) 1x still looks sharp, and it's the single biggest lever for
# keeping each image in the "loads instantly, doesn't read as image-heavy" range every email
# signature guide recommends (rule of thumb: keep each individual image well under ~50KB).
_STATIC_SCALE = 2
_BADGE_SIZE = 26  # displayed size of a rail icon circle at the FULL 5-icon count (rendered at
# _STATIC_SCALE). 28 -> 24 -> 26 (explicit ask, this round: "a little bigger on the email
# version"). Not smaller than this - under ~22-24px a round tap target starts reading as too small
# to comfortably hit on a phone, and the glyph itself gets hard to recognize at a glance.
#
# This is the FLOOR, not the only size any more - _rail_icons_html grows each icon further when
# the agent has fewer than 5 networks filled in (explicit ask: "if the agent deselects some it
# should make them all a bit bigger since there is more space there" - the pill's own OUTER width
# (_RAIL_OUTER_W, below) stays fixed either way, since that's baked into the card's overall width
# math at module-load time; only the icon drawn inside it grows, up to _BADGE_SIZE_MAX.
_BADGE_SIZE_MAX = _BADGE_SIZE + 8  # 34 at 5 icons' 26px floor - leaves a comfortable ~6px margin
# inside the pill's own ~46px inner width (_BADGE_SIZE + 20) even at the largest size, so a single
# icon never touches the pill's own border.
# A small gap between the icon rail's own accent-bordered pill and the card's outer edges (top,
# bottom, left) - the pill used to be exactly as tall/wide as the row it sat in, so its border sat
# flush against the card's own border on 3 sides with no breathing room at all. This inset is spent
# as real padding on a plain (borderless, card-bg-colored) OUTER <td>, with the actual bordered pill
# as a fixed-height inner table nested inside it - a <td>'s own padding can't pull ITS OWN border
# away from its box edge, so pulling the pill's border off the card edge needs this second layer.
_RAIL_INSET = 4
# Only left/top/bottom get this gap now, not right (explicit ask: "bring the icons over to the
# right, don't need that much dead space" - the old symmetric inset was leaving an unused strip of
# card_bg between the pill's own right edge and main_cell right next to it). One _RAIL_INSET, not
# two - see the outer rail <td>'s own padding in _build_gallery.
_RAIL_OUTER_W = _BADGE_SIZE + 20 + _RAIL_INSET
# Same idea as _RAIL_INSET used to be, mirrored for the stats column, but fully removed now on
# every side (explicit ask: "fill all the space... to the top and bottom and right side") - see
# the stats <td> in _build_gallery, which no longer spends anything on a breathing-room gap here.
_STATS_W = 100  # 108 -> 96 -> 112 -> 100 (explicit ask: "fix the dimensions issue... i don't
# care if the stats section has to be condensed width-wise" - part of trimming the card's overall
# width for email-client safety. Still comfortably fits the widest real pill value ($17.24M needs
# 80px at the pill's own 18px/900 font - see _STATS_IMG_DISPLAY_W's own comment for the margin).
# _CARD_WIDTH is trimmed by the exact same 12px below, so main_width (the identity column's own
# width, and so the buttons' safe margin) is completely unaffected - only the stats column itself,
# and the card's total width, actually shrink.
# 708 -> 658 -> 680 -> 696 -> 688 -> 676. The 658 step was a real bug, not just a tight fit: its
# math (see the old comment this replaced) compared the button labels against the FULL main_cell
# content width (552px), but that whole width was never actually available to the identity column
# alone - the photo sits in the SAME row, in its own fixed-width <td> first, and the identity <td>
# only gets whatever's left over after that. At 658 the photo column was eating enough of the row
# that the identity column dropped under the ~378px the 3 real button labels ("My Agent Hub ->"/
# "Ready to Buy or Sell?"/"What's My Home Worth?") need bare at their actual 9.5px/700 font+padding,
# and the 3rd one wrapped to its own line. 680 fixed that with a healthy ~24px margin; 696 matched
# _STATS_W's own +16 with no change to that margin; 688 trimmed 8px off both the identity column's
# safety margin and the recent-sales dead space before stats. 676 (-12, matching _STATS_W's own
# -12 above) is a width-safety trim, not a margin trim - main_width (676 - rail 52 - stats 100 =
# 524) is IDENTICAL to the 688 step's, so the ~14px button/gap margins from that step carry over
# unchanged; only the stats column and the card's total width actually shrink.
#
# This still can't get all the way down to the ~600-650px "classic safe" email-signature width
# without cutting into the button row itself (see the ~378px bare-button-width number above) or
# shrinking the rail's own icon size (_BADGE_SIZE, untouched this round) - 676 is the real floor
# given everything else the button/photo/rail sizing this file has settled on.
# Original reasoning for 600->720: contact info/CTA buttons used to render as their own full-width
# row below the photo; once they moved beside the photo (matching the Live Preview's layout, see
# _build_gallery), that column only had ~260px at 600px wide, well short of the ~350-380px the
# preview gives it - not enough for all 3 buttons on one line, so they wrapped (2 buttons, then the
# 3rd alone) where the preview never does.
_CARD_WIDTH = 676
# Room for the button row beside the photo, left of the stats column (measured on the rendered card).
_ACTION_ROW_MAX_W = 380

_PHOTO_DISPLAY = 108  # 104 -> 120 -> 108 (explicit ask: 120 read as too large once actually seen -
# 108 instead matches the Live Preview's own .portrait-clip exactly: 114px including its 3px
# border on each side, 114-6=108 for the photo itself - see signaturePreview.ts).
# 6, not the old 12 - this used to be how far a soft gradient halo bled out past the photo on
# every side. render_photo_jpeg dropped that halo entirely (a flat multi-band "glow" only ever
# read as visibly banded/lumpy, never as a real soft glow - see its docstring) in favor of a clean
# accent-color ring drawn directly on the photo's own edge, so this is now just a small breathing
# margin around the photo, not bleed room for anything - shrinking it also shrinks the photo's own
# canvas height, one of several tightening changes that bring the card's overall height down.
_PHOTO_FRAME_PAD = 6
# Smaller than _PHOTO_FRAME_PAD on purpose - with a symmetric pad the photo sat visibly lower than
# the name/title beside it (which starts right at the row's own top with no extra offset). A
# thinner top pad pulls the photo level with it - same fix as the frontend preview's asymmetric
# .portrait-frame margin (see signaturePreview.ts).
_PHOTO_FRAME_PAD_TOP = 0  # as thin as this canvas can make it - moves the photo up as far as
# possible toward level with the name/title beside it (see the frontend's equivalent negative
# .portrait-frame margin in signaturePreview.ts, which can push further since it isn't bounded by
# a fixed raster canvas the way this halo image is).
_PHOTO_FRAME_PAD_LEFT = 0  # same idea as _PHOTO_FRAME_PAD_TOP, applied to the left side - the
# photo's own halo used to sit the same _PHOTO_FRAME_PAD in from the left as it does on the right,
# which left its actual visible content (ring+headshot, past the halo band) starting noticeably
# right of where the Recent Sales card's own left border sits directly below it (explicit ask:
# "have save contact photo section start exactly inline with recent sales section").
_PHOTO_CANVAS_W = _PHOTO_DISPLAY + _PHOTO_FRAME_PAD_LEFT + _PHOTO_FRAME_PAD  # photo.gif's actual rendered width (1x)
_PHOTO_CANVAS_H = _PHOTO_DISPLAY + _PHOTO_FRAME_PAD_TOP + _PHOTO_FRAME_PAD  # ...and height - NOT square
# The photo+identity row's own up/right nudge (explicit, repeated ask: "move the save contact +
# picture above it as an entire unit up a bit and to the right a bit without moving anything
# else") now lives entirely in a `position:relative` offset on that row's own wrapper in
# _build_gallery, NOT as td padding or an outer margin the way earlier attempts did it - both of
# those actually changed the row's real occupied height/flow position, which is exactly what
# dragged the rail/stats columns and the recent-sales bar below it out of sync (twice). A relative
# offset shifts only where the row PAINTS, not the box it occupies in the flow - every sibling
# below it (recent-sales bar, rail, stats) stays exactly where it already was, and there's no
# content_h math to keep in sync any more for this.
# Positive = shifts the row DOWN from its natural flow position, negative = UP (standard CSS
# `top` behavior for position:relative - positive top pushes the box below its normal position).
# 8 (up, via the old template's hardcoded minus sign) -> 4 (up, still) -> -4 (still typed as a
# negative NUMBER, but the template was ALSO changed to stop negating it - so this was still
# rendering as "top:-4px", i.e. still UP, the exact same bug wearing a different number) -> +4
# (the first real downward value this constant has held, to balance the top/bottom gap) -> -2
# (explicit ask, this round: "move up the entire section... a little" - a fresh, smaller nudge
# back toward the top edge, independent of the earlier top/bottom-gap balancing; paint-only, so
# the recent-sales bar/rail/stats below it are untouched either way - see the offset's own
# docstring above).
_IDENTITY_ROW_OFFSET_TOP = -2
_IDENTITY_ROW_OFFSET_LEFT = 6
_PHOTO_RADII = (18, 5, 18, 5)  # tl, tr, br, bl
# The "Save Contact" bar baked into the bottom of the photo itself (see _bake_save_contact_bar) -
# overlapping the headshot instead of a separate HTML row below it, so it adds zero extra card
# height. Spans the photo's FULL width in the agent's own accent color (not a small centered dark
# pill) - the bottom corners pick up the photo's own rounded-corner mask for free, applied by the
# caller right after this draws. Fixed height (not sized to the text) so the real click target - a
# plain HTML image map in _build_gallery, not CSS position:absolute, which classic Outlook's
# Word-based renderer ignores entirely - can be computed independently from this same constant
# instead of the draw function having to report its geometry back.
_SAVE_CONTACT_BAR_H = 19  # 20 -> 19 (5% smaller, explicit ask, paired with the photo itself
# growing 15% above)

_LISTING_PHOTO_DISPLAY_W, _LISTING_PHOTO_DISPLAY_H = 130, 88
# The whole recent-sale/featured-listing strip - photo AND price/address/status/specs - renders
# as ONE flattened image (see render_listing_bar_jpeg/gif), not as a photo plus separate HTML
# text. That's deliberate: this image is fetched live by URL every time the signature is viewed
# (see pick_featured_items' day-based rotation and the module docstring), so baking every part of
# it into that one image is what makes an ALREADY-INSTALLED, years-old, never-touched-again
# signature keep showing whichever listing/sale is current on the agent's Agent Hub - no
# re-copy-paste required. If the text were separate real HTML instead, it would be frozen at
# whatever was true the moment the agent copied the signature, while only the photo silently
# rotated underneath it - a mismatched, actively misleading result.
# 540, matching main_cell's actual available content width with the icon rail AND stats column
# both present (720 card - rail - stats - main_cell's own left/right padding - see _build_gallery's
# own main_width/content-width math). The old 480 was sized for an earlier, narrower card - once
# the card grew to 720px (see _CARD_WIDTH) this stayed capped at 480 and left a growing gap of bare
# card background between the listing/recent-sales strip's own right edge and the stats column
# ("way too much space" between them). Height raised 78->92 - the extra room freed up by baking
# Save Contact into the photo instead of a separate row (see _SAVE_CONTACT_BAR_H) went here, since a
# taller photo strip/mini-sale-card was worth more than a shorter card overall. Recent-sale mode
# uses the exact same w/h so the two never look like different-sized cards.
# Height raised again, 92->104->118, then back down to 108 (matches the Live Preview's own
# .mini-bubble min-height exactly - see signaturePreview.ts) - 118 was part of the same "too wide/
# too tall overall" pass that also grew the photo too far; both came back down together this round.
_LISTING_BAR_DISPLAY_W, _LISTING_BAR_DISPLAY_H = 540, 108
_LISTING_BAR_JPEG_W, _LISTING_BAR_JPEG_H = _LISTING_BAR_DISPLAY_W * _STATIC_SCALE, _LISTING_BAR_DISPLAY_H * _STATIC_SCALE
_LISTING_MARGIN_TOP = 0  # gap above the listing/recent-sales banner - see side_column_content_height.
# 4 -> 2 -> 0 (explicit ask, repeated: "recent sales section can move much higher... small gap from
# it and the buttons" - the bar's own border/background already reads as a clearly separate card
# from the button row above it with zero extra margin, so there's no real need to reserve space
# here just for breathing room.

# main_cell's own top/bottom padding - shared with the icon rail and stats <td>'s own padding (see
# _build_gallery) so all three columns' content starts/ends at the exact same y position instead of
# each guessing a close-but-not-quite-equal number.
_MAIN_CELL_PAD_V = 10
# Bottom padding under the Recent Sales/Featured Listing strip - explicit ask: the icon rail and the
# stats column should end right where Recent Sales ends instead of running on below it, making the
# whole email card taller for nothing. 10 -> 4 (just enough that the strip's own border doesn't sit
# on the card's bottom edge). Every side column is sized off the same row height (see row_h in
# _build_gallery), so this one number is what sets where all three columns end.
_MAIN_CELL_PAD_BOTTOM = 4

# Recent-sale mode: a single bubble with a "RECENT SALES" header and a row of 3 mini photo cards
# (town + price only, SOLD stamp - no beds/baths/specs) at a time, same overall w/h as the featured
# listing bar above. When the agent has more than 3 qualifying sales it scrolls the window by one
# card every _SALE_MINI_STEP_MS, continuously cycling through the whole rotation pool - see
# render_recent_sales_carousel_gif.
_SALE_MINI_COUNT = 3
_SALE_MINI_STEP_MS = 4000
# 1 -> 2 intermediate frames, 90ms -> 70ms each - one single frame was barely a slide at all (an
# almost-instant cut from one settled position to the next); 2 frames at 70ms is a real ~140ms
# glide, still snappy, not sluggish. Tried 3 back then and it pushed one real test file past
# 500KB, but that was against the OLDER, bigger _LISTING_BAR_DISPLAY_W/H (540x108) this bar no
# longer uses - the since-narrowed/shrunk cards (see _SALE_MINI_BAR_DISPLAY_W/H,
# _SALE_MINI_CARD_SHRINK) freed enough of that size budget to afford 3 again (explicit ask: "the
# animation for changing properties is really bad" - a 2-frame, ~140ms glide read as a near-cut;
# measured a real file at 3 frames/220 colors here and it's still comfortably under 500KB).
_SALE_MINI_SLIDE_FRAMES = 3
_SALE_MINI_SLIDE_FRAME_MS = 65
# GIF has no delta/motion compression - every frame of a scrolling photo strip is a brand new full
# encode, so animated frame count is the single biggest lever on file size. Capped independently of
# _SALES_ROTATION_POOL_SIZE (still up to 9 *eligible* sales) so the "how many sales can qualify"
# promise in the settings copy stays accurate even though only this many animate through one loop.
_SALE_MINI_MAX_ANIMATED = 4
_SALE_MINI_PAD_DISPLAY = 8
# The "Recent Sales" header row is gone (explicit ask: "make the photos of the sales larger, remove
# the recent sales [label]... so the photos can actually be more like the featured listing one") -
# the three photos now fill the bubble, each stamped "RECENTLY SOLD" in its top-left corner (see
# _draw_mini_sale_card). 0 here drops the header row from the HTML and hands its height to the
# photos; _LEGACY_SALES_HEADER_H_DISPLAY keeps the standalone header image renderable for
# signatures pasted before this change, whose HTML still requests sales-header.png.
_SALE_MINI_HEADER_H_DISPLAY = 0
_LEGACY_SALES_HEADER_H_DISPLAY = 22  # 22 -> 18 -> 22 (explicit ask: "a tad more space above and below the
# actual 'recent sales' text, moving photos part down a tad" - the text stays centered, so the extra
# 4px splits evenly above/below it; _SALE_MINI_BAR_DISPLAY_H grows by the same 4px so the photos
# keep their exact height and just shift down). Was 22 -> 18 (paired with _SALE_MINI_BAR_DISPLAY_H's own shrink
# below, so the header stays roughly the same share of the bar instead of eating proportionally
# more of a now-shorter card). just the header text - no underline mark any more, the extra
# height it used to reserve goes straight to the mini sale photos below (see _mini_card_geometry),
# which were reading as noticeably wide/short rectangles at the old, shorter card_h.
_SALE_MINI_GAP_DISPLAY = 6

# Recent-sales mode gets its own, NARROWER bar width - not the shared _LISTING_BAR_DISPLAY_W
# featured-listing mode still uses at full width:100%. Explicit ask, repeated a few times: the
# recent-sales card was stretching all the way out to meet the stats column, well past where the
# button row above it naturally ends, instead of ending close to it like the reference design.
# Shrinking the 3 mini-cards 15% (and NOT redistributing the freed width into wider gaps, unlike
# an earlier "15% smaller" pass that got reverted for doing that and reading as broken/uneven) and
# then sizing the bar to exactly what those smaller cards need - instead of forcing them to fill
# the old 540px - naturally makes the whole bar narrower AND left-aligned, without touching
# _CARD_WIDTH/main_width (so the button row's own safe margin is completely unaffected - see
# _CARD_WIDTH's own comment on why THAT can't shrink further).
# 0.85 -> 0.93 (explicit ask: "make the recent sales photo section a little longer... try to match
# the ending of the whats my home worth button with the recent sales" - measured the real button
# row (My Agent Hub/Ready to Buy or Sell/What's My Home Worth at their actual 9.5px/700 font+padding)
# against the photo+gap it sits beside: ~114px photo canvas + 10px gap + ~378px of buttons = ~502px
# from main_cell's own left edge, which is what this bar's own width needs to land on to end at the
# same x as the button row, since both live in the same main_cell and start at the same left edge.
_SALE_MINI_CARD_SHRINK = 0.96  # 0.93 -> 0.96 (explicit ask: "recent sales section has a decent
# amount of dead space between its right side end and start of the stats section... make recent
# sales section a tad bigger" - widens the bar to ~517px, still safely inside main_cell's ~520px
# real content width (see main_width's own comment), without needing to grow _SALE_MINI_BAR_
# DISPLAY_H (which would also grow content_h and, with it, the rail/stats columns - explicitly
# ruled out this round: "not changing any positioning of stats or anything else").
_sale_mini_content_w_full = _LISTING_BAR_DISPLAY_W - _SALE_MINI_PAD_DISPLAY * 2 - _SALE_MINI_GAP_DISPLAY * (_SALE_MINI_COUNT - 1)
_sale_mini_card_w_shrunk = round((_sale_mini_content_w_full // _SALE_MINI_COUNT) * _SALE_MINI_CARD_SHRINK)
_SALE_MINI_BAR_DISPLAY_W = _SALE_MINI_PAD_DISPLAY * 2 + _sale_mini_card_w_shrunk * _SALE_MINI_COUNT + _SALE_MINI_GAP_DISPLAY * (_SALE_MINI_COUNT - 1)
_SALE_MINI_BAR_JPEG_W = _SALE_MINI_BAR_DISPLAY_W * _STATIC_SCALE
# A dedicated, slightly TALLER height for the recent-sales bar only (explicit ask: "a little
# taller") - the featured-listing strip keeps _LISTING_BAR_DISPLAY_H at its own original 108
# unchanged, since this request was specifically about the recent-sales cards/text quality, not
# the single-photo listing banner. _mini_card_geometry's own `scale` (h / _LISTING_BAR_DISPLAY_H)
# still uses 108 as its baseline, so this taller height just scales every mini-card measurement
# (photo, price/town text, padding) up proportionally for free - which also happens to be why the
# price/town text reads sharper again after last round's -15% size cut.
# 128 -> 118 once the header row went: the photos alone now stand about as tall as the featured
# listing's photo (108 - its own margins), which is the look asked for.
_SALE_MINI_BAR_DISPLAY_H = 118  # was 124 -> 128 paired with the header's own +4px (see
# _SALE_MINI_HEADER_H_DISPLAY) so the mini photos keep their height. Was 108 -> 118 -> 92 -> 124 (explicit ask, this round: "the recent
# sales section is way shrunk, should be taller, going all the way down" - the previous 92 came
# from an earlier, since-superseded ask to shrink the rail/stats columns; it also left the email
# version noticeably shorter than the Live Preview's own equivalent card, which is backwards -
# .mini-bubble (signaturePreview.ts) is deliberately TALLER than .gallery-listing there (108 vs
# 94px), not shorter. 124 restores that same ratio on this system's own 108px listing-mode
# baseline (108 * 108/94 = 124.08), so recent-sales mode is proportionally taller than the
# single-listing banner here too, matching the Live Preview instead of undercutting it. The
# rail/stats columns' own height always just matches whatever this bar (plus the photo above it)
# add up to (see side_column_content_height/content_h in _build_gallery), so this single constant
# is also what makes the stats gradient reach the card's real bottom edge again - it was already
# computed off this same number, just off a too-short one. _mini_card_geometry scales every
# measurement inside the bar (pad/gap/photo size/text) off this automatically, via its own
# `scale = h / _LISTING_BAR_DISPLAY_H` - no other constant needs to move in step with this one.
_SALE_MINI_BAR_JPEG_H = _SALE_MINI_BAR_DISPLAY_H * _STATIC_SCALE

TEMPLATES = ["gallery"]

DEFAULT_SIGNATURE_CONFIG: dict[str, Any] = {
    "template": "gallery",
    "fields": {
        "headshot": {"include": True},
        "name": {"include": True},
        "title": {"include": True},
        "company": {"include": True},
        "team": {"include": False, "value": ""},
        "phone": {"include": True},
        "email": {"include": True},
        "website": {"include": True},
        "agentHubUrl": {"include": True},
        # Matches Agent Hub's own social platform set exactly (components/agent-hub/
        # SocialLinksEditor.tsx's defaultSocialLinks) - no LinkedIn, because Agent Hub doesn't
        # manage one; a prior version of this let LinkedIn be typed in manually here, which was
        # exactly the duplicate-data-entry problem this whole feature is supposed to avoid.
        "instagram": {"include": False},
        "facebook": {"include": False},
        "youtube": {"include": False},
        "tiktok": {"include": False},
    },
    "ctaButtons": [],
    # Listings & sales and stats both start on (user, 10/15) - mirrors DEFAULT_SIGNATURE_CONFIG in
    # frontend/lib/signatureConfig.ts. Each only shows once there's something behind it.
    "blockVisibility": {"banner": True},
    "banner": {
        "showFeaturedListing": True,
        "showRecentSales": True,
        "showStats": True,
    },
    "accentColor": DEFAULT_ACCENT,
    "fontFamily": "system",
    # Purely a cosmetic trust badge next to the name (small checkmark), not tied to any real
    # verification system - on by default since it's a pure design accent nobody asked to opt
    # into; the agent can still turn it off explicitly.
    "showVerifiedBadge": True,
    # Customize tab toggle - flips every raster asset AND the install HTML's own table colors to
    # the dark palette (see _theme_colors/theme_card_bg above). Read fresh from this same saved
    # config by every asset endpoint on every fetch, so toggling it applies to an already-installed
    # signature automatically, the same as a listing/sale change - no re-copy-paste.
    "darkMode": False,
}


LUXE_ACCENT = "#C9A24D"

# OpenHousePal Luxe "finish" - the same Luxe look in gold (default), silver or bronze. Mirrors
# frontend/lib/luxeMetals.ts (kept in sync by hand). RGB tuples are what the PIL renderers draw.
LUXE_METALS: dict[str, dict[str, Any]] = {
    "gold": {"base": "#C9A24D", "light": "#E8C97A", "deep": "#8B6A2B", "base_rgb": (201, 162, 77), "light_rgb": (232, 201, 122), "deep_rgb": (139, 106, 43), "pale_rgb": (243, 223, 168),
             "mid_rgb": (168, 128, 58), "wash_light": "#F4EAD7", "wash_dark": "#2A2218", "border_light": "#E3CF9E", "border_dark": "#5A4A2A", "chip_light": (246, 234, 206), "chip_dark": (58, 48, 30)},
    "silver": {"base": "#A9AFB7", "light": "#E3E7EC", "deep": "#6B717A", "base_rgb": (169, 175, 183), "light_rgb": (227, 231, 236), "deep_rgb": (107, 113, 122), "pale_rgb": (245, 247, 249),
               "mid_rgb": (110, 117, 126), "wash_light": "#ECEEF1", "wash_dark": "#1E2228", "border_light": "#D3D8DE", "border_dark": "#4A4F57", "chip_light": (233, 236, 240), "chip_dark": (48, 52, 58)},
    "bronze": {"base": "#A9714B", "light": "#DBA47C", "deep": "#6E4426", "base_rgb": (169, 113, 75), "light_rgb": (219, 164, 124), "deep_rgb": (110, 68, 38), "pale_rgb": (246, 217, 194),
               "mid_rgb": (140, 90, 56), "wash_light": "#F2E2D5", "wash_dark": "#2A1C14", "border_light": "#E2C2A9", "border_dark": "#5A3C28", "chip_light": (244, 226, 212), "chip_dark": (60, 40, 28)},
}


def luxe_metal(name: Any) -> dict[str, Any]:
    return LUXE_METALS.get(name if isinstance(name, str) else "gold", LUXE_METALS["gold"])


def luxe_metal_for_accent(accent_hex: str) -> dict[str, Any]:
    """The metal whose base is this accent (resolve_signature_config sets accentColor to it)."""
    for m in LUXE_METALS.values():
        if m["base"].lower() == (accent_hex or "").lower():
            return m
    return LUXE_METALS["gold"]


def resolve_signature_config(config: Optional[dict]) -> dict[str, Any]:
    """Deep-merges the agent's saved signature config over the defaults so every key the
    renderer/preview relies on is always present."""
    saved = config or {}
    resolved: dict[str, Any] = {**DEFAULT_SIGNATURE_CONFIG, **saved}
    fields: dict[str, Any] = {}
    saved_fields = saved.get("fields") or {}
    for key, default_field in DEFAULT_SIGNATURE_CONFIG["fields"].items():
        fields[key] = {**default_field, **(saved_fields.get(key) or {})}
    resolved["fields"] = fields
    resolved["banner"] = {**DEFAULT_SIGNATURE_CONFIG["banner"], **(saved.get("banner") or {})}
    resolved["blockVisibility"] = {**DEFAULT_SIGNATURE_CONFIG["blockVisibility"], **(saved.get("blockVisibility") or {})}
    ctas = saved.get("ctaButtons")
    resolved["ctaButtons"] = ctas if isinstance(ctas, list) else []
    resolved["showVerifiedBadge"] = bool(saved.get("showVerifiedBadge", True))
    resolved["darkMode"] = bool(saved.get("darkMode", False))
    # OpenHousePal Luxe (Customize tab's premade theme): gold + Georgia, same values the Studio
    # preview locks in (LUXE_ACCENT / LUXE_FONT in the email-signature page).
    resolved["luxe"] = bool(saved.get("luxe", False))
    resolved["luxeMetal"] = saved.get("luxeMetal") if saved.get("luxeMetal") in LUXE_METALS else "gold"
    if resolved["luxe"]:
        resolved["accentColor"] = LUXE_METALS[resolved["luxeMetal"]]["base"]
        resolved["fontFamily"] = "georgia"
    # Only one design exists now - always "gallery", regardless of what an older saved config says.
    resolved["template"] = "gallery"
    return resolved


def _social_url(social_links: list, social_id: str) -> str:
    for link in social_links or []:
        if isinstance(link, dict) and link.get("id") == social_id and link.get("enabled"):
            # a typo like "aw" is not a link - never a dead icon in someone's email
            from app.services.website.content_adapters import public_web_url
            return public_web_url(str(link.get("url") or "")) or ""
    return ""


def _esc(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def is_hex_color(value: str) -> bool:
    value = (value or "").lstrip("#")
    if len(value) != 6:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


def _hex_to_rgb(hex_color: str, fallback: tuple[int, int, int] = (184, 121, 67)) -> tuple[int, int, int]:
    value = (hex_color or "").lstrip("#")
    if len(value) != 6:
        return fallback
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return fallback


def _luminance(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _mix_hex(hex_a: str, hex_b: str, t: float) -> str:
    """Blends two hex colors (t=0 -> hex_a, t=1 -> hex_b) and returns a plain solid hex, not an
    rgba() value - used to build gradient stops and tinted backgrounds as real solid colors so
    classic Outlook (which doesn't reliably render CSS gradients or rgba backgrounds in table
    cells) still gets a sensible flat color instead of a broken/transparent one."""
    a, b = _hex_to_rgb(hex_a), _hex_to_rgb(hex_b)
    mixed = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return "#" + "".join(f"{max(0, min(255, c)):02X}" for c in mixed)


def _contrast_text(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    return (17, 24, 39) if _luminance(rgb) > 140 else (255, 255, 255)


def _card_wash_bg(accent_hex: str, dark: bool = False) -> tuple[str, str]:
    """Two flat hex gradient stops approximating the Live Preview's own `_cardWashBg`
    (signaturePreview.ts) - a neutral wash tinted faintly by the agent's accent color, used for the
    Recent Sales/Featured Listing strip and the icon rail. Real `color-mix()` (what the preview
    uses) isn't safe in email, so this pre-computes the same two blends as plain hex via _mix_hex,
    the same technique _stats_gradient_stops already relies on. Without this both of those blocks fell
    back to a flat, gradient-less tint - a visible step down from the preview's identity."""
    if dark:
        return _mix_hex(accent_hex, "#24262C", 0.84), _mix_hex(accent_hex, "#17181C", 0.94)
    return _mix_hex(accent_hex, "#FFFFFF", 0.88), _mix_hex(accent_hex, "#F6F2EA", 0.96)


def _contrast_text_hex(hex_color: str) -> str:
    r, g, b = _contrast_text(_hex_to_rgb(hex_color))
    return f"#{r:02x}{g:02x}{b:02x}"


def _safe_accent_on_bg_hex(accent_hex: str, bg_hex: str) -> str:
    r, g, b = _safe_accent_on_bg(accent_hex, bg_hex)
    return f"#{r:02x}{g:02x}{b:02x}"


def _safe_accent_on_bg(accent_hex: str, bg_hex: str) -> tuple[int, int, int]:
    """For the accent used directly as a text color against the card's own background (not as a
    filled chip, where _contrast_text already applies) - a white/near-white accent on the light
    theme, or a black/near-black accent on the dark theme, reads as nearly invisible. Swaps to
    solid white or black only in that specific low-contrast case; every normal accent color comes
    back unchanged. Mirrors _safeAccentOnCard in signaturePreview.ts."""
    accent_rgb = _hex_to_rgb(accent_hex)
    bg_rgb = _hex_to_rgb(bg_hex, fallback=(255, 255, 255))
    if abs(_luminance(accent_rgb) - _luminance(bg_rgb)) < 60:
        return (255, 255, 255) if _luminance(bg_rgb) < 128 else (17, 24, 39)
    return accent_rgb


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

def _inter_font(size: float, weight: int):
    from PIL import ImageFont
    font = ImageFont.truetype(_FONT_PATH, max(1, round(size)))
    try:
        font.set_variation_by_axes([_FONT_OPSZ_MAX, weight])
    except Exception:  # noqa: BLE001 - falls back to the font's default instance, still renders
        pass
    return font


def _fmt_money(value) -> str:
    try:
        return f"${int(value):,}"
    except (TypeError, ValueError):
        return ""


def _fmt_compact_money(value) -> str:
    """Same M/K/B convention as the frontend's fmtCompactMoney() - $1,900,000 becomes "$1.9M",
    $1,750,000 becomes "$1.75M" - used for the recent-sale pair cards, which don't have room for
    full-precision digits and (unlike the single-listing bar) never showed them anyway.

    B added (explicit ask) - a total sales volume crossing a billion used to just keep counting in
    M ("$1234.57M" instead of "$1.23B"), which was both a worse read and exactly the kind of long
    string _fit_value_font (see _compose_stats_canvas) has to work the hardest to shrink down to
    fit the pill - shorter notation is a real win on top of that, not just a cosmetic one."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if not v:
        return "$0"
    if v >= 1_000_000_000:
        b = v / 1_000_000_000
        b = b if b == int(b) else round(b * 100) / 100
        return f"${b:g}B"
    if v >= 1_000_000:
        m = v / 1_000_000
        m = m if m == int(m) else round(m * 100) / 100
        return f"${m:g}M"
    if v >= 1_000:
        return f"${round(v / 1000)}K"
    return f"${int(v):,}"


def _fmt_num(value) -> str:
    """3.5 stays "3.5", 4.0 becomes "4", 0.38 stays "0.38" - for beds/baths/acres, which come
    back from Bright MLS as floats even when the "natural" display is an integer."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    return str(int(f)) if f == int(f) else f"{f:g}"


def _fmt_phone(raw: str) -> str:
    """A bare 10-digit US number (or one with a leading "1") becomes "(610) 781-7003" instead of
    a hard-to-scan run of digits - anything else (extension, international, already-punctuated)
    is left exactly as the agent entered it rather than mangled by a formatter that doesn't
    understand its shape."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) != 10:
        return raw
    return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"


# ---------------------------------------------------------------------------
# Sales / listings data
# ---------------------------------------------------------------------------

def _clean_status(item: dict) -> str:
    return str(item.get("status") or "").replace("-BRIGHT", "").replace("_", " ").strip().lower()


def _eligible(items: list, status_check, price_floor: int = 0, require_visible: bool = True) -> list:
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        status = _clean_status(item)
        if not status_check(status):
            continue
        if require_visible and not item.get("visible", True):
            continue
        if price_floor and int(item.get("price") or 0) < price_floor:
            continue
        cleaned.append(item)
    return cleaned


# "closed" counts as sold (matches formatSaleStatus() in lib/agentSales.ts) and a $100k floor
# screens out stray placeholder/incomplete records - same rules the public Agent Hub page itself
# uses, so nothing the signature shows can disagree with what's live there.
_is_sold_status = lambda s: "sold" in s or "closed" in s
_SOLD_PRICE_FLOOR = 100_000
_SALES_ROTATION_POOL_SIZE = 9

_EPOCH = date(1970, 1, 1)


def _epoch_day_index() -> int:
    """Days since the Unix epoch in UTC - the same formula lib/agentSales.ts' epochDayIndex()
    uses (Math.floor(Date.now() / 86400000), which is always UTC), instead of Python's own
    toordinal() (days since 0001-01-01) or date.today() (the server's LOCAL date). Using the
    server's local date here would drift a few hours out of sync with the frontend's UTC-based
    index once a day (whenever local and UTC disagree on the calendar date), which would show a
    different item in the Studio live preview than the actual backend-rendered signature image
    during that window - anchoring both to UTC keeps them always in agreement."""
    return (datetime.now(timezone.utc).date() - _EPOCH).days


def _sales_rotation_pool(sales: list, min_price: int = 0) -> list:
    """The most recent N sales that are shown AND meet the agent's optional minimum-price filter
    (Recent Sales settings, on both Settings and Studio > Site) - mirrors
    lib/agentSales.ts' getSalesRotationPool() so an agent with a long tail of old/low sales never
    has those crowd out what should actually rotate through the signature."""
    eligible = _eligible(sales or [], _is_sold_status, max(_SOLD_PRICE_FLOOR, min_price))
    eligible.sort(key=lambda s: str(s.get("sold_date") or ""), reverse=True)
    return eligible[:_SALES_ROTATION_POOL_SIZE]


def pick_featured_items(agent_hub_config: dict, show_listing: bool, show_sales: bool) -> Optional[tuple[str, list[dict]]]:
    """Picks what the banner shows today, with the "smart" fallback the Studio checkboxes
    describe: an active listing wins when the agent has one (and asked to show listings) -
    exactly one, in the full listing-bar layout (photo + status + address + price + specs).
    Recent sales fill in only when there's currently nothing for sale, or the agent only opted
    into sales - the full rotation pool (see _sales_rotation_pool), which
    render_recent_sales_carousel_gif windows through 3 at a time, scrolling one card every few
    seconds so the whole pool gets seen over time.

    Returns (mode, items) where mode is "listing" or "sale", or None if nothing is eligible.
    Rotating by day index (not a stored index) means WHICH item(s) are in play is deterministic
    and stateless - every fetch on the same calendar day starts from the same item(s), advancing
    at UTC midnight, without a scheduled job or extra DB row. Which sale is actually on-screen at
    any given moment within a "sale" day additionally cycles continuously within the GIF itself."""
    pool = featured_pool(agent_hub_config, show_listing, show_sales)
    if pool and pool[0] == "listing":
        listings = pool[1]
        return ("listing", [listings[_epoch_day_index() % len(listings)]])
    # 1-2 sales: the featured-listing strip (one still, or two flipping) - see render_sale_feature.
    if pool and pool[0] == "sale" and len(pool[1]) < _SALE_MINI_COUNT:
        return ("sale_feature", pool[1])
    return pool


def featured_pool(agent_hub_config: dict, show_listing: bool, show_sales: bool) -> Optional[tuple[str, list[dict]]]:
    """Everything pick_featured_items could show, before the day-of-rotation pick - i.e. what the
    banner's CONTENT is, independent of which day it is (the re-copy fingerprint uses this, so a
    daily rotation alone never looks like an edit)."""
    hub = agent_hub_config or {}
    # Same buckets as formatSaleStatus() in lib/agentSales.ts - "active under contract" counts too.
    listings = _eligible(hub.get("myListings") or [], lambda s: any(k in s for k in ("active", "pending", "under contract", "coming soon"))) if show_listing else []
    if listings:
        return ("listing", listings)
    min_price = int(hub.get("salesMinPrice") or 0)
    pool = _sales_rotation_pool(hub.get("recentSales") or [], min_price) if show_sales else []
    if not pool:
        return None
    # "Recently sold" stamps only under $50M in volume (services/sales_wording) - carried on each
    # card so every renderer (and the image fingerprints, which hash the items) follows it.
    from app.services.sales_wording import sales_are_recent
    recent = sales_are_recent((compute_sales_stats(hub) or {}).get("totalVolume"))
    pool = [{**s, "_recent": recent} for s in pool]
    # Full pool (not sliced) - render_recent_sales_carousel_gif/jpeg windows through it itself
    # (always showing 3 at a time, scrolling one card every few seconds), so the whole qualifying
    # set gets seen over time instead of only ever the first 2-3.
    return ("sale", pool)


def compute_sales_stats(agent_hub_config: dict) -> Optional[dict]:
    """Mirrors the frontend's getSaleSummary() (lib/agentSales.ts) so the signature's stats
    column always agrees with what the agent's own Agent Hub shows - including the $100k sold
    price floor, and (deliberately) not re-filtered by `.visible`, since the Agent Hub's own
    stats total counts every qualifying sale, not just the ones toggled on for its carousel."""
    hub = agent_hub_config or {}
    sales = _eligible(hub.get("recentSales") or [], _is_sold_status, _SOLD_PRICE_FLOOR, require_visible=False)
    total_sold = len(sales)
    if not total_sold:
        return None
    total_volume = sum(int(s.get("price") or 0) for s in sales if str(s.get("price") or "").strip())
    avg_price = round(total_volume / total_sold) if total_sold else 0
    return {"totalSold": total_sold, "totalVolume": total_volume, "avgPrice": avg_price}


# ---------------------------------------------------------------------------
# GIF assembly
# ---------------------------------------------------------------------------

def _ease_settle(t: float) -> float:
    """Cubic ease-out: fast start, gentle stop at exactly 1.0 when t=1. Used for every one-shot
    entrance so the LAST frame always lands precisely on the "ideal" resting composition, with
    motion decelerating into it."""
    return 1 - (1 - t) ** 3


def _frames_to_gif(frames: list, duration_ms: int = 90, hold_ms: Optional[int] = None, colors: int = 200, dither: bool = True) -> bytes:
    """Quantizes every frame against ONE shared palette (built from all frames stacked together)
    before saving, instead of letting Pillow's GIF writer quantize each frame independently -
    independent per-frame palettes cause visible color flicker on soft gradients/text edges
    between frames, which a shared palette avoids.

    No `loop` kwarg is ever passed to Pillow's save() - omitting it (Pillow's default) means no
    Netscape loop extension gets written, so every GIF here plays its entrance ONCE and then holds
    on the last frame, rather than repeating forever. `hold_ms` optionally gives the final frame a
    longer duration so the settled state doesn't feel like it "just stopped".

    `colors`/`dither` trade quality for file size - GIF has no inter-frame delta compression, so a
    photo-heavy animation is size-sensitive in a way a mostly-flat asset (a badge) isn't; callers
    with real photo content pass fewer colors and disable dithering, since Floyd-Steinberg noise is
    close to incompressible under GIF's LZW.
    """
    from PIL import Image

    w, h = frames[0].size
    strip = Image.new("RGB", (w, h * len(frames)))
    for i, frame in enumerate(frames):
        strip.paste(frame.convert("RGB"), (0, i * h))
    shared_palette = strip.convert("P", palette=Image.ADAPTIVE, colors=colors)
    dither_mode = Image.FLOYDSTEINBERG if dither else Image.NONE
    quantized = [f.convert("RGB").quantize(palette=shared_palette, dither=dither_mode) for f in frames]
    durations = [duration_ms] * len(quantized)
    if hold_ms:
        durations[-1] = hold_ms
    buffer = BytesIO()
    quantized[0].save(
        buffer, format="GIF", save_all=True, append_images=quantized[1:],
        duration=durations, disposal=1, optimize=True,
    )
    return buffer.getvalue()


def _frames_to_looping_gif(frames: list, durations: list[int], colors: int = 200, dither: bool = True) -> bytes:
    """Sibling of _frames_to_gif for a genuinely repeating animation (the recent-sales carousel) -
    takes an explicit per-frame duration (not one uniform value) and passes `loop=0`, the one place
    in this module that writes the Netscape loop extension, so playback repeats forever instead of
    playing once and holding."""
    from PIL import Image

    w, h = frames[0].size
    strip = Image.new("RGB", (w, h * len(frames)))
    for i, frame in enumerate(frames):
        strip.paste(frame.convert("RGB"), (0, i * h))
    shared_palette = strip.convert("P", palette=Image.ADAPTIVE, colors=colors)
    dither_mode = Image.FLOYDSTEINBERG if dither else Image.NONE
    quantized = [f.convert("RGB").quantize(palette=shared_palette, dither=dither_mode) for f in frames]
    buffer = BytesIO()
    quantized[0].save(
        buffer, format="GIF", save_all=True, append_images=quantized[1:],
        duration=durations, disposal=2, optimize=True, loop=0,
    )
    return buffer.getvalue()


async def _fetch_image(url: str):
    from PIL import Image
    import httpx

    # Media saved as a relative path ("/uploads/...") is served by this API - resolve it like the
    # frontend's resolveMediaUrl. Anything still unreachable (a frontend-only placeholder such as
    # "/home.jpg", a dead link, a timeout) falls back to the initials instead of a 500 that leaves
    # a broken picture in the agent's signature.
    url = (url or "").strip()
    if url.startswith("/") and not url.startswith("//"):
        base = (os.getenv("API_URL") or "").rstrip("/")
        if not base:
            return None
        url = base + url
    if not url.startswith(("http://", "https://")):
        return None
    try:
        from app.utils.safe_fetch import safe_get
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            response = await safe_get(client, url)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        return Image.open(BytesIO(response.content)).convert("RGB")
    except Exception:  # noqa: BLE001 - a corrupt/unsupported fetch just falls back to the initial
        return None


def _cover_crop(img, target_w: int, target_h: int, focal: tuple[float, float] | None = None):
    """`focal` is (fx, fy) in [0, 1] - same semantics as CSS object-position (0.5, 0.5 = centered,
    matching the old always-centered behavior when omitted). fy=0 keeps the TOP of the source
    image, fy=1 keeps the BOTTOM - lets a caller honor a photo's real focal point (e.g. Agent Hub's
    own photo cropper) instead of blindly cropping dead-center, which can cut off the top of the
    subject's head on a photo where the face sits higher up than that."""
    from PIL import Image
    fx, fy = focal if focal else (0.5, 0.5)
    src_ratio = img.width / img.height
    dst_ratio = target_w / target_h
    if src_ratio > dst_ratio:
        new_w = int(img.height * dst_ratio)
        left = round((img.width - new_w) * fx)
        img = img.crop((left, 0, left + new_w, img.height))
    else:
        new_h = int(img.width / dst_ratio)
        top = round((img.height - new_h) * fy)
        img = img.crop((0, top, img.width, top + new_h))
    return img.resize((target_w, target_h), Image.LANCZOS)


def _initials_avatar(name: str, size: int, accent_rgb: tuple[int, int, int]):
    from PIL import Image, ImageDraw
    canvas = Image.new("RGB", (size, size), accent_rgb)
    draw = ImageDraw.Draw(canvas)
    initials = "".join(p[0].upper() for p in (name or "").split()[:2]) or "A"
    font = _inter_font(size * 0.4, 700)
    bbox = draw.textbbox((0, 0), initials, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), initials, font=font, fill=_contrast_text(accent_rgb))
    return canvas


def _rounded_corner_mask(w: int, h: int, tl: int, tr: int, br: int, bl: int):
    """A fill mask with an independent corner radius per corner (Pillow's rounded_rectangle only
    takes one radius for all four) - built by blackening each corner's bounding square, then
    drawing a white quarter-circle pieslice back into it. Matches the reference design's
    alternating-radius photo frame (one crisp corner reads as more editorial than a uniform
    rounded square)."""
    from PIL import Image, ImageDraw
    mask = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(mask)
    if tl > 0:
        draw.rectangle((0, 0, tl, tl), fill=0)
        draw.pieslice((0, 0, tl * 2, tl * 2), 180, 270, fill=255)
    if tr > 0:
        draw.rectangle((w - tr, 0, w, tr), fill=0)
        draw.pieslice((w - tr * 2, 0, w, tr * 2), 270, 360, fill=255)
    if br > 0:
        draw.rectangle((w - br, h - br, w, h), fill=0)
        draw.pieslice((w - br * 2, h - br * 2, w, h), 0, 90, fill=255)
    if bl > 0:
        draw.rectangle((0, h - bl, bl, h), fill=0)
        draw.pieslice((0, h - bl * 2, bl * 2, h), 90, 180, fill=255)
    return mask


_SAVE_CONTACT_LABEL = "SAVE CONTACT"


def _draw_tracked_text(draw, xy: tuple[float, float], text: str, font, fill, tracking: float = 0) -> None:
    """draw.text() has no letter-spacing of its own - at this label's small raster size, PIL's
    default (zero) tracking packed the bold caps together tightly enough to read as a blurry smudge
    rather than a clean word. Draws one character at a time, advancing by that glyph's own measured
    width plus `tracking` px, instead of relying on the font's built-in (cramped) advance widths."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def _bake_save_contact_bar(cropped, w: int, h: int, accent_rgb: tuple[int, int, int], scale: float = 1) -> None:
    """Draws a full-width bar in the agent's own accent color with a plain white 'SAVE CONTACT'
    label (a black pixel outline was tried here and read as messy at this size, so this is back to
    a flat white fill, no stroke), across the bottom of the already-cropped headshot, BEFORE the
    rounded-corner photo mask gets applied in the caller - so the mask clips its bottom corners
    exactly like the photo itself, with no separate HTML row (and so no extra card height) needed.
    Plain rectangle, not a pill - it's meant to read as spanning the entire photo width. Fixed
    height (_SAVE_CONTACT_BAR_H), not sized to the text, so the real click target - a plain HTML
    image map over this exact rect (see _build_gallery) - can be computed from the same constant
    without this function having to report its geometry back. <map>/<area> is old enough HTML to
    still work in classic Outlook's Word-based renderer, unlike CSS position:absolute.

    `scale` lets a caller draw this onto a canvas rendered at N times the normal display resolution
    (see render_photo_jpeg) while keeping the bar/text proportioned exactly like the 1x GIF version
    - every absolute constant here (bar height, font size, tracking) is a 1x-display measurement,
    so a caller working in 2x canvas pixels must scale them up by the same factor or the bar would
    render at half its intended relative size."""
    from PIL import Image, ImageDraw
    bh = round(_SAVE_CONTACT_BAR_H * scale)
    py1 = h - bh
    tracking = 1.1 * scale

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    # A subtle top-to-bottom gradient (lighter -> accent -> darker), not a flat fill - reads as a
    # deliberately polished metal/glass pill instead of a plain color block, matching the true-
    # metal accent swatches' own "polished" look and the live preview's identical treatment
    # (see vcardBg in signaturePreview.ts).
    light_rgb = tuple(min(255, round(c + (255 - c) * 0.22)) for c in accent_rgb)
    dark_edge_rgb = tuple(round(c * 0.82) for c in accent_rgb)
    for row in range(bh):
        t = row / max(1, bh - 1)
        if t < 0.5:
            mix = tuple(round(light_rgb[i] + (accent_rgb[i] - light_rgb[i]) * (t / 0.5)) for i in range(3))
        else:
            mix = tuple(round(accent_rgb[i] + (dark_edge_rgb[i] - accent_rgb[i]) * ((t - 0.5) / 0.5)) for i in range(3))
        odraw.line((0, py1 + row, w, py1 + row), fill=(*mix, 235))
    # A single brighter "glass" highlight line right at the bar's own top edge - the "elevated"
    # look for this control now that the (never-quite-working) animated shine sweep is gone,
    # reading as a raised, glossy pill at rest instead.
    glass_rgb = tuple(min(255, round(c + (255 - c) * 0.55)) for c in accent_rgb)
    odraw.line((0, py1, w, py1), fill=(*glass_rgb, 210))
    # Bumped from 9 - a bit more prominent, closer to how bold the buttons/My Agent Hub read. Still
    # a different literal typeface than those (this is baked-in Inter, a raster image - the buttons
    # are real HTML text rendered in the recipient's own system UI font, e.g. Segoe UI/San Francisco
    # - the two can never be pixel-identical, but matching size/weight keeps them reading as the
    # same family of control.
    font = _inter_font(9.025 * scale, 700)  # 10 -> 9.5 -> 9.025 (5% smaller again, explicit ask)
    char_widths = [odraw.textlength(ch, font=font) for ch in _SAVE_CONTACT_LABEL]
    tw = sum(char_widths) + tracking * (len(_SAVE_CONTACT_LABEL) - 1)
    bbox = odraw.textbbox((0, 0), _SAVE_CONTACT_LABEL, font=font)
    th = bbox[3] - bbox[1]
    tx = (w - tw) / 2
    ty = py1 + (bh - th) / 2 - bbox[1]
    # Computed per-accent (_contrast_text), not hardcoded white - a genuinely light/white accent
    # made white label text nearly invisible against its own bar (real legibility bug). A black drop
    # shadow only makes sense as a lift for WHITE text on a colored bar - black text on a light bar
    # already has its own natural contrast, so the shadow (still just a single soft pass, not the
    # stats pills' full double-shadow - a black pixel OUTLINE was tried here before and read as
    # messy at this small a size, see this function's own docstring) only draws in that case.
    text_rgb = _contrast_text(accent_rgb)
    if text_rgb == (255, 255, 255):
        from PIL import ImageFilter
        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        _draw_tracked_text(ImageDraw.Draw(shadow), (tx, ty + round(1 * scale)), _SAVE_CONTACT_LABEL, font, (0, 0, 0, 145), tracking=tracking)
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(1, round(scale))))
        overlay = Image.alpha_composite(overlay, shadow)
    odraw = ImageDraw.Draw(overlay)
    _draw_tracked_text(odraw, (tx, ty), _SAVE_CONTACT_LABEL, font, (*text_rgb, 255), tracking=tracking)

    composited = Image.alpha_composite(cropped.convert("RGBA"), overlay).convert("RGB")
    cropped.paste(composited)


async def render_photo_gif(photo_url: Optional[str], agent_name: str, accent_hex: str, card_bg_hex: str, photo_position: Optional[dict] = None) -> bytes:
    """A settle of the agent's headshot into a squared, alternating-corner-radius frame, sitting on
    a soft layered accent-tinted halo baked in behind it (the reference design's decorative "photo
    frame" motif - a real ::before/::after CSS effect there, so it has to be flattened into the
    raster here). A "Save Contact" bar is baked into the bottom of the photo itself (see
    _bake_save_contact_bar) rather than added as a separate element below it, so the badge overlaps
    the photo with zero extra height. `card_bg_hex` fills/tints the halo, since GIF has no reliable
    alpha transparency - so this needs the exact background color it sits on, same reasoning as
    render_badge_gif.

    The photo, its ring, the halo, and the Save Contact bar are composited ONCE into one static
    "unit" - the animation then slides that WHOLE unit side to side as one rigid piece (built once
    padded out horizontally, then a shifting crop of it per frame), rather than panning the photo
    independently inside a frame that stays still. That old approach (a Ken-Burns pan on just the
    photo, cropped inside a static ring+halo) read as the picture sliding around loose inside a
    fixed window, not the photo and its outline moving together as one piece - the ask was for the
    whole thing, ring included, to drift together, with the picture itself never moving relative to
    its own shell (matching how the Customize tab's drag-photo control works: only the crop/focal
    point ever changes, never an internal pan). No scale/zoom either (a zoomed version of this same
    whole-unit approach was tried first and replaced with this plain horizontal slide instead).

    There is no way for HTML to detect "does this mailbox support animated GIF" and serve different
    content - no such capability query exists in email. The motion is a ONE-SHOT entrance (settles
    and holds, never loops - see the module docstring). Frame 0 is the same slightly-offset start
    the whole animation eases FROM (matching the original design's own tolerance here - frame 0 was
    never meant to be pixel-identical to the settled state, just close to it), which is what classic
    desktop Outlook's Word-based renderer permanently freezes on, since it never advances past the
    first GIF frame - it still reads as a complete, intentional composition. Clients that DO animate
    get the full settle, ending on frame n-1 (held via `hold_ms`), which - being an undrifted crop
    of the static unit - lines up exactly with the fixed image-map click rect _build_gallery
    computes for the Save Contact bar.

    `photo_position` is Agent Hub's own saved photo focal point ({"x":.., "y":..}, 0-100, from the
    Settings photo cropper) - without it this would always center-crop the source photo, which can
    cut off the top of the agent's head on a photo where the face sits higher up than dead-center
    (the same photo the Agent Hub page itself crops correctly via this exact value)."""
    from PIL import Image, ImageDraw, ImageChops

    accent_rgb = _hex_to_rgb(accent_hex)
    card_bg_rgb = _hex_to_rgb(card_bg_hex, fallback=(255, 255, 255))
    size = _PHOTO_DISPLAY
    focal = None
    if isinstance(photo_position, dict) and isinstance(photo_position.get("x"), (int, float)) and isinstance(photo_position.get("y"), (int, float)):
        focal = (photo_position["x"] / 100, photo_position["y"] / 100)
    raw = (await _fetch_image(photo_url)) if photo_url else None
    # A single, exact-size focal crop - no oversize/pan slack needed any more, since the photo no
    # longer pans independently inside its frame (see the docstring above).
    if raw is None:
        photo = _initials_avatar(agent_name, size, accent_rgb)
    else:
        photo = _cover_crop(raw, size, size, focal=focal)

    photo_mask = _rounded_corner_mask(size, size, *_PHOTO_RADII)
    offset = _PHOTO_FRAME_PAD  # right/bottom
    offset_left = _PHOTO_FRAME_PAD_LEFT
    offset_top = _PHOTO_FRAME_PAD_TOP
    frame_radius = 26

    # The accent OUTLINE ring - drawn INSET, directly onto the photo pixels themselves (mask
    # difference of the photo's own outer corner-mask and a slightly smaller inner one), not
    # outset on the halo at an offset past the photo's edge. An outset ring needs halo padding on
    # every side to have room to be drawn without clipping, but the top pad is intentionally almost
    # zero (see _PHOTO_FRAME_PAD_TOP) so the photo can sit as high as possible - an outset ring
    # would get clipped clean off at the top. Inset, the ring sits flush against the photo's actual
    # edge on all 4 sides regardless of how thin the surrounding halo padding is, matching the live
    # preview's identical real-border treatment (see .portrait-clip in signaturePreview.ts).
    ring_width = 3
    inner_radii = tuple(max(0, r - ring_width) for r in _PHOTO_RADII)
    inner_mask = Image.new("L", (size, size), 0)
    inner_mask.paste(_rounded_corner_mask(size - ring_width * 2, size - ring_width * 2, *inner_radii), (ring_width, ring_width))
    ring_mask = ImageChops.subtract(photo_mask, inner_mask)
    ring_layer = Image.new("RGB", (size, size), accent_rgb)
    photo.paste(ring_layer, (0, 0), ring_mask)
    _bake_save_contact_bar(photo, size, size, accent_rgb)

    # 3 concentric flat-color bands blending from the card background at the outer edge up to a
    # noticeably warmer accent tint right behind the photo. Flat bands, not a real per-pixel
    # gradient - GIF has no delta compression, so a smooth gradient would add real weight to every
    # single frame; flat bands read as an intentional soft glow and cost almost nothing extra to
    # encode. 6 bands, not 3 - the coarser 3-band version had visible hard steps between rings
    # instead of a smooth-reading glow (this is cheap either way, it's still just a handful of flat
    # polygons, not photographic detail, so more bands cost effectively nothing in file size).
    # Canvas is NOT square (see _PHOTO_CANVAS_W/H) - the top pad is thinner than the other 3 sides
    # so the photo sits level with the name/title beside it instead of visibly lower (see
    # _PHOTO_FRAME_PAD_TOP's comment).
    unit = Image.new("RGB", (_PHOTO_CANVAS_W, _PHOTO_CANVAS_H), card_bg_rgb)
    udraw = ImageDraw.Draw(unit)
    bands = 6
    for i in range(bands):
        inset = round(offset * (1 - i / bands))
        inset_left = round(offset_left * (1 - i / bands))
        inset_top = round(offset_top * (1 - i / bands))
        band_hex = _mix_hex(card_bg_hex, accent_hex, 0.16 + 0.24 * ((i + 1) / bands))
        udraw.rounded_rectangle(
            (inset_left, inset_top, _PHOTO_CANVAS_W - inset, _PHOTO_CANVAS_H - inset),
            radius=frame_radius, fill=_hex_to_rgb(band_hex),
        )
    unit.paste(photo, (offset_left, offset_top), photo_mask)

    # The whole unit (halo + ring + photo + Save Contact bar, now permanently fixed relative to
    # each other) gets padded out sideways once, then each frame crops a shifting window back out
    # of that wider version - the raster equivalent of a CSS `transform: translateX()` side-to-side
    # drift on the whole element (no scale/zoom - a zoom version was tried here and the explicit
    # follow-up ask was to replace it with a slow side-to-side slide instead). Only extra WIDTH is
    # added (no extra height) since the motion is horizontal only.
    pan_pad = 1.05
    big_w, big_h = round(_PHOTO_CANVAS_W * pan_pad), _PHOTO_CANVAS_H
    big = unit.resize((big_w, big_h), Image.LANCZOS)
    max_drift_x = (big_w - _PHOTO_CANVAS_W) / 2

    frames = []
    n = 6
    for i in range(n):
        t = _ease_settle(i / (n - 1))
        # Starts offset to one side, eases to dead center (the exact static unit) by the last,
        # held frame - a one-shot slide into place rather than the live preview's own continuous
        # back-and-forth loop, since a GIF here never loops (see _frames_to_gif's docstring).
        drift_x = (1 - t) * max_drift_x
        left = round((big_w - _PHOTO_CANVAS_W) / 2 - drift_x)
        left = max(0, min(big_w - _PHOTO_CANVAS_W, left))
        frame = big.crop((left, 0, left + _PHOTO_CANVAS_W, _PHOTO_CANVAS_H))
        frames.append(frame)
    # 220 colors, not 110 - a real photograph (skin tones, soft background blur) quantized that
    # hard with no dithering was visibly banding/posterizing instead of reading as a normal photo,
    # a big part of why this looked noticeably flatter than the CSS-rendered Live Preview. Well
    # under GIF's 256-color hard limit, and this is a single photo across only 6 frames sharing one
    # palette, not a size-sensitive multi-photo asset like the sales carousel below.
    return _frames_to_gif(frames, duration_ms=110, hold_ms=2200, colors=220, dither=False)


_PHOTO_JPEG_SCALE = 2  # retina - see render_photo_jpeg
_PHOTO_SUPERSAMPLE = 3  # draw-big-then-downsample factor for the mask/ring - see render_photo_jpeg


async def _compose_photo_unit(photo_url: Optional[str], agent_name: str, accent_hex: str, card_bg_hex: str, photo_position: Optional[dict] = None, transparent: bool = False):
    """The static, settled composition render_photo_gif's animation eases INTO - same crop and
    baked-in Save Contact bar, just without the entrance motion and WITHOUT that GIF's soft
    gradient halo behind the photo. That halo was a handful of flat color bands (GIF's 256-color
    palette can't hold a real gradient - see render_photo_gif's own comment on this), and even
    smoothed out to many more/thinner bands here, it still read as visibly lumpy/uneven rather than
    a clean soft glow, not something more bands actually fixes. A plain accent-color ring drawn
    directly on the photo's own rounded edge (no separate halo layer at all) is what the reference
    design's own border treatment actually looks like - see .portrait-clip's real `border` in
    signaturePreview.ts, not a glow effect. This also renders at _PHOTO_JPEG_SCALE (2x, retina) -
    render_photo_gif deliberately stays at 1x since a retina animated GIF has no delta compression
    and gets expensive fast; a single static JPEG pays no such cost.

    The rounded-corner mask and the ring are both drawn at _PHOTO_SUPERSAMPLE times this target
    resolution, then downsampled with LANCZOS before compositing - same reasoning as
    render_badge_png's supersampling. Pillow's mask/shape drawing has no anti-aliasing of its own,
    so the accent ring (the photo's ONLY frame element now that the halo's gone - see above) came
    out with visibly jagged, stair-stepped corners at 2x alone. Downsampling a supersampled MASK is
    what actually smooths it - the mask's own edge pixels become partial/gray values instead of a
    hard black-or-white cutoff, which is exactly what a real anti-aliased renderer would have
    produced directly."""
    from PIL import Image, ImageChops

    scale = _PHOTO_JPEG_SCALE
    super_scale = scale * _PHOTO_SUPERSAMPLE
    accent_rgb = _hex_to_rgb(accent_hex)
    card_bg_rgb = _hex_to_rgb(card_bg_hex, fallback=(255, 255, 255))
    true_size = round(_PHOTO_DISPLAY * scale)
    size = round(_PHOTO_DISPLAY * super_scale)
    focal = None
    if isinstance(photo_position, dict) and isinstance(photo_position.get("x"), (int, float)) and isinstance(photo_position.get("y"), (int, float)):
        focal = (photo_position["x"] / 100, photo_position["y"] / 100)
    raw = (await _fetch_image(photo_url)) if photo_url else None
    if raw is None:
        photo = _initials_avatar(agent_name, size, accent_rgb)
    else:
        photo = _cover_crop(raw, size, size, focal=focal)

    radii = tuple(round(r * super_scale) for r in _PHOTO_RADII)
    photo_mask = _rounded_corner_mask(size, size, *radii)

    # Inset ring (same technique as render_photo_gif - see that function's comment for why it's
    # drawn INSET onto the photo itself rather than outset past its edge): a bit thicker than the
    # GIF's 3px (4px) now that this is the photo's ONLY frame element, not one layer among a ring +
    # halo + card border, so it needs to read clearly on its own.
    ring_width = round(4 * super_scale)
    inner_radii = tuple(max(0, r - ring_width) for r in radii)
    inner_mask = Image.new("L", (size, size), 0)
    inner_mask.paste(_rounded_corner_mask(size - ring_width * 2, size - ring_width * 2, *inner_radii), (ring_width, ring_width))
    ring_mask = ImageChops.subtract(photo_mask, inner_mask)
    ring_layer = Image.new("RGB", (size, size), accent_rgb)
    photo.paste(ring_layer, (0, 0), ring_mask)
    _bake_save_contact_bar(photo, size, size, accent_rgb, scale=super_scale)

    # Downsample the supersampled photo+ring+bar AND its mask together, before the final paste -
    # resizing the mask is what turns its hard-edged corners into smooth, anti-aliased ones.
    photo = photo.resize((true_size, true_size), Image.LANCZOS)
    photo_mask = photo_mask.resize((true_size, true_size), Image.LANCZOS)

    # Flat card-background canvas, not a gradient halo - see the docstring above. _PHOTO_FRAME_PAD
    # is now just a small breathing margin around the photo, not bleed room for a glow.
    offset_left = round(_PHOTO_FRAME_PAD_LEFT * scale)
    offset_top = round(_PHOTO_FRAME_PAD_TOP * scale)
    canvas_w, canvas_h = round(_PHOTO_CANVAS_W * scale), round(_PHOTO_CANVAS_H * scale)
    # transparent=True (photo.png, what the install HTML uses): the margin around the rounded photo
    # is see-through instead of card-colored, so when a mail app's dark mode re-colors the card
    # there's no light box/"white outline" left around the photo (owner, 10/24). On the normal card
    # it looks exactly the same as the flat version.
    unit = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0)) if transparent else Image.new("RGB", (canvas_w, canvas_h), card_bg_rgb)
    unit.paste(photo.convert("RGBA") if transparent else photo, (offset_left, offset_top), photo_mask)
    return unit


async def render_photo_jpeg(photo_url: Optional[str], agent_name: str, accent_hex: str, card_bg_hex: str, photo_position: Optional[dict] = None) -> bytes:
    """JPEG encode of _compose_photo_unit - the universal fallback inside the <picture> tag
    _build_gallery now wraps the photo in (see render_photo_webp), so every mail client that
    doesn't understand <picture>/WebP still gets this exact image, unchanged from before.

    subsampling=0 (4:4:4, full color resolution) - JPEG's default chroma subsampling (4:2:0)
    throws away half the color detail on every edge, which is exactly what was making the Save
    Contact bar's white text on a colored background look soft/ringed even though it's already
    drawn supersampled-then-downsampled above (same root cause the stats pills had, before those
    moved to lossless PNG - the whole photo can't do that here without also blowing up the actual
    headshot's own file size, but keeping full chroma resolution gets most of the same fix)."""
    unit = await _compose_photo_unit(photo_url, agent_name, accent_hex, card_bg_hex, photo_position)
    buffer = BytesIO()
    unit.convert("RGB").save(buffer, format="JPEG", quality=92, optimize=True, subsampling=0)
    return buffer.getvalue()


async def render_photo_png(photo_url: Optional[str], agent_name: str, accent_hex: str, card_bg_hex: str, photo_position: Optional[dict] = None) -> bytes:
    """Dark-mode-proof photo for the install HTML: the same composition as render_photo_jpeg, but
    with a see-through margin (see _compose_photo_unit's `transparent`). Gmail/Outlook dark mode
    re-color the card behind it but never touch images, so a flat card-colored margin baked into
    the picture showed up as a light frame around the photo."""
    unit = await _compose_photo_unit(photo_url, agent_name, accent_hex, card_bg_hex, photo_position, transparent=True)
    buffer = BytesIO()
    unit.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


async def render_photo_webp(photo_url: Optional[str], agent_name: str, accent_hex: str, card_bg_hex: str, photo_position: Optional[dict] = None) -> bytes:
    """WebP encode of the exact same _compose_photo_unit composition render_photo_jpeg uses -
    same crop, same ring, same baked-in Save Contact bar, just a smaller/higher-quality file for
    the clients that support it (see the <picture> wrapper in _build_gallery). WebP has no chroma
    subsampling to fight the way JPEG does, so this doesn't need render_photo_jpeg's own
    subsampling=0 workaround to keep the Save Contact bar's text crisp."""
    unit = await _compose_photo_unit(photo_url, agent_name, accent_hex, card_bg_hex, photo_position)
    buffer = BytesIO()
    unit.convert("RGB").save(buffer, format="WEBP", quality=90, method=6)
    return buffer.getvalue()


# Real per-network brand colors for the rail glyphs, instead of a flat dark/light monochrome -
# "website" has no fixed brand color so it keeps the neutral dark glyph.
_BRAND_GLYPH_COLORS: dict[str, tuple[int, int, int]] = {
    "instagram": (225, 48, 108),
    "facebook": (24, 119, 242),
    "youtube": (255, 0, 0),
    "tiktok": (17, 24, 39),
}


def _draw_social_glyph(draw, cx: float, cy: float, r: float, network: str, color: tuple[int, int, int, int], width: int = 2) -> None:
    """Hand-drawn monochrome glyphs (no external icon font/asset shipped in this repo) - simple
    enough to read at 20-30px, close enough to each platform's real mark to be recognizable."""
    if network == "website":
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=width)
        draw.line((cx - r, cy, cx + r, cy), fill=color, width=width)
        draw.ellipse((cx - r * 0.42, cy - r, cx + r * 0.42, cy + r), outline=color, width=width)
    elif network == "instagram":
        s = r * 1.05
        draw.rounded_rectangle((cx - s, cy - s, cx + s, cy + s), radius=s * 0.38, outline=color, width=width)
        ir = r * 0.48
        draw.ellipse((cx - ir, cy - ir, cx + ir, cy + ir), outline=color, width=width)
        dot_r = max(1.5, r * 0.11)
        dot_x, dot_y = cx + s * 0.62, cy - s * 0.62
        draw.ellipse((dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r), fill=color)
    elif network == "facebook":
        # 2.4x r, not 1.7x - a single lowercase glyph has a lot of built-in font leading around it,
        # so it was reading visibly smaller than the other icons' shapes even at "the same" radius.
        font = _inter_font(r * 2.4, 800)
        bbox = draw.textbbox((0, 0), "f", font=font)
        draw.text((cx - (bbox[2] - bbox[0]) / 2 - bbox[0], cy - (bbox[3] - bbox[1]) / 2 - bbox[1]), "f", font=font, fill=color)
    elif network == "youtube":
        w, h = r * 1.1, r * 0.78
        draw.rounded_rectangle((cx - w, cy - h, cx + w, cy + h), radius=h * 0.45, outline=color, width=width)
        tri = r * 0.5
        draw.polygon([(cx - tri * 0.35, cy - tri * 0.65), (cx - tri * 0.35, cy + tri * 0.65), (cx + tri * 0.75, cy)], fill=color)
    elif network == "tiktok":
        # A multicolor "real logo" version (black chip, note layered 3x in brand cyan/red/white)
        # was tried and reverted - it read as visually inconsistent sitting next to every other
        # icon's plain single-color glyph on a shared white chip. Plain lucide Music2 (a bare
        # circle + 2 straight lines) was tried after that as the safe fallback, but read as too
        # generic/not recognizable as TikTok specifically (explicit ask for "a way more accurate
        # tiktok logo"). This keeps the single-color/white-chip treatment every other icon here
        # uses (so it still sits consistently in the rail), but with a filled note + a real curled
        # flag (not a straight diagonal line) - the same shape the multicolor version used, just
        # drawn once in the network's own brand color instead of 3x offset in brand colors.
        note_r, note_ry = r * 0.36, r * 0.30
        note_cx, note_cy = cx - r * 0.10, cy + r * 0.34
        draw.ellipse((note_cx - note_r, note_cy - note_ry, note_cx + note_r, note_cy + note_ry), fill=color)
        stem_w = max(width * 1.4, r * 0.20)
        stem_x = note_cx + note_r * 0.80
        stem_top = cy - r * 0.74
        draw.rounded_rectangle((stem_x - stem_w / 2, stem_top, stem_x + stem_w / 2, note_cy), radius=stem_w / 2, fill=color)
        hook_r = r * 0.36
        hook_box = (stem_x - hook_r * 0.1, stem_top - hook_r * 0.15, stem_x + hook_r * 1.9, stem_top + hook_r * 1.55)
        draw.pieslice(hook_box, start=195, end=345, fill=color)


@lru_cache(maxsize=64)
def render_badge_gif(network: str, accent_hex: str = DEFAULT_ACCENT, card_bg_hex: str = "#FFFFFF") -> bytes:
    """A small white chip with the network's real brand-colored glyph, popping in and settling.
    card_bg_hex flattens the circle onto the exact background it will sit on (GIF has no reliable
    alpha transparency) - the rail now sits on a solid accent-color pill (see _rail_icons_html), so
    the caller passes that same accent through `?bg=` rather than this always assuming white.

    The pop-in range is deliberately small (scale 0.93->1.0, glyph mostly visible from frame 0) so
    the first frame alone - what a client that can't animate GIFs freezes on forever, see
    render_photo_gif's docstring - already reads as a complete, resting icon, not a shrunk
    mid-animation state."""
    from PIL import Image, ImageDraw

    card_bg_rgb = _hex_to_rgb(card_bg_hex, fallback=(255, 255, 255))
    # A white chip reads cleanly against any accent color the agent might pick, so the icon's own
    # real brand color (drawn on top) stays legible regardless of accent - unlike deriving the
    # chip/glyph from the background's own light/dark value, which only worked when the background
    # was a near-neutral tint (true before the rail became a solid accent pill). Every network gets
    # this same white chip, tiktok included (a black-chip "real logo" version was tried and
    # reverted - see _draw_social_glyph's tiktok case).
    circle_fill = (255, 255, 255)
    glyph_color = _BRAND_GLYPH_COLORS.get(network, (17, 24, 39))
    size = _BADGE_SIZE * _STATIC_SCALE
    r = size / 2 - 3

    frames = []
    n = 6
    for i in range(n):
        t = _ease_settle(i / (n - 1))
        scale = 0.93 + 0.07 * t  # a gentle settle, not a shrink-to-pop
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cur_r = r * scale
        # A soft dark outline, not the accent color - the chip already sits on a solid accent
        # pill, so an accent-colored ring around it would be invisible against its own background.
        draw.ellipse((size / 2 - cur_r, size / 2 - cur_r, size / 2 + cur_r, size / 2 + cur_r), fill=(*circle_fill, 255), outline=(0, 0, 0, 30), width=1)
        glyph_alpha = int(255 * (0.85 + 0.15 * t))
        _draw_social_glyph(draw, size / 2, size / 2, cur_r * 0.52, network, (*glyph_color, glyph_alpha))
        canvas = Image.new("RGB", (size, size), card_bg_rgb)
        canvas.paste(layer, (0, 0), layer)
        frames.append(canvas)
    return _frames_to_gif(frames, duration_ms=70, hold_ms=2000)


_BADGE_SUPERSAMPLE = 6  # see render_badge_png


@lru_cache(maxsize=64)
def render_badge_png(network: str, size_display: int = _BADGE_SIZE) -> bytes:
    """The static, settled badge (render_badge_gif's last frame: scale 1.0, full glyph opacity) as
    a real PNG - no `?bg=` flattening needed like the GIF version, since PNG (unlike GIF) has true
    alpha, so this composites cleanly against whatever it's actually placed on. Also sidesteps GIF's
    256-color palette, which was visibly softening this icon's anti-aliased edges.

    `size_display` (CSS/HTML display px, same units as _BADGE_SIZE) - the rail draws each icon
    bigger than the _BADGE_SIZE floor when the agent has fewer than 5 networks filled in (see
    _rail_icons_html), so this is genuinely re-rendered at the requested size rather than just
    upscaled from one fixed bitmap, which would read soft/blurry at the larger sizes. maxsize=64
    on the cache (was 32) - up to 5 networks x 5 possible sizes (_BADGE_SIZE.._BADGE_SIZE_MAX) is
    still a small, fully-bounded key space, not user-controlled beyond that finite set (see the
    route's own clamping).

    Drawn at _BADGE_SUPERSAMPLE times the final pixel size, then downsampled with LANCZOS - Pillow's
    ImageDraw has no built-in anti-aliasing of its own (a circle/glyph drawn directly at the target
    48x48 canvas comes out with visibly jagged, stair-stepped edges), and that jaggedness was a real
    part of why these read as "terrible" quality, not just a resolution/palette problem. Drawing
    large and shrinking down is the standard way to get smooth edges out of a renderer with no native
    anti-aliasing - the downsample softens every hard pixel edge into the intermediate shades a truly
    anti-aliased renderer would have produced directly."""
    from PIL import Image, ImageDraw

    glyph_color = _BRAND_GLYPH_COLORS.get(network, (17, 24, 39))
    size = size_display * _STATIC_SCALE
    big_size = size * _BADGE_SUPERSAMPLE
    r = big_size / 2 - 3 * _BADGE_SUPERSAMPLE
    layer = Image.new("RGBA", (big_size, big_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.ellipse((big_size / 2 - r, big_size / 2 - r, big_size / 2 + r, big_size / 2 + r), fill=(255, 255, 255, 255), outline=(0, 0, 0, 30), width=_BADGE_SUPERSAMPLE)
    _draw_social_glyph(draw, big_size / 2, big_size / 2, r * 0.52, network, (*glyph_color, 255), width=2 * _BADGE_SUPERSAMPLE)
    layer = layer.resize((size, size), Image.LANCZOS)
    buffer = BytesIO()
    layer.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _draw_pill(draw, x: float, y: float, text: str, bg_rgb: tuple, text_rgb: tuple, font, pad_x: float, pad_y: float, radius: float) -> tuple[float, float]:
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pill_w, pill_h = w + pad_x * 2, h + pad_y * 2
    draw.rounded_rectangle((x, y, x + pill_w, y + pill_h), radius=radius, fill=bg_rgb)
    draw.text((x + pad_x - bbox[0], y + pad_y - bbox[1]), text, font=font, fill=text_rgb)
    return pill_w, pill_h


def _diagonal_wash_image(w: int, h: int, hex_a: str, hex_b: str):
    """A two-stop top-left-to-bottom-right gradient fill, the baked-image equivalent of the Live
    Preview's `_cardWashBg` (signaturePreview.ts) - real CSS gradients can paint a <td> background
    directly (see _card_wash_bg/_stats_gradient_stops), but these baked JPEGs need actual pixels.
    Built small (Pillow's linear_gradient is a fixed 256x256) and scaled up with a cheap resize -
    a smooth 2-stop wash has no fine detail that upsampling would lose, so there's no need to
    render it pixel-by-pixel at full size on every request."""
    from PIL import Image, ImageOps

    base = Image.linear_gradient("L").rotate(-45, resample=Image.BICUBIC, expand=True)
    bw, bh = base.size
    crop_w, crop_h = min(bw, max(1, round(bh * w / h))), min(bh, max(1, round(bw * h / w)))
    left, top = (bw - crop_w) // 2, (bh - crop_h) // 2
    base = base.crop((left, top, left + crop_w, top + crop_h)).resize((w, h), Image.BILINEAR)
    return ImageOps.colorize(base, black=_hex_to_rgb(hex_a), white=_hex_to_rgb(hex_b)).convert("RGB")


def _accent_glow_hex(accent_hex: str, dark: bool = False) -> str:
    """The soft accent highlight the Site version layers over its wash (signaturePreview.ts
    _cardWashBg's radial glow): 55% accent into a lighter neutral gray on the dark theme, 38%
    accent into white on the light theme."""
    return _mix_hex(accent_hex, "#4A4D54", 0.45) if dark else _mix_hex(accent_hex, "#FFFFFF", 0.62)


def _wash_with_glow_image(w: int, h: int, accent_hex: str, dark: bool = False):
    """Baked-pixel copy of the Site version's full `_cardWashBg` (signaturePreview.ts): the
    diagonal accent wash PLUS its radial accent glow (`circle at 14% 50%`, fading out by 58% of the
    farthest-corner radius). Explicit ask: the email Recent Sales background read as a "strange"
    dark diagonal instead of the lighter accent-tinted look the Site version has - email clients
    can't be trusted with radial gradients, but these strips are images anyway, so the exact same
    look just gets painted in. No numpy here, so the falloff comes from Pillow's own 256px radial
    gradient, resized so its edge lands exactly at the glow's fade-out radius."""
    from PIL import Image

    wash_light, wash_dark = _card_wash_bg(accent_hex, dark=dark)
    base = _diagonal_wash_image(w, h, wash_light, wash_dark)
    cx, cy = 0.14 * w, 0.5 * h
    far = max(((w - cx) ** 2 + (h - cy) ** 2) ** 0.5, ((w - cx) ** 2 + cy ** 2) ** 0.5, (cx ** 2 + cy ** 2) ** 0.5)
    fade_r = max(1, round(0.58 * far))
    # radial_gradient("L") is 256x256 and reaches 255 at the CORNER (radius ~181), not the edge
    # (radius 128) - remapped so full strength at the center falls linearly to exactly 0 at radius
    # 128, i.e. at `fade_r` once resized. Left unmapped it was still ~30% strong at the square's
    # edge and then cut off, which showed as a hard vertical seam across the strip.
    falloff = Image.radial_gradient("L").point(lambda v: max(0, 255 - round(v * 1.41421))).resize((fade_r * 2, fade_r * 2), Image.BILINEAR)
    mask = Image.new("L", (w, h), 0)
    mask.paste(falloff, (round(cx - fade_r), round(cy - fade_r)))
    glow = Image.new("RGB", (w, h), _hex_to_rgb(_accent_glow_hex(accent_hex, dark)))
    return Image.composite(glow, base, mask)


def _compose_listing_bar_frame(photo, accent_hex: str, item: dict, pan_t: float, w: int, h: int, dark: bool = False, luxe: bool = False):
    """Renders the ENTIRE recent-sale/featured-listing strip - photo, status pill, street/city-
    state, price, and beds/baths/acres/sqft - as one flattened frame. See the module-level
    comment on _LISTING_BAR_DISPLAY_W for why this has to be one baked image rather than a photo
    plus separate real HTML text: this is what actually keeps an already-installed signature
    showing the agent's CURRENT listing/sale without a re-copy-paste, since it's fetched live by
    URL on every view. `dark` re-themes the background/text/divider colors (see _theme_colors) -
    resolved fresh from the signature's saved config by the caller on every render, same
    live-updates-without-reinstalling guarantee as everything else here."""
    from PIL import Image, ImageDraw

    scale = h / _LISTING_BAR_DISPLAY_H
    accent_rgb = _hex_to_rgb(accent_hex)
    _, card_border_hex, text_hex, label_hex = _theme_colors(dark)
    text_rgb, label_rgb = _hex_to_rgb(text_hex), _hex_to_rgb(label_hex)
    metal = luxe_metal_for_accent(accent_hex) if luxe else LUXE_METALS["gold"]
    if luxe:
        # OpenHousePal Luxe: ivory (or midnight) with a soft metal light in the top-right corner,
        # the Spotlight header's look - still one flat image, so every email client shows it.
        canvas = _diagonal_wash_image(w, h, metal["wash_dark"] if dark else metal["wash_light"], "#0B0F0E" if dark else "#FFFDF8")
        glow_r = round(h * 1.6)
        falloff = Image.radial_gradient("L").point(lambda v: max(0, 255 - round(v * 1.41421))).resize((glow_r * 2, glow_r * 2), Image.BILINEAR)
        mask = Image.new("L", (w, h), 0)
        mask.paste(falloff.point(lambda v: round(v * (0.30 if dark else 0.45))), (w - glow_r, -glow_r))
        canvas = Image.composite(Image.new("RGB", (w, h), metal["base_rgb"]), canvas, mask)
        text_rgb = (255, 255, 255) if dark else (17, 24, 39)
        label_rgb = (196, 186, 168) if dark else (107, 98, 84)
        accent_rgb = metal["light_rgb"] if dark else metal["mid_rgb"]
        card_border_hex = metal["border_dark"] if dark else metal["border_light"]
    else:
        canvas = _wash_with_glow_image(w, h, accent_hex, dark=dark)
    draw = ImageDraw.Draw(canvas)

    photo_w = round(_LISTING_PHOTO_DISPLAY_W * scale)
    pad = round(10 * scale)

    if photo is not None:
        zoom = 1.0 + 0.05 * pan_t
        crop_w, crop_h = max(1, int(photo.width / zoom)), max(1, int(photo.height / zoom))
        left, top = (photo.width - crop_w) // 2, (photo.height - crop_h) // 2
        cropped = photo.crop((left, top, left + crop_w, top + crop_h)).resize((photo_w, h), Image.LANCZOS)
        mask = _rounded_corner_mask(photo_w, h, *([round(6 * scale)] * 4))
        canvas.paste(cropped, (0, 0), mask)

    raw_status = str(item.get("status") or "").replace("-BRIGHT", "").replace("-bright", "").replace("_", " ").strip().lower()
    if "coming soon" in raw_status:
        status, pill_bg = "Coming Soon", (17, 24, 39)        # black, like the MLS reads it
    elif "under contract" in raw_status:
        status, pill_bg = "Under Contract", (183, 121, 31)
    elif "pending" in raw_status:
        status, pill_bg = "Pending", (183, 121, 31)
    elif "sold" in raw_status or "closed" in raw_status:
        status, pill_bg = "Sold", (225, 29, 72)
    elif "active" in raw_status:
        status, pill_bg = "Active", (31, 157, 85)
    else:
        status, pill_bg = (raw_status.title() or "Featured"), (17, 24, 39)
    is_sold = status == "Sold"
    # Photo shade toward the copy so the edge reads as one piece, then the status chip.
    shade = Image.new("L", (photo_w, 1), 0)
    for x in range(photo_w):
        shade.putpixel((x, 0), max(0, min(255, round(255 * max(0.0, (x / photo_w) - 0.62) / 0.38 * 0.35))))
    canvas.paste(Image.new("RGB", (photo_w, h), (0, 0, 0)), (0, 0), shade.resize((photo_w, h)))
    _draw_pill(
        draw, round(7 * scale), round(7 * scale), status.upper(), pill_bg, (255, 255, 255),
        _inter_font(7 * scale, 800), pad_x=round(7 * scale), pad_y=round(3.5 * scale), radius=round(8 * scale),
    )

    from app.services.sales_wording import sales_words
    kicker = sales_words("RECENT SALE", item.get("_recent", True)) if is_sold else ("COMING SOON" if status == "Coming Soon" else "FEATURED LISTING")
    street = str(item.get("address") or "").strip().split(",")[0].strip()
    city_state = ", ".join(x for x in [item.get("city"), item.get("state")] if x)
    price = _fmt_money(item.get("price"))
    specs_bits = [(item.get("beds"), "bd"), (item.get("baths"), "ba")]
    specs = [f"{_fmt_num(v)} {label}" for v, label in specs_bits if v]
    sqft = item.get("sqft")
    if sqft:
        specs.append(f"{int(sqft):,} sq ft")
    elif item.get("acres"):
        specs.append(f"{_fmt_num(item.get('acres'))} ac")

    # Sized up to use the strip's room like the Site version's card: kicker, a big street line,
    # city, spec chips on the left; a large price over a "View home" pill on the right.
    kicker_font, addr_font, loc_font = _inter_font(8.5 * scale, 800), _inter_font(19 * scale, 800), _inter_font(11 * scale, 600)
    price_font, chip_font, cta_font = _inter_font(22 * scale, 900), _inter_font(9 * scale, 700), _inter_font(8.5 * scale, 800)
    cta_text = "VIEW HOME  >"

    price_w = draw.textbbox((0, 0), price, font=price_font)[2] if price else 0
    cta_w = draw.textbbox((0, 0), cta_text, font=cta_font)[2] + round(20 * scale)
    side_w = max(price_w, cta_w) + round(4 * scale)
    text_x = photo_w + pad + round(5 * scale)
    divider_x = w - pad - side_w - round(12 * scale)
    copy_w = divider_x - text_x - round(8 * scale)

    top = round(15 * scale)
    # kicker with a short accent rule
    rule_y = top + round(5 * scale)
    draw.line((text_x, rule_y, text_x + round(14 * scale), rule_y), fill=accent_rgb, width=max(1, round(1.2 * scale)))
    draw.text((text_x + round(19 * scale), top), kicker, font=kicker_font, fill=accent_rgb)
    addr_text = street
    while addr_text and draw.textbbox((0, 0), addr_text, font=addr_font)[2] > copy_w and len(addr_text) > 4:
        addr_text = addr_text[:-1]
    if addr_text != street and addr_text:
        addr_text = addr_text[:-1] + "…"
    draw.text((text_x, top + round(13 * scale)), addr_text, font=addr_font, fill=text_rgb)
    draw.text((text_x, top + round(38 * scale)), city_state, font=loc_font, fill=label_rgb)

    # specs as tinted chips (gold-tinted on Luxe)
    if luxe:
        chip_bg = metal["chip_dark"] if dark else metal["chip_light"]
    else:
        chip_bg = tuple(round(c + (255 - c) * 0.82) for c in accent_rgb) if not dark else tuple(round(c * 0.32) for c in accent_rgb)
    cx = text_x
    for spec in specs:
        cw = draw.textbbox((0, 0), spec, font=chip_font)[2] + round(14 * scale)
        if cx + cw > divider_x - round(6 * scale):
            break
        _draw_pill(draw, cx, top + round(58 * scale), spec, chip_bg, text_rgb, chip_font, pad_x=round(7 * scale), pad_y=round(3.5 * scale), radius=round(9 * scale))
        cx += cw + round(5 * scale)

    draw.line((divider_x, round(14 * scale), divider_x, h - round(14 * scale)), fill=_hex_to_rgb(card_border_hex), width=max(1, round(scale)))

    # price + "VIEW HOME" pill, right-aligned, the pair centered vertically
    right = w - pad - round(2 * scale)
    if price:
        bbox = draw.textbbox((0, 0), price, font=price_font)
        draw.text((right - (bbox[2] - bbox[0]) - bbox[0], round(27 * scale) - bbox[1]), price, font=price_font, fill=((255, 255, 255) if (luxe and dark) else text_rgb))
    if luxe:
        pill_rgb, cta_text_rgb = metal["base_rgb"], (17, 24, 39)
    else:
        pill_rgb, cta_text_rgb = accent_rgb, _contrast_text(accent_rgb)
    cbox = draw.textbbox((0, 0), cta_text, font=cta_font)
    pill_w = (cbox[2] - cbox[0]) + round(20 * scale)
    _draw_pill(draw, right - pill_w, round(60 * scale), cta_text, pill_rgb, cta_text_rgb, cta_font, pad_x=round(10 * scale), pad_y=round(5.5 * scale), radius=round(12 * scale))

    if luxe:
        # A thin gold edge across the top (bright in the middle) and a gold hairline frame around
        # the copy - the Spotlight header's gold sweep, frozen.
        edge_h = max(2, round(2 * scale))
        for x in range(w):
            t = 1 - abs((x / max(1, w - 1)) - 0.5) * 2
            col = tuple(round(a + (b - a) * t) for a, b in zip(metal["deep_rgb"], metal["pale_rgb"]))
            draw.line((x, 0, x, edge_h - 1), fill=col)
        inset = round(5 * scale)
        draw.rounded_rectangle((photo_w + inset, inset + edge_h, w - inset, h - inset), radius=round(8 * scale), outline=(_hex_to_rgb(metal["border_dark"]) if dark else _hex_to_rgb(metal["border_light"])), width=max(1, round(scale)))

    return canvas


async def render_listing_bar_jpeg(item: dict, accent_hex: str = DEFAULT_ACCENT, dark: bool = False, luxe: bool = False) -> bytes:
    photo = None
    photo_url = item.get("image")
    if photo_url:
        raw = await _fetch_image(photo_url)
        if raw is not None:
            photo = _cover_crop(raw, round(_LISTING_PHOTO_DISPLAY_W * _STATIC_SCALE * 1.03), _LISTING_BAR_JPEG_H)
        else:
            logger.warning("Signature listing photo fetch failed", extra={"photo_url": photo_url})
    frame = _compose_listing_bar_frame(photo, accent_hex, item, pan_t=0.0, w=_LISTING_BAR_JPEG_W, h=_LISTING_BAR_JPEG_H, dark=dark, luxe=luxe)
    buffer = BytesIO()
    frame.save(buffer, format="JPEG", quality=86, optimize=True)
    return buffer.getvalue()


async def render_listing_bar_webp(item: dict, accent_hex: str = DEFAULT_ACCENT, dark: bool = False, luxe: bool = False) -> bytes:
    """WebP sibling of render_listing_bar_jpeg - the featured-listing strip was already fully
    static (this mode never used the animated GIF path in the real install HTML, only the
    recent-sales strip did), so this is purely a smaller-file swap, not a new animation."""
    photo = None
    photo_url = item.get("image")
    if photo_url:
        raw = await _fetch_image(photo_url)
        if raw is not None:
            photo = _cover_crop(raw, round(_LISTING_PHOTO_DISPLAY_W * _STATIC_SCALE * 1.03), _LISTING_BAR_JPEG_H)
        else:
            logger.warning("Signature listing photo fetch failed", extra={"photo_url": photo_url})
    frame = _compose_listing_bar_frame(photo, accent_hex, item, pan_t=0.0, w=_LISTING_BAR_JPEG_W, h=_LISTING_BAR_JPEG_H, dark=dark, luxe=luxe)
    buffer = BytesIO()
    frame.save(buffer, format="WEBP", quality=86, method=6)
    return buffer.getvalue()


async def render_listing_bar_gif(item: dict, accent_hex: str = DEFAULT_ACCENT, dark: bool = False, luxe: bool = False) -> bytes:
    """The motion equivalent of render_listing_bar_jpeg: a gentle zoom-settle on the photo ending
    at (approximately) the same crop the static JPEG uses. Renders at 1x display size, not the
    JPEG's 2x - a photographic animated GIF is expensive per frame with no delta compression, so
    this is the other big lever (alongside the tiny zoom range) for keeping it well under a
    reasonable per-image weight budget."""
    w, h = _LISTING_BAR_DISPLAY_W, _LISTING_BAR_DISPLAY_H
    photo = None
    photo_url = item.get("image")
    if photo_url:
        raw = await _fetch_image(photo_url)
        if raw is not None:
            photo = _cover_crop(raw, round(_LISTING_PHOTO_DISPLAY_W * 1.06), h)
        else:
            logger.warning("Signature listing photo fetch failed", extra={"photo_url": photo_url})
    n = 5
    frames = [_compose_listing_bar_frame(photo, accent_hex, item, pan_t=1 - _ease_settle(i / (n - 1)), w=w, h=h, dark=dark, luxe=luxe) for i in range(n)]
    # 220 colors, not 110 - same reasoning as render_photo_gif: a real listing photo quantized
    # that hard with no dithering was visibly banding instead of reading as a normal photo.
    return _frames_to_gif(frames, duration_ms=90, hold_ms=2200, colors=220, dither=False)


_SALE_FLIP_HOLD_MS = 4000
_SALE_FLIP_FADE_FRAMES = 3
_SALE_FLIP_FADE_MS = 70


async def render_sale_feature(items: list[dict], accent_hex: str = DEFAULT_ACCENT, dark: bool = False, luxe: bool = False) -> tuple[bytes, str]:
    """An agent with only 1 or 2 recent sales (no listing) - the 3-slot mini strip left one or two
    slots empty, so these get the featured-listing strip instead (user, 10/15): one sale is that
    strip as a still JPEG; two flip between each other (a looping GIF - a soft fade, then each
    held a few seconds). Same size and URL as the featured-listing strip, so the installed HTML
    never changes between a listing, one sale and two. Clients that don't animate GIFs (classic
    desktop Outlook) show the first frame - the newest sale, a complete strip on its own.
    Returns (bytes, media type)."""
    from PIL import Image
    if len(items) < 2:
        return await render_listing_bar_jpeg(items[0], accent_hex, dark=dark, luxe=luxe), "image/jpeg"
    # 1.25x, not the JPEG's 2x: an animated photo GIF has no inter-frame compression - this keeps
    # both homes sharp on a phone at a weight an email signature can carry.
    w, h = round(_LISTING_BAR_DISPLAY_W * 1.25), round(_LISTING_BAR_DISPLAY_H * 1.25)
    stills = []
    for item in items[:2]:
        photo = None
        if item.get("image"):
            raw = await _fetch_image(item["image"])
            if raw is not None:
                photo = _cover_crop(raw, round(_LISTING_PHOTO_DISPLAY_W * 1.25 * 1.03), h)
        stills.append(_compose_listing_bar_frame(photo, accent_hex, item, pan_t=0.0, w=w, h=h, dark=dark, luxe=luxe).convert("RGB"))
    a, b = stills
    # A slide, not a fade - a fade ghosts two addresses and prices over each other mid-way.
    frames, durations = [a], [_SALE_FLIP_HOLD_MS]
    for src, dst in ((a, b), (b, a)):
        for i in range(1, _SALE_FLIP_FADE_FRAMES + 1):
            t = i / (_SALE_FLIP_FADE_FRAMES + 1)
            off = round(w * (1 - (1 - t) ** 3))  # ease-out
            f = Image.new("RGB", (w, h))
            f.paste(src, (-off, 0))
            f.paste(dst, (w - off, 0))
            frames.append(f)
            durations.append(_SALE_FLIP_FADE_MS)
        frames.append(dst)
        durations.append(_SALE_FLIP_HOLD_MS)
    frames.pop()  # the loop comes back to `a` by itself
    durations.pop()
    return _frames_to_looping_gif(frames, durations, colors=236, dither=False), "image/gif"


def _mini_card_geometry(w: int, h: int) -> tuple[int, int, int, int, int, float, int]:
    scale = h / _LISTING_BAR_DISPLAY_H
    pad = round(_SALE_MINI_PAD_DISPLAY * scale)
    # _SALES_HEADER_SCALE (a fixed 2x retina multiplier), NOT `scale` (the bar-height-relative
    # ratio every OTHER measurement here uses) - the real "RECENT SALES" header is its own
    # separate image (render_recent_sales_header_png/webp), always rendered at a flat
    # _SALE_MINI_HEADER_H_DISPLAY * _SALES_HEADER_SCALE, completely independent of how tall the
    # overall bar is. Using the wrong scale here made this function think the header (and so the
    # cards below it) needed less room than the real header image actually takes, so the cards
    # portion rendered ~2-3px TALLER than _listing_bar_html's own `h -= header_h` in the HTML
    # assumes it declared - the header+cards stack ends up taller than the content_h budget the
    # rail/stats columns are sized to, which is exactly why the stats gradient fell a couple
    # pixels short of the card's real bottom edge ("doesn't go all the way to the bottom", explicit
    # ask) even though every number LOOKS consistent if you only check this function in isolation.
    header_h = round(_SALE_MINI_HEADER_H_DISPLAY * _SALES_HEADER_SCALE)
    gap = round(_SALE_MINI_GAP_DISPLAY * scale)
    content_w = w - pad * 2
    card_w_full = (content_w - gap * (_SALE_MINI_COUNT - 1)) // _SALE_MINI_COUNT
    # A smaller, dedicated top offset (not `pad`, which is still the left/right side margin) -
    # explicit ask to bring the photos up a little within their own now-shorter canvas (see
    # render_recent_sales_header_png/_listing_bar_html's sales_header row for why the header no
    # longer lives in this same canvas at all).
    card_y = round(4 * scale)
    # Bottom margin 6 -> 2 -> 6 - trimmed to 2 to let photos run almost to the bar's own bottom
    # edge, but that left them sitting so close to the OUTER container's own rounded (10px)
    # corners that the bottom-left/right corners of the strip itself were visibly getting clipped
    # by that rounding ("sit a little low and get cut off", explicit ask) - back to 6 for enough
    # clearance.
    card_h_full = h - header_h - card_y - round(6 * scale)
    # Cards sized to exactly fill this (now intentionally narrower - see _SALE_MINI_BAR_DISPLAY_W)
    # bar's own content width/height - the freed-width-into-wider-gaps version tried earlier read
    # as "ruined"/uneven; this bar just doesn't reserve that extra width in the first place.
    card_w = card_w_full
    card_h = card_h_full
    return pad, header_h, gap, card_w, card_h, scale, card_y


def _compose_recent_sales_header(accent_hex: str, w: int, header_h: int, scale: float, dark: bool = False, bg=None, recent: bool = True):
    """`bg`, when given, is a pre-built background (see _diagonal_wash_image) this paints straight
    onto instead of a flat fill - lets the static JPEG carousel's header share one continuous
    accent wash with the mini-cards below it rather than each independently flat-filling its own
    region. The animated GIF path doesn't pass one (unmodified this round - see render_photo_gif
    -era decision to leave the animated paths alone), so it keeps the old flat card background."""
    from PIL import Image, ImageDraw

    from PIL import ImageFilter

    if bg is not None:
        canvas = bg.copy()
    else:
        card_bg_hex, _, _, _ = _theme_colors(dark)
        canvas = Image.new("RGB", (w, header_h), _hex_to_rgb(card_bg_hex, fallback=(255, 253, 248)))
    pad = round(_SALE_MINI_PAD_DISPLAY * scale)
    # 8.5 -> 13 -> 6.5 -> 9.5 - the 50%-down 6.5 step overshot ("way too small", explicit ask);
    # 9.5 instead matches the Save Contact bar's own text size (_bake_save_contact_bar) so the
    # card's two small-caption labels read as the same family of text instead of one dwarfing the
    # other.
    # A fixed choice per theme, not computed against the accent, and no underline mark - the
    # underline row was judged not worth the height it took from the mini sale photos below (see
    # _SALE_MINI_HEADER_H_DISPLAY). Black on the light theme, white on the dark theme - its own
    # background stays fairly dark there, so black was unreadable.
    fill = (255, 255, 255) if dark else (17, 24, 39)

    # Drawn on a separately-supersampled layer, then downsampled before compositing - same "draw
    # big, shrink down" fix as the mini-card price/city text (see _draw_mini_sale_card). Drawn
    # directly at `scale` alone (as low as 1x on the GIF path) this read as rough/jagged ("very low
    # quality", explicit ask) next to everything else on the card, which already gets this
    # treatment. Also now vertically centered within header_h instead of pinned to a small fixed
    # top offset, which left it sitting high with dead space below ("not centered in its place").
    text_ss = 3
    # 800 -> 680 (15% less bold) -> back to 800, explicit ask to match the "STATS" header's own
    # font weight (render_stats_png's header_font) so the two small-caption labels use the exact
    # same treatment. 9.5 -> 10.45 -> 12 (explicit ask, repeated: "make a little bigger now") -
    # the header row has room for it now that this text isn't forced uppercase any more.
    big_font = _inter_font(12 * scale * text_ss, 800)
    tw, th = w * text_ss, header_h * text_ss
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    # "RECENT SALES" -> "Recent Sales" (explicit ask: "anything we can do about the recent sales
    # text, maybe its being caps is bad quality" - the Live Preview's own equivalent label is real
    # mixed-case text too (see signaturePreview.ts's .mini-header), just CSS-uppercased for
    # display; a solid block of same-height raster caps has less silhouette variation than mixed
    # case, so it reads muddier once the shadow/blur pass is applied at this small a size).
    from app.services.sales_wording import sales_words
    label = sales_words("Recent Sales", recent)
    bbox = probe.textbbox((0, 0), label, font=big_font)
    # Back to left-pinned at `pad` (briefly centered last round - explicit ask to revert: "make
    # recent sales text back in left side").
    tx = pad * text_ss - bbox[0]
    # True dead-center, not biased up a few px like before - explicit ask: "centered in its spot
    # so nice space above and below even".
    ty = (th - (bbox[3] - bbox[1])) / 2 - bbox[1]

    tight = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    ImageDraw.Draw(tight).text((tx, ty + round(1 * scale * text_ss)), label, font=big_font, fill=(0, 0, 0, 90 if dark else 60))
    tight = tight.filter(ImageFilter.GaussianBlur(max(1, round(scale * text_ss / 2))))
    text_layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    text_layer = Image.alpha_composite(text_layer, tight)
    ImageDraw.Draw(text_layer).text((tx, ty), label, font=big_font, fill=(*fill, 255))

    text_layer = text_layer.resize((w, header_h), Image.LANCZOS)
    canvas = Image.alpha_composite(canvas.convert("RGBA"), text_layer).convert("RGB")
    return canvas


_SALES_HEADER_SCALE = 2  # retina - this is a standalone lossless PNG now (see below), no GIF
# palette to protect any more, same reasoning as _STATS_IMG_SCALE.


def render_recent_sales_header_png(accent_hex: str = DEFAULT_ACCENT, dark: bool = False, recent: bool = True) -> bytes:
    """The "RECENT SALES" label, split out into its own small lossless PNG stacked in its own <tr>
    directly above the listing.gif/jpg cards image (see _listing_bar_html's sales_header row),
    instead of baked into the very top of that shared image the way it used to be. A GIF's limited
    color palette kept visibly banding/quantizing the soft text-shadow blur right around this
    text's own letterforms ("super bad quality", explicit ask, more than once) no matter how much
    supersampling or how many palette colors that shared image was given - the root cause was the
    lossy/palette-limited FORMAT, not the resolution. A PNG has no such limit, so this is now
    genuinely pixel-crisp, the same guarantee stats.png already has for its own text.

    Cropped from a wash sized to the FULL (header + cards) bar height, not independently generated
    at just this small image's own height, so its gradient is effectively continuous with the top
    slice the cards image below it crops from its own independently-built copy of that same wash
    (see render_recent_sales_carousel_gif/jpeg) - the two separate images still read as one strip
    even though they can no longer literally share pixels."""
    scale = _SALES_HEADER_SCALE
    w, h = round(_SALE_MINI_BAR_DISPLAY_W * scale), round(_SALE_MINI_BAR_DISPLAY_H * scale)
    header_h = round(_LEGACY_SALES_HEADER_H_DISPLAY * scale)
    full_wash = _wash_with_glow_image(w, h, accent_hex, dark=dark)
    bg = full_wash.crop((0, 0, w, header_h))
    canvas = _compose_recent_sales_header(accent_hex, w, header_h, scale, dark=dark, bg=bg, recent=recent)
    buffer = BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_recent_sales_header_webp(accent_hex: str = DEFAULT_ACCENT, dark: bool = False, recent: bool = True) -> bytes:
    """Lossless WebP sibling of render_recent_sales_header_png, same composition - see that
    function's own docstring for why this text specifically has to stay lossless (it's the exact
    text that used to visibly band under GIF's limited palette)."""
    scale = _SALES_HEADER_SCALE
    w, h = round(_SALE_MINI_BAR_DISPLAY_W * scale), round(_SALE_MINI_BAR_DISPLAY_H * scale)
    header_h = round(_LEGACY_SALES_HEADER_H_DISPLAY * scale)
    full_wash = _wash_with_glow_image(w, h, accent_hex, dark=dark)
    bg = full_wash.crop((0, 0, w, header_h))
    canvas = _compose_recent_sales_header(accent_hex, w, header_h, scale, dark=dark, bg=bg, recent=recent)
    buffer = BytesIO()
    canvas.save(buffer, format="WEBP", lossless=True, method=6)
    return buffer.getvalue()


def _draw_mini_sale_card(canvas, draw, x: int, y: int, cw: int, ch: int, photo, item: dict, scale: float) -> None:
    """One mini sale card: a full-bleed photo, a small "RECENTLY SOLD" stamp in the top-left corner
    (the same place the featured listing's status pill sits), and town/price over a bottom
    gradient scrim - no beds/baths/specs, which crowded out the photo at this size."""
    from PIL import Image

    radius = round(8 * scale)
    mask = _rounded_corner_mask(cw, ch, radius, radius, radius, radius)
    if photo is not None:
        canvas.paste(_cover_crop(photo, cw, ch), (x, y), mask)
    else:
        canvas.paste(Image.new("RGB", (cw, ch), _hex_to_rgb(DEFAULT_ACCENT)), (x, y), mask)

    # A bottom gradient scrim for text legibility over the photo - IS corner-masked to the card's
    # own rounded rect (re-using the same bottom slice of `mask` used for the photo paste above),
    # despite the plain-rectangle scrim/composite math above it: pasting that rectangle back with
    # no mask was overwriting the card's own bottom-left/right rounded corners with hard square
    # ones, since those corners are actually just background showing through where `mask` excluded
    # the photo, not real rounded pixels of their own that a square overwrite would merge with
    # ("bottom of the photos don't have circle edges", explicit ask - a real bug, not a style ask).
    from PIL import ImageDraw
    scrim_h = round(ch * 0.6)
    scrim = Image.new("RGBA", (cw, scrim_h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scrim)
    for row in range(scrim_h):
        alpha = int(165 * (row / scrim_h) ** 1.3)
        sdraw.line((0, row, cw, row), fill=(0, 0, 0, alpha))
    region = canvas.crop((x, y + ch - scrim_h, x + cw, y + ch)).convert("RGBA")
    region = Image.alpha_composite(region, scrim).convert("RGB")
    canvas.paste(region, (x, y + ch - scrim_h), mask.crop((0, ch - scrim_h, cw, ch)))

    town = str(item.get("city") or "").strip().upper()
    price = _fmt_money(item.get("price"))
    if price or town:
        # Drawn on a separately-supersampled layer, then downsampled before compositing onto the
        # card - this card's own base resolution (scale=_STATIC_SCALE, 2x retina) isn't enough
        # supersampling on its own for small text to read crisp at actual size. The photo/scrim
        # don't need this (photographic detail and a soft gradient have no hard edges to alias),
        # but text does - same "draw big, shrink down" fix already used for the photo ring/badges/
        # stats pills. 16/10.5 -> 9.6/6.3 (40% down) -> back to 16/10.5 ("larger like it was",
        # explicit ask - the 40% cut read as too small once the cards themselves went back to full
        # size, see _mini_card_geometry). Town upper-cased, both getting the same double
        # text-shadow the stats pills use (see render_stats_png's _shadowed_text) - a tight dark
        # pass plus a softer wider one underneath the crisp white text, matching the reference
        # "DOYLESTOWN / $1,900,000" look.
        from PIL import ImageFilter
        text_ss = 2
        tw, th = cw * text_ss, ch * text_ss
        # 700/600 tried, reverted back to 900/800 ("the same font we had before... it looked
        # better", explicit ask). Size then cut 15% (16/10.5 -> 13.6/8.9) as part of the broader
        # "make the recent sales section tighter/smaller" pass (see _SALE_MINI_BAR_DISPLAY_W), then
        # +10% (explicit ask, this round) now that the taller cards have the room for it.
        #
        # text_scale, not the raw `scale` (card_h / _LISTING_BAR_DISPLAY_H) - when _SALE_MINI_BAR_
        # DISPLAY_H later got shrunk (118 -> 92, "stats/icons much less tall"), `scale` dropped with
        # it and this text visibly degraded ("city and price... bad quality now", explicit ask) even
        # though nothing about the text itself changed - it was just riding the same scale factor as
        # the card's own height for no real reason. Floored at the old 118px height's own ratio so
        # the text never gets smaller than it read at full quality, independent of how short the
        # card itself is allowed to get.
        text_scale = max(scale, 118 / _LISTING_BAR_DISPLAY_H)
        price_font = _inter_font(13.6 * 1.10 * text_scale * text_ss, 900)
        town_font = _inter_font(8.9 * 1.10 * text_scale * text_ss, 800)

        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        positions = []
        tty = th - round(8 * scale * text_ss)
        # Left-aligned at a fixed margin, not centered - centering was tried and explicitly
        # reverted ("have it still on the left side how we had it before").
        if price:
            bbox = probe.textbbox((0, 0), price, font=price_font)
            tty -= (bbox[3] - bbox[1])
            positions.append((round(6 * scale * text_ss), tty, price, price_font))
        if town:
            bbox = probe.textbbox((0, 0), town, font=town_font)
            tty -= bbox[3] + round(2 * scale * text_ss)
            positions.append((round(6 * scale * text_ss), tty, town, town_font))

        tight = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        tdraw_tight = ImageDraw.Draw(tight)
        soft = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        tdraw_soft = ImageDraw.Draw(soft)
        for tx, ty, text, font in positions:
            tdraw_tight.text((tx, ty + round(1.5 * scale * text_ss)), text, font=font, fill=(0, 0, 0, 220))
            tdraw_soft.text((tx, ty + round(3 * scale * text_ss)), text, font=font, fill=(0, 0, 0, 110))
        tight = tight.filter(ImageFilter.GaussianBlur(max(1, round(scale * text_ss))))
        soft = soft.filter(ImageFilter.GaussianBlur(max(1, round(3 * scale * text_ss))))

        text_layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        text_layer = Image.alpha_composite(text_layer, tight)
        text_layer = Image.alpha_composite(text_layer, soft)
        tdraw = ImageDraw.Draw(text_layer)
        for tx, ty, text, font in positions:
            tdraw.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))

        # The "RECENTLY SOLD" stamp: a small rose pill, top-left, drawn on this same supersampled
        # layer (so its rounded corners and letters come out smooth once it's shrunk down) - the
        # mini-card twin of the Live Preview's .mini-tag.
        stamp_font = _inter_font(6.4 * text_scale * text_ss, 800)
        from app.services.sales_wording import sales_words
        stamp = sales_words("RECENTLY SOLD", item.get("_recent", True))
        sb = probe.textbbox((0, 0), stamp, font=stamp_font)
        pad_x, pad_y = round(5 * scale * text_ss), round(2.6 * scale * text_ss)
        sx, sy = round(6 * scale * text_ss), round(6 * scale * text_ss)
        sw, sh = (sb[2] - sb[0]) + pad_x * 2, (sb[3] - sb[1]) + pad_y * 2
        tdraw.rounded_rectangle((sx, sy, sx + sw, sy + sh), radius=sh // 2, fill=(201, 24, 64, 255))
        tdraw.text((sx + pad_x - sb[0], sy + pad_y - sb[1]), stamp, font=stamp_font, fill=(255, 255, 255, 255))

        text_layer = text_layer.resize((cw, ch), Image.LANCZOS)
        canvas.paste(text_layer, (x, y), text_layer)


def _compose_sales_strip(order: list[dict], photos: dict, card_w: int, card_h: int, gap: int, scale: float, dark: bool = False, bg=None):
    """One wide strip with every card in `order` laid out left to right - the windowed carousel
    below just crops a moving `w`-wide slice out of this, which is a far simpler way to get a
    smooth continuous scroll than compositing a new frame's worth of cards from scratch. `bg` is
    the same pre-built wash slice _compose_recent_sales_header takes - see its docstring."""
    from PIL import Image, ImageDraw

    n = len(order)
    strip_w = n * card_w + (n - 1) * gap if n else card_w
    strip_w = max(strip_w, card_w)
    if bg is not None:
        strip = bg.copy()
    else:
        card_bg_hex, _, _, _ = _theme_colors(dark)
        strip = Image.new("RGB", (strip_w, card_h), _hex_to_rgb(card_bg_hex, fallback=(255, 253, 248)))
    draw = ImageDraw.Draw(strip)
    x = 0
    for item in order:
        _draw_mini_sale_card(strip, draw, x, 0, card_w, card_h, photos.get(item.get("id")), item, scale)
        x += card_w + gap
    return strip


async def _fetch_mini_photos(items: list[dict], card_w: int, card_h: int) -> dict:
    """Crops each photo down to exactly the card's own native (1x) render size - these cards
    render well under 60px tall, so fetching/keeping any extra resolution beyond that just makes
    every frame's GIF quantization pass bigger for zero visible gain."""
    photos: dict = {}
    for item in items:
        url = item.get("image")
        if not url:
            photos[item.get("id")] = None
            continue
        raw = await _fetch_image(url)
        if raw is None:
            logger.warning("Signature recent-sale photo fetch failed", extra={"photo_url": url})
            photos[item.get("id")] = None
        else:
            # Plain dead-center crop (no focal) - a fy=0.95 bottom bias was tried here (see git
            # history) to avoid a mostly-sky crop on photos with lots of headroom, but it made this
            # the one photo on the whole card NOT matching the Live Preview's own .mini-slide
            # (background-position:center, see signaturePreview.ts) or this card's own other two
            # photos (headshot, featured listing - both already plain-centered by default, see
            # _cover_crop's own focal=None behavior) - explicit ask to make it consistent instead.
            photos[item.get("id")] = _cover_crop(raw, card_w, card_h)
    return photos


async def _recent_sales_static_canvas(items: list[dict], accent_hex: str = DEFAULT_ACCENT, dark: bool = False):
    """The Recent Sales cards at rest - today's 3 sales (rotating day to day via the epoch index,
    never animating within a view), on the same wash+glow background as the header image above it
    so the two stacked images read as one continuous strip. Shared by the JPEG and WebP encoders."""
    if not items:
        return None
    w, h = _SALE_MINI_BAR_JPEG_W, _SALE_MINI_BAR_JPEG_H
    pad, header_h, gap, card_w, card_h, scale, card_y = _mini_card_geometry(w, h)
    n = len(items)
    start = _epoch_day_index() % n
    window = [items[(start + i) % n] for i in range(min(_SALE_MINI_COUNT, n))]
    photos = await _fetch_mini_photos(window, card_w, card_h)

    # Cropped from a background sized to the FULL (header + cards) bar height - keeps this image's
    # gradient continuous with the slice render_recent_sales_header_png crops for itself.
    full_wash = _wash_with_glow_image(w, h, accent_hex, dark=dark)
    canvas = full_wash.crop((0, header_h, w, h))
    m = len(window)
    strip_w = m * card_w + (m - 1) * gap if m else card_w
    strip_bg = canvas.crop((pad, card_y, pad + strip_w, card_y + card_h))
    strip = _compose_sales_strip(window, photos, card_w, card_h, gap, scale, dark=dark, bg=strip_bg)
    canvas.paste(strip, (pad, card_y))
    return canvas


async def render_recent_sales_carousel_jpeg(items: list[dict], accent_hex: str = DEFAULT_ACCENT, dark: bool = False) -> bytes:
    """Static Recent Sales cards (no header text - see render_recent_sales_header_png)."""
    canvas = await _recent_sales_static_canvas(items, accent_hex, dark)
    if canvas is None:
        return b""
    buffer = BytesIO()
    canvas.save(buffer, format="JPEG", quality=86, optimize=True)
    return buffer.getvalue()


async def _sales_carousel_frames(items: list[dict], accent_hex: str, dark: bool):
    """Shared frame-building for the recent-sales carousel - used by both
    render_recent_sales_carousel_gif and render_recent_sales_carousel_webp so the two encoders
    can't drift out of sync on layout/content. Returns None when there's nothing to show, or
    (frames, durations) - exactly 1 frame/duration when the pool is too small to animate (>=3
    sales needed for real motion), matching each encoder's own single-frame handling."""
    # Built at the same 2x geometry as the static JPEG, then each frame is downsampled to 1x at the
    # end (see _downsample). _mini_card_geometry's header_h is always the 2x header height, so
    # building frames directly at 1x subtracted a 2x header from a 1x bar - the animated cards came
    # out 18px shorter than the JPEG's (88 vs 106), leaving dead space under Recent Sales because
    # the rail/stats columns are sized for the full bar (explicit ask: "no reason for so much dead
    # space under the recent sales section"). Downsampling keeps the WebP's file size where it was.
    w, h = _SALE_MINI_BAR_JPEG_W, _SALE_MINI_BAR_JPEG_H
    pad, header_h, gap, card_w, card_h, scale, card_y = _mini_card_geometry(w, h)
    full_n = len(items)
    if full_n == 0:
        return None
    start = _epoch_day_index() % full_n
    rotated = [items[(start + i) % full_n] for i in range(full_n)]
    # Bounded independently of the pool's own eligibility size (up to _SALES_ROTATION_POOL_SIZE) -
    # see _SALE_MINI_MAX_ANIMATED. Which sales fall in this window still changes day to day via the
    # rotation above, so a long tail of sales isn't permanently excluded, just not all animated
    # within any single loop.
    order = rotated[:_SALE_MINI_MAX_ANIMATED]
    n = len(order)
    photos = await _fetch_mini_photos(order, card_w, card_h)

    # Cropped from a wash sized to the FULL (header + cards) bar height, not independently
    # generated at just this shorter canvas's own height - keeps this image's gradient pixel-
    # identical to the slice render_recent_sales_header_png's own wash crop leaves off from, so the
    # two stacked images (see _listing_bar_html) still read as one continuous gradient. Built once
    # here since it's identical on every frame, not per-frame.
    wash_light, wash_dark = _card_wash_bg(accent_hex, dark=dark)
    full_wash = _diagonal_wash_image(w, h, wash_light, wash_dark)
    cards_bg = full_wash.crop((0, header_h, w, h))
    # w - pad*2, matching _mini_card_geometry's own content_w exactly (both pads, not just one) -
    # this was the actual bug behind "I can still see a sliver of the 4th property": with only one
    # pad subtracted, the crop window was `pad` px WIDER than the 3 cards + gaps it's supposed to
    # frame, exposing real pixels from whatever card sits just past the 3rd in the scrolling strip.
    content_w = w - pad * 2

    from PIL import Image as _PILImage
    out_size = (_SALE_MINI_BAR_DISPLAY_W, (h - header_h) // _STATIC_SCALE)

    def frame_at(strip, offset: int):
        frame = cards_bg.copy()
        frame.paste(strip.crop((offset, 0, offset + content_w, card_h)), (pad, card_y))
        return frame.resize(out_size, _PILImage.LANCZOS)

    if n <= _SALE_MINI_COUNT:
        # content_w wide, not just the cards' own natural width - a still-short pool (1-2 sales)
        # needs the same padding the old separate pad_strip step gave it, so the crop never runs
        # past the strip's own right edge; passing a content_w-wide `bg` slice does that for free.
        strip_bg = cards_bg.crop((pad, card_y, pad + content_w, card_y + card_h))
        strip = _compose_sales_strip(order, photos, card_w, card_h, gap, scale, dark=dark, bg=strip_bg)
        return [frame_at(strip, 0)], [0]

    # Two laps of the pool back-to-back so the crop window can scroll one full lap (n steps)
    # without ever running past the strip's own right edge. A fresh, independently-sized wash
    # (not a crop of `full_wash`) since this strip is much wider than one frame - the gradient
    # subtly drifts under the cards as the crop window scrolls across it, which reads as an
    # intentional bit of extra motion rather than a static backdrop.
    strip_w = max(2 * n * card_w + (2 * n - 1) * gap, card_w)
    strip_bg = _diagonal_wash_image(strip_w, card_h, wash_light, wash_dark)
    strip = _compose_sales_strip(order + order, photos, card_w, card_h, gap, scale, dark=dark, bg=strip_bg)
    step_w = card_w + gap
    frames: list = []
    durations: list[int] = []
    for i in range(n):
        base_offset = i * step_w
        frames.append(frame_at(strip, base_offset))
        durations.append(_SALE_MINI_STEP_MS)
        for step in range(1, _SALE_MINI_SLIDE_FRAMES + 1):
            t = _ease_settle(step / _SALE_MINI_SLIDE_FRAMES)
            frames.append(frame_at(strip, base_offset + round(step_w * t)))
            durations.append(_SALE_MINI_SLIDE_FRAME_MS)
    return frames, durations


async def render_recent_sales_carousel_gif(items: list[dict], accent_hex: str = DEFAULT_ACCENT, dark: bool = False) -> bytes:
    """A single "RECENT SALES" bubble that always shows 3 sales (town + price + SOLD stamp, no
    specs) at once. With more than 3 qualifying sales it continuously scrolls the window by one
    card every _SALE_MINI_STEP_MS, cycling through the agent's whole rotation pool before looping
    back. Kept around as a format option (see render_recent_sales_carousel_webp for the one
    _build_gallery actually uses now, plus render_recent_sales_carousel_jpeg for the fully static
    fallback) - GIF's palette limits are exactly why WebP replaced it as the default animated
    format, not because GIF stopped working."""
    result = await _sales_carousel_frames(items, accent_hex, dark)
    if result is None:
        return b""
    frames, durations = result
    if len(frames) == 1:
        buffer = BytesIO()
        frames[0].save(buffer, format="GIF")
        return buffer.getvalue()
    # 80 -> 140 -> 200 -> 160 -> 220. The shadowed text (see _draw_mini_sale_card and
    # _compose_recent_sales_header) blurs into a lot of intermediate gray tones around every
    # letterform, and anything under ~200 colors with no dithering visibly banded/quantized right
    # on those edges ("super bad quality", explicit ask, specifically on the RECENT SALES header
    # text). 160 was the settled trade-off back when the bar was taller/wider - the photo and
    # bar-height cuts made later in this same pass (see _PHOTO_DISPLAY, _LISTING_BAR_DISPLAY_H)
    # freed up enough of that original size budget to afford 220 again without breaking the
    # ~500KB ceiling (measured: a real file at 220 colors here comes in well under it).
    return _frames_to_looping_gif(frames, durations, colors=220, dither=False)


async def render_recent_sales_carousel_webp(items: list[dict], accent_hex: str = DEFAULT_ACCENT, dark: bool = False) -> bytes:
    """STATIC WebP of the Recent Sales cards - explicit ask, repeated: the email version's sales
    should never shuffle/scroll ("takes too much space and makes it less compatible to really work
    on every provider"). Same still frame as the JPEG fallback (so every client, WebP-capable or
    not, shows the identical three sales), just smaller on the wire. Kept at 2x for retina
    sharpness - a single still frame is small enough that there's no reason to downsample it the
    way the old 13-frame animation had to be. Which 3 sales show still rotates day to day."""
    canvas = await _recent_sales_static_canvas(items, accent_hex, dark)
    if canvas is None:
        return b""
    buffer = BytesIO()
    canvas.save(buffer, format="WEBP", quality=88, method=6)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

CARD_BG = "#FFFDF8"
_CARD_BORDER = "#E7DDCF"
_TEXT_COLOR = "#171817"
_LABEL_COLOR = "#77746C"

# Dark mode (Customize tab toggle) - a deep neutral charcoal rather than a pure black, so the warm
# accent colors still read as warm against it instead of clashing. Every raster asset (photo,
# badges, listing bar, recent-sales carousel) needs its OWN correctly-themed background baked in,
# since GIF/JPEG have no reliable alpha to sit transparently over whatever HTML happens to be
# behind them - see render_photo_gif's docstring for the same constraint.
CARD_BG_DARK = "#1B1D22"
_CARD_BORDER_DARK = "#34373E"
_TEXT_COLOR_DARK = "#F4F2EC"
_LABEL_COLOR_DARK = "#9B9890"


def theme_card_bg(dark: bool) -> str:
    """The single source of truth callers outside this module (email_signature_routes.py's asset
    endpoints) use to pick photo.gif/badge-gif's background - keeps the light/dark hex values
    defined in exactly one place."""
    return CARD_BG_DARK if dark else CARD_BG


def _theme_colors(dark: bool) -> tuple[str, str, str, str]:
    """(card_bg, card_border, text_color, label_color) for the current theme - resolved fresh on
    every render from the signature's OWN saved config (see build_signature_html), never baked in
    at "install" time, so flipping the Customize tab's dark-mode toggle takes effect on an
    already-installed signature the next time any mailbox fetches its images - no re-copy-paste,
    same guarantee every other live-config-driven part of this design already has."""
    return (CARD_BG_DARK, _CARD_BORDER_DARK, _TEXT_COLOR_DARK, _LABEL_COLOR_DARK) if dark else (CARD_BG, _CARD_BORDER, _TEXT_COLOR, _LABEL_COLOR)


def _resolved_identity(cfg: dict, hub: dict, owner: User) -> dict:
    profile = hub.get("profile") or {}
    contact = hub.get("contact") or {}
    fields = cfg["fields"]
    display_name = (profile.get("name") or "").strip() or f"{owner.first_name} {owner.last_name}".strip()
    return {
        "name": display_name if fields["name"]["include"] else "",
        "title": profile.get("title", "").strip() if fields["title"]["include"] else "",
        "company": profile.get("brokerage", "").strip() if fields["company"]["include"] else "",
        "team": profile.get("team", "").strip() if fields["team"]["include"] else "",
        "headshot": (hub.get("profilePhotoUrl") or owner.photo_url or "") if fields["headshot"]["include"] else "",
        "phone": contact.get("call", "").strip() if fields["phone"]["include"] else "",
        "email": contact.get("email", "").strip() if fields["email"]["include"] else "",
    }


def _stable_social(social_links: list, network: str, redirect_base_url: str) -> str:
    """A social/website icon's href: the stable /go/{owner_id}/social/{network} redirect, which
    looks up the agent's CURRENT link for that network on every click - so fixing a typo'd or
    changed Instagram/website URL in Settings takes effect in an already-installed signature with
    no re-paste (it used to bake the raw URL in at copy time). Still returns "" when the agent has
    no link for that network, since that decides whether the icon renders at all."""
    url = _social_url(social_links, network)
    if not url:
        return ""
    return f"{redirect_base_url}/social/{network}" if redirect_base_url else url


def _resolved_links(cfg: dict, hub: dict, redirect_base_url: str) -> dict:
    fields = cfg["fields"]
    social_links = hub.get("socialLinks") or []
    website = _stable_social(social_links, "website", redirect_base_url) if fields["website"]["include"] else ""
    # The Agent Hub button routes through the stable /go/{owner_id}/hub redirect (see
    # email_signature_routes.py) rather than a directly-resolved URL, so it keeps working across
    # slug renames, domain changes, and tier upgrades/downgrades without the agent ever having to
    # touch Gmail/Outlook again.
    agent_hub_url = f"{redirect_base_url}/hub" if redirect_base_url else ""
    return {
        "website": website,
        "agentHubUrl": agent_hub_url if fields["agentHubUrl"]["include"] and agent_hub_url else "",
        # Same stable-redirect guarantee as agentHubUrl - always resolves to whatever the agent's
        # current Contact Card settings say, so updating a phone number in Settings doesn't require
        # re-pasting the signature. Not gated by a field toggle (there's no "hide the save-contact
        # badge" setting) - it's always shown, same as the Agent Hub's own Save Contact button.
        "vcardUrl": f"{redirect_base_url}/vcard" if redirect_base_url else "",
        "instagram": _stable_social(social_links, "instagram", redirect_base_url) if fields["instagram"]["include"] else "",
        "facebook": _stable_social(social_links, "facebook", redirect_base_url) if fields["facebook"]["include"] else "",
        "youtube": _stable_social(social_links, "youtube", redirect_base_url) if fields["youtube"]["include"] else "",
        "tiktok": _stable_social(social_links, "tiktok", redirect_base_url) if fields["tiktok"]["include"] else "",
    }


def _rail_icons_html(links: dict, asset_base: str, card_bg_hex: str, content_h: int = 0) -> str:
    """A vertical stack of small brand-colored icon chips (website/instagram/facebook/youtube/
    tiktok) - each icon a real <img> (never a background-image trick) so it's simple and reliable.
    Just the icon stack itself - the containing accent-OUTLINE pill (rounded rectangle, not filled)
    is the <td> this sits inside in _build_gallery, so that <td> can naturally stretch to the full
    row height the way a separately-sized inner table can't.

    `content_h` (see side_column_content_height) is what makes that stretch NOT read as dead space:
    a CSS percentage height has no definite ancestor to resolve against here (the card's own outer
    table is deliberately auto/content-height), and real flexbox isn't available in email HTML at
    all - but the caller already KNOWS the target height ahead of time from the photo/listing's own
    fixed pixel sizes, so this spends it directly as fixed-pixel spacer rows between icons instead,
    the same "spread evenly" result the Live Preview's real `justify-content: space-evenly`
    (.gallery-socials) produces - a real leading gap too, not just trailing, so the icons read as
    centered in the pill rather than pinned to its top edge.

    `card_bg_hex` is unused now that badges are PNG (real alpha, no flattening needed) - kept as a
    parameter so callers don't need to change, only what this does with it.

    An accent-tinted "bubble" behind each icon (matching the stats pills' own treatment) was tried
    and reverted - explicit ask, it read as worse than the plain chip."""
    order = [("website", links["website"]), ("instagram", links["instagram"]), ("facebook", links["facebook"]), ("youtube", links["youtube"]), ("tiktok", links["tiktok"])]
    active = [(network, url) for network, url in order if url]
    if not active:
        return ""
    # Bigger icons the fewer networks the agent has filled in (explicit ask: "if the agent
    # deselects some it should make them all a bit bigger since there is more space there") - each
    # missing icon (below the max 5) hands its share of the pill's fixed vertical budget to the
    # remaining ones as real pixel size, not just wider gaps around the same small icons. Still
    # capped at _BADGE_SIZE_MAX (see its own comment) so a single icon never touches the pill's
    # own border.
    icon_size = min(_BADGE_SIZE_MAX, _BADGE_SIZE + 2 * (5 - len(active)))
    icons = []
    for network, url in active:
        href = url if url.startswith("http") else f"https://{url}"
        icons.append(
            f'<a href="{_esc(href)}"><img src="{asset_base}/social/{network}.png?size={icon_size}" width="{icon_size}" height="{icon_size}" '
            f'alt="{network}" style="display:block;border:0;border-radius:50%;" /></a>'
        )
    # n+1 gaps - one BEFORE the first icon too, not just between/after - true space-evenly instead
    # of flush-top. That leading gap is what visually centers the whole stack in the pill.
    # `content_h` is the exact pixel height available INSIDE the pill (the caller already took off
    # the pill's border and padding). Gaps are floored and the leftover pixels are handed out one
    # at a time, so the stack fills that height exactly - never a few pixels over (which used to
    # make the rail the tallest cell and stretch the whole card).
    n = len(icons)
    extra = content_h - n * icon_size
    if extra >= (n + 1) * 4:
        base_gap, leftover = divmod(extra, n + 1)
        gaps = [base_gap + (1 if i < leftover else 0) for i in range(n + 1)]
    else:
        gaps = [4] * (n + 1)

    def spacer(height: int) -> str:
        return f'<tr><td height="{height}" style="height:{height}px;line-height:1px;font-size:1px;">&nbsp;</td></tr>'

    rows = [spacer(gaps[0])]
    for i, icon in enumerate(icons):
        rows.append(f'<tr><td align="center" style="line-height:0;font-size:0;">{icon}</td></tr>')
        rows.append(spacer(gaps[i + 1]))
    return f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">{"".join(rows)}</table>'


def _contact_inline_html(identity: dict, text_color: str, label_color: str, divider_color: str, font_stack: str) -> str:
    """Phone/Email laid out inline with a bold short label prefix (Mobile / Email), separated by a
    thin divider line. Uses wrapping inline-block spans in a plain div - NOT a <table> with
    white-space:nowrap - on purpose: a real name + a full email address easily adds up to 300px+,
    more than the ~260px available next to the photo, and a rigid nowrap table row would force the
    whole card wider than its fixed 600px instead of wrapping. inline-block wraps onto a second
    line automatically when it runs out of room, the same way the reference design's own flex-wrap
    contact row behaves. Website is NOT repeated here - it's already the first rail icon (a real
    link to the site), so a second text copy of the same URL would be pure redundancy."""
    items = []
    if identity["phone"]:
        tel_digits = re.sub(r"[^\d+]", "", identity["phone"])
        items.append(("Mobile", f'<a href="tel:{_esc(tel_digits)}" style="color:{text_color};text-decoration:none;font-weight:700;">{_esc(_fmt_phone(identity["phone"]))}</a>'))
    if identity["email"]:
        items.append(("Email", f'<a href="mailto:{_esc(identity["email"])}" style="color:{text_color};text-decoration:none;font-weight:700;">{_esc(identity["email"])}</a>'))
    if not items:
        return ""
    spans = []
    for i, (label, value_html) in enumerate(items):
        border = f'border-left:1px solid {divider_color};padding-left:10px;' if i > 0 else ""
        spans.append(
            f'<span style="display:inline-block;{border}padding-right:10px;margin:0 0 4px 0;'
            f'font-family:{font_stack};font-size:10.5px;">'
            f'<span style="color:{label_color};font-weight:800;">{label}</span> {value_html}</span>'
        )
    return f'<div>{"".join(spans)}</div>'


def _action_row_html(links: dict, cta_buttons: list, accent: str, redirect_base_url: str, font_stack: str, card_bg: str = CARD_BG, text_on_accent: Optional[str] = None) -> str:
    """Up to 3 pill buttons, ALL solid-filled in the agent's accent color like "My Agent Hub"
    always was - agent-configured CTA buttons used to render as plain accent-outlined pills, which
    went invisible whenever the accent itself was white/near-white (nothing to outline against)
    and read as a visual downgrade next to the one solid button anyway. Wrapping inline-block (not
    a rigid nowrap <table> row) for the same overflow reason as _contact_inline_html - 3 real
    button labels can exceed the available width, and this lets a 3rd button drop to its own line
    instead of blowing out the card width.

    A button tied to one of the agent's premade forms (`formType` set) resolves through the same
    stable /go/{owner_id}/form/{formType} redirect the Agent Hub button uses, instead of a bare
    saved URL - a form's own slug is editable in Studio, so this always sends a click to whatever
    that form's current slug is, not whatever it was the day the signature was installed. Only a
    genuinely custom button (no formType) uses its typed `url` as-is - there's no stable indirect
    version of an arbitrary external link the agent provided."""
    buttons = []
    if links.get("agentHubUrl"):
        buttons.append(("My Agent Hub &rarr;", links["agentHubUrl"]))
    for b in cta_buttons:
        if not (isinstance(b, dict) and b.get("enabled") and str(b.get("label") or "").strip()):
            continue
        form_type = str(b.get("formType") or "").strip()
        url = f"{redirect_base_url}/form/{form_type}" if form_type and redirect_base_url else str(b.get("url") or "").strip()
        if not url:
            continue
        buttons.append((_esc(b["label"]), url))
    buttons = buttons[:3]
    if not buttons:
        return ""
    # A fixed compact sans-serif here, NOT the agent's chosen `font_stack` (used for the name/body
    # text) - a serif choice like "Editorial" is noticeably wider per character, and inheriting it
    # was pushing the 3rd button (usually "What's My Home Worth?") onto its own line instead of
    # staying with the other two. Buttons are compact UI, not editorial copy, so they keep one
    # consistent, space-efficient font regardless of what's picked for the rest of the card.
    button_font_stack = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
    # Computed per-accent (_contrast_text_hex), not hardcoded white - a genuinely light/white accent
    # made white button text nearly invisible (a real legibility bug, not just an aesthetic one).
    # This is different from the "faux-outline shadow read as messy" experiment noted elsewhere in
    # this module: that was about ADDING a text-shadow/stroke on top of white text, which is still
    # avoided here (buttons stay flat, no stroke) - this only changes which flat color gets picked.
    text_color = text_on_accent or _contrast_text_hex(accent)
    # A subtle top-to-bottom gradient (declared AFTER the flat `background` so clients that
    # understand CSS gradients use it, and everything else silently keeps the flat accent fill
    # right before it) - reads as a deliberately polished metal/glass pill instead of a plain
    # color block, matching the Save Contact bar's identical treatment.
    light_hex = _mix_hex(accent, "#FFFFFF", 0.22)
    dark_hex = _mix_hex(accent, "#000000", 0.18)
    # Border defaults to the safe-contrast color too (white outline on a dark theme + dark accent,
    # black outline on the light theme + a white/near-white accent) - a plain accent border would
    # otherwise nearly vanish against a card background it's barely distinguishable from, leaving
    # the button with no defined edge in exactly that case.
    border_color = _safe_accent_on_bg_hex(accent, card_bg)
    cells = []
    for i, (label, url) in enumerate(buttons):
        href = url if str(url).startswith("http") else f"https://{url}"
        # "My Agent Hub" (always first) gets a visibly stronger box-shadow than the other buttons -
        # the single most important link on the card, so it should read as more prominent, not
        # blend in with the rest.
        shadow = "0 8px 18px rgba(0,0,0,.32)" if i == 0 else "0 3px 8px rgba(0,0,0,.18)"
        style = (
            f'background-color:{accent};background-image:linear-gradient(180deg,{light_hex} 0%,{accent} 55%,{dark_hex} 100%);'
            f'color:{text_color};border:1px solid {border_color};box-shadow:{shadow};'
        )
        # font-size stays 9.5px (bigger, more prominent text - the actual ask), but padding pulled
        # back 7px 13px -> 6px 10px - the fuller padding pushed the 3rd button onto its own line
        # again, and keeping the text size while trimming the padding was the explicit fix requested
        # rather than reverting the text size itself.
        cells.append((
            f'<a href="{_esc(href)}" style="display:inline-block;white-space:nowrap;{style}'
            f'font-family:{button_font_stack};font-size:{{fs}};font-weight:700;text-decoration:none;'
            f'padding:{{pad}};border-radius:8px;',
            label,
        ))
    # One table row, not wrapping inline-blocks: phone mail apps shrink the card to fit and then
    # enlarge small text, which used to push the 3rd button onto its own line. Table cells never
    # wrap, so the row stays one line on every screen. Rough width estimate (bold ~0.55em/char, calibrated on the rendered card) picks
    # a compact size for long labels, and only a set that still can't fit falls back to wrapping.
    def est(fs: float, pad_x: int) -> float:
        return sum(len(re.sub(r"&\w+;", "x", lbl)) * fs * 0.55 + pad_x * 2 + 8 for _, lbl in cells)
    for fs, pad_v, pad_x in ((9.5, 6, 10), (8.5, 5, 8)):
        if est(fs, pad_x) <= _ACTION_ROW_MAX_W:
            tds = "".join(
                f'<td style="padding:0 6px 6px 0;">{a.replace("{fs}", f"{fs:g}px").replace("{pad}", f"{pad_v}px {pad_x}px")}">{lbl}</a></td>'
                for a, lbl in cells
            )
            return f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>{tds}</tr></table>'
    return "<div>" + "".join(
        f'{a.replace("{fs}", "8.5px").replace("{pad}", "5px 8px")}margin:0 6px 6px 0;">{lbl}</a>' for a, lbl in cells
    ) + "</div>"


def _featured(cfg: dict, hub: dict) -> Optional[tuple[str, list[dict]]]:
    if not cfg["blockVisibility"].get("banner"):
        return None
    banner_cfg = cfg["banner"]
    return pick_featured_items(hub, banner_cfg.get("showFeaturedListing", True), banner_cfg.get("showRecentSales", True))


def wants_stats(cfg: dict) -> bool:
    """Not module-private (no leading underscore) - the stats.png route needs this same check
    before rendering the live stats image, not just _build_gallery deciding whether to reserve the
    column for it."""
    # Its own switch - stats show even with the listings & sales strip off (user, 10/15).
    return bool(cfg["banner"].get("showStats"))


def side_column_content_height(cfg: dict, hub: dict, owner: User) -> int:
    """The icon rail's and stats column's predictable content height - not module-private, since
    the /stats.png route needs the exact same number _build_gallery uses (see render_stats_png)
    to space the stat pills across it, not just a locally-guessed height that could drift out of
    sync with the actual row.

    This is knowable ahead of time, without measuring anything: main_cell's real height is set by
    its two fixed-pixel hero elements - the photo canvas and the listing/recent-sales banner - not
    by the identity block's name/title/contact/buttons, which always render shorter than the photo
    (see _PHOTO_CANVAS_H) regardless of how long the agent's name or title text is. So there's no
    need to actually lay anything out to know how tall this column should be; it's just those two
    constants, conditional on whether each is present."""
    identity = _resolved_identity(cfg, hub, owner)
    h = _PHOTO_CANVAS_H if (identity["headshot"] or identity["name"]) else 0
    featured = _featured(cfg, hub)
    if featured:
        featured_mode, _ = featured
        h += _LISTING_MARGIN_TOP + (_SALE_MINI_BAR_DISPLAY_H if featured_mode == "sale" else _LISTING_BAR_DISPLAY_H)
    elif stats_as_strip(cfg, hub):
        h += _LISTING_MARGIN_TOP + _STATS_STRIP_DISPLAY_H
    return h


_STATS_IMG_SCALE = 2
_STATS_SUPERSAMPLE = 3  # draw-big-then-downsample factor for the pill borders - see render_stats_png
_STATS_IMG_DISPLAY_W = 84  # the stats column (100px, see _STATS_W) minus its own 8px side padding, both sides


async def _compose_stats_canvas(stats: dict, accent_hex: str, dark_theme: bool = False, target_h: int = 0):
    """Header ("Stats") + up to 3 bordered pills (Volume/Sold/Avg), as ONE live-fetched image -
    same reasoning as the photo/listing images (see their docstrings), applied to a piece that used
    to be the one part of this card that was ALWAYS frozen at copy time even though it's arguably
    the most likely thing to go stale: the numbers themselves. A signature installed once, months
    ago, kept showing whatever totalSold/totalVolume were true the day it was copied - closing 5
    more deals since then never showed up. Baked-in accent-color pill borders/backgrounds had the
    same problem as the photo halo used to (a color change after install never applied) for the
    same reason. Both are fixed now that this is a URL fetched fresh on every view like everything
    else server-rendered here.

    PNG with a transparent backdrop, not JPEG - two independent reasons. First, the pills need to
    float over the column's own gradient wash (see _stats_gradient_stops in _build_gallery) instead
    of sitting on a flat rectangle that blots it out; a transparent canvas lets the header/pills be
    the only opaque pixels, same as how the badges already sit on whatever's behind them. Second,
    JPEG's lossy 4:2:0 chroma subsampling visibly rang/scalloped the thin, high-contrast pill
    borders into a bumpy-looking edge no quality setting fixed - a flat-color, thin-line image like
    this is exactly what lossless PNG is for.

    Drawn at _STATS_SUPERSAMPLE times the true output size, then downsampled with LANCZOS at the
    very end - Pillow's rounded_rectangle has no anti-aliasing of its own, so the pill borders came
    out visibly jagged at 2x alone (same underlying issue render_photo_jpeg's ring had, and
    render_badge_png's circles had before that - see either docstring). Simplest of the three to
    fix: everything here is one flat canvas with no separate mask/photo layer to juggle, so the
    whole thing just gets built bigger and shrunk once at the end.

    `target_h` (see side_column_content_height), when given and taller than the pills' own natural
    stacked height, spreads the surplus evenly across the gaps between/after them - the same
    "space-evenly, not clustered-then-a-dead-block" fix _rail_icons_html applies to the icon rail,
    for the same reason: this image's own <td> stretches to match the row's real height regardless,
    so the only choice is whether that leftover room reads as intentional breathing room woven
    through the pills or as one ignored gap underneath them."""
    from PIL import Image, ImageDraw, ImageFilter

    true_scale = _STATS_IMG_SCALE
    scale = true_scale * _STATS_SUPERSAMPLE
    accent_rgb = _hex_to_rgb(accent_hex)
    # Always white, not conditional on the accent's own brightness - that conditional (dark text
    # for a "light" accent like gold/silver/light red/white, by luminance) predates the text-shadow
    # this now always draws underneath the glyphs (see _shadowed_text): the shadow is what actually
    # keeps it legible against a light accent, the same way it does against a dark one, so the accent
    # never needs to flip the text color itself anymore. That conditional was real - it correctly
    # picked dark text for those lighter presets - but "correct in isolation" still read as visibly
    # inconsistent next to bronze/every other accent's white text, which is what got flagged.
    text_rgb = (255, 255, 255)
    pill_border_rgb = _hex_to_rgb(_mix_hex(accent_hex, "#FFFFFF", 0.35))
    pill_bg_rgb = _hex_to_rgb(_mix_hex(accent_hex, "#FFFFFF", 0.16))

    # "Closed", not "Sold" - matches the Live Preview's own stat-item label exactly (see
    # signaturePreview.ts's featured/stats markup), which this had drifted from. Labels are drawn
    # upper-case directly (VOLUME/CLOSED/AVG) since there's no CSS text-transform in a raster image.
    cells = [("VOLUME", _fmt_compact_money(stats["totalVolume"])), ("CLOSED", str(stats["totalSold"])), ("AVG", _fmt_compact_money(stats["avgPrice"]))]
    cells = [(label, value) for label, value in cells if value]

    w = round(_STATS_IMG_DISPLAY_W * scale)
    # 20 -> 18 -> 16 (explicit ask, "a little bit smaller") - now just under the Live Preview's own
    # .stat-item (17px), close enough that neither dwarfs the other.
    # 11/16/8 -> 12/18/9 (explicit ask, this round: "make the boxes in stats and stats text a
    # little bigger as it will have more space" - the stats column itself just got 16px wider, see
    # _STATS_W).
    header_font = _inter_font(12 * scale, 800)
    value_font = _inter_font(18 * scale, 900)
    label_font = _inter_font(9 * scale, 800)
    pad_y_base = round(8 * scale)  # 11 -> 7 -> 4 -> 12 -> 8. The 12 step was for "boxes read as
    # rectangles, should be squares" - real at the time, but this function's pill stack has its own
    # NATURAL minimum height (header + 3 pills + their own padding) that target_h can only ever
    # GROW, never shrink below - so once the photo/recent-sales bar (the actual content_h driver)
    # got shrunk in a later round ("stats much less tall"), the column silently stayed stuck at its
    # old natural height instead of getting shorter, because that natural floor was now the taller
    # of the two numbers. 8 is the real fix for that: still visibly taller/squarer than the original
    # 4 (a flat 2.2:1 bar), just not so tall that the natural floor exceeds the new, shorter
    # content_h and desyncs this column from the rail/row again (see side_column_content_height).
    label_gap = round(4 * scale)  # tightened 4->1.5 in an earlier round, back to 4 (explicit ask -
    # the number and its label were now reading as too cramped together; wants real breathing room
    # between them, with the underline mark still sitting right under the label itself).
    header_pad_top = round(12 * scale)  # 0 -> 6 -> 10 -> 15 -> 20 -> 16 -> 12 (explicit ask,
    # repeated: "stats much less tall") - see pad_y_base's own comment for why this actually
    # matters now (this column's real height is the pill stack's OWN natural size, not a budget it
    # gets stretched to fill, once that natural size is the taller of the two).
    header_pad_bottom = round(5 * scale)  # 9 -> 7 -> 5, same reasoning
    base_gaps = [round(g * scale) for g in (5, 5, 5)]  # 8 -> 6 -> 5, same reasoning
    radius = round(9 * scale)

    # The small underline mark under each label (see .stat-item p:last-child::after in
    # signaturePreview.ts) - a short 14px-wide, 2px-tall rounded bar centered under the WORD, not
    # the wider pill. Sized here (not down by the drawing loop) so _pill_heights can reserve real
    # room for it below the label instead of it overflowing past the pill's own bottom edge.
    mark_w, mark_h = round(14 * scale), max(1, round(2 * scale))
    mark_radius = mark_h / 2
    mark_gap = round(6 * scale)

    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    header_bbox = probe.textbbox((0, 0), "STATS", font=header_font)
    # header_pad_top before the text, then the text itself, then a short underline mark (the same
    # one every pill's own label gets - see the per-cell loop below) instead of the old full-width
    # divider line, then header_pad_bottom before the first pill.
    header_h = header_pad_top + (header_bbox[3] - header_bbox[1]) + mark_gap + mark_h + header_pad_bottom

    def _pill_heights(pad_y: float) -> list[float]:
        heights = []
        for label, value in cells:
            vbbox = probe.textbbox((0, 0), value, font=value_font)
            lbbox = probe.textbbox((0, 0), label, font=label_font)
            heights.append(pad_y + (vbbox[3] - vbbox[1]) + label_gap + (lbbox[3] - lbbox[1]) + mark_gap + mark_h + pad_y)
        return heights

    gaps = list(base_gaps[:len(cells)])
    trailing_gap = 0.0
    pad_y = pad_y_base
    pill_heights = _pill_heights(pad_y)
    natural_h = header_h + sum(gaps) + sum(pill_heights)
    # `target_h` comes in as CSS display px (1x, same units as _PHOTO_CANVAS_H/
    # _LISTING_BAR_DISPLAY_H) - `scale` is raw-supersampled px per CSS px, so multiplying (not
    # dividing by true_scale, which would silently undercount by SUPERSAMPLE-fold) converts it into
    # the same units header_h/pill_heights are already in.
    target_h_scaled = round(target_h * scale) if target_h else 0
    if cells and target_h_scaled > natural_h:
        surplus = target_h_scaled - natural_h
        # Surplus first grows the GAPS (one before each pill, plus a new trailing one) up to a cap -
        # past that cap it grows the PILLS themselves (via pad_y, which keeps the value/label text
        # correctly centered for free, since pad_y already pads both above and below them) instead
        # of piling all of it into ever-wider gaps. Uncapped gaps were exactly why the pills read as
        # "small" next to the space around them instead of the column actually looking fuller.
        n_slots = len(cells) + 1
        # 18 -> 28 (explicit ask: "put some more space between all the stat cards and its title...
        # without changing height") - raising this cap lets more of the column's surplus height go
        # into the gaps BETWEEN the title/pills instead of overflowing into pad_y (which only ever
        # grows each pill's own internal top/bottom padding, not the visible space between them).
        max_gap_add = round(28 * scale)
        gap_add = min(max_gap_add, surplus / n_slots)
        gaps = [g + gap_add for g in gaps]
        trailing_gap = gap_add
        leftover = surplus - gap_add * n_slots
        if leftover > 0:
            pad_y = pad_y_base + leftover / (2 * len(cells))
            pill_heights = _pill_heights(pad_y)

    text_rgba, border_rgba, pill_bg_rgba = (*text_rgb, 255), (*pill_border_rgb, 255), (*pill_bg_rgb, 255)
    total_h = header_h + sum(gaps) + sum(pill_heights) + trailing_gap
    canvas = Image.new("RGBA", (w, round(total_h)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # A raster stand-in for the Live Preview's own .stat-item text-shadow (signaturePreview.ts: two
    # stacked shadows, a tight dark one plus a softer wider one) - CSS text-shadow has no raster
    # equivalent, but drawing the same glyphs in black on their own layer, blurring THAT layer, and
    # compositing it underneath the crisp real text reads the same way: a stunning, embossed pop
    # instead of flat text that can wash out against a lightly-tinted pill.
    def _shadowed_text(xy, text, font, fill):
        tight = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(tight).text((xy[0], xy[1] + round(1 * scale)), text, font=font, fill=(0, 0, 0, 217))
        tight = tight.filter(ImageFilter.GaussianBlur(max(1, round(1.5 * scale / 2))))
        soft = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(soft).text((xy[0], xy[1] + round(2 * scale)), text, font=font, fill=(0, 0, 0, 102))
        soft = soft.filter(ImageFilter.GaussianBlur(max(1, round(4 * scale / 2))))
        merged = Image.alpha_composite(canvas, tight)
        merged = Image.alpha_composite(merged, soft)
        ImageDraw.Draw(merged).text(xy, text, font=font, fill=fill)
        return merged

    hbbox = draw.textbbox((0, 0), "STATS", font=header_font)
    hx = (w - (hbbox[2] - hbbox[0])) / 2
    canvas = _shadowed_text((hx - hbbox[0], header_pad_top - hbbox[1]), "STATS", header_font, text_rgba)
    draw = ImageDraw.Draw(canvas)
    # The same short underline mark every pill's label gets below it (see the per-cell loop below),
    # not the old full-width divider line - explicit ask to make this one match those instead of
    # reading as a separate, odd style of its own.
    header_mark_y = header_pad_top + (hbbox[3] - hbbox[1]) + mark_gap
    header_mark_x = (w - mark_w) / 2
    header_mark_shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(header_mark_shadow).rounded_rectangle(
        (header_mark_x, header_mark_y + round(1 * scale), header_mark_x + mark_w, header_mark_y + round(1 * scale) + mark_h),
        radius=mark_radius, fill=(0, 0, 0, 128),
    )
    header_mark_shadow = header_mark_shadow.filter(ImageFilter.GaussianBlur(max(1, round(2 * scale / 2))))
    canvas = Image.alpha_composite(canvas, header_mark_shadow)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((header_mark_x, header_mark_y, header_mark_x + mark_w, header_mark_y + mark_h), radius=mark_radius, fill=text_rgba)
    y = header_h

    # Value text has no fixed max length - a big enough sales volume ("$100M", or worse "$1234.57M"
    # once a total crosses into the billions - see _fmt_compact_money, which has no "B" notation)
    # could render wider than the pill itself, since value_font was always one fixed size with no
    # check against the pill's actual width (explicit ask: "make... smart enough so the number
    # fits"). max_w leaves the same ~2 CSS px margin on each side the pill's own padding implies;
    # shrinking in ~6% steps down to 60% of the base size is enough range to fit even a billion-
    # dollar total without the digits reading as a visibly different, jarringly small font.
    max_value_w = w - round(12 * scale)

    def _fit_value_font(text: str):
        size = 18 * scale
        font = _inter_font(size, 900)
        bbox = probe.textbbox((0, 0), text, font=font)
        while (bbox[2] - bbox[0]) > max_value_w and size > 18 * scale * 0.6:
            size *= 0.94
            font = _inter_font(size, 900)
            bbox = probe.textbbox((0, 0), text, font=font)
        return font, bbox

    for i, (label, value) in enumerate(cells):
        y += gaps[i]
        ph = pill_heights[i]
        draw.rounded_rectangle((0, y, w, y + ph), radius=radius, outline=border_rgba, fill=pill_bg_rgba, width=max(1, round(scale / 2)))
        value_font_fit, vbbox = _fit_value_font(value)
        vx = (w - (vbbox[2] - vbbox[0])) / 2
        vy = y + pad_y
        canvas = _shadowed_text((vx - vbbox[0], vy - vbbox[1]), value, value_font_fit, text_rgba)
        draw = ImageDraw.Draw(canvas)
        lbbox = draw.textbbox((0, 0), label, font=label_font)
        lx = (w - (lbbox[2] - lbbox[0])) / 2
        ly = vy + (vbbox[3] - vbbox[1]) + label_gap
        canvas = _shadowed_text((lx - lbbox[0], ly - lbbox[1]), label, label_font, text_rgba)
        draw = ImageDraw.Draw(canvas)

        mark_y = ly + (lbbox[3] - lbbox[1]) + mark_gap
        mark_x = (w - mark_w) / 2
        shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow_layer).rounded_rectangle(
            (mark_x, mark_y + round(1 * scale), mark_x + mark_w, mark_y + round(1 * scale) + mark_h),
            radius=mark_radius, fill=(0, 0, 0, 128),
        )
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(max(1, round(2 * scale / 2))))
        canvas = Image.alpha_composite(canvas, shadow_layer)
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((mark_x, mark_y, mark_x + mark_w, mark_y + mark_h), radius=mark_radius, fill=text_rgba)
        y += ph

    true_w = round(_STATS_IMG_DISPLAY_W * true_scale)
    true_h = round(canvas.height * true_scale / scale)
    canvas = canvas.resize((true_w, true_h), Image.LANCZOS)
    return canvas


async def render_stats_png(stats: dict, accent_hex: str, dark_theme: bool = False, target_h: int = 0) -> bytes:
    """PNG encode of _compose_stats_canvas - the universal fallback inside the <picture> tag
    _build_gallery now wraps the stats image in (see render_stats_webp). Lossless, same as
    before, so every mail client that doesn't understand <picture>/WebP sees exactly what it
    always did."""
    canvas = await _compose_stats_canvas(stats, accent_hex, dark_theme=dark_theme, target_h=target_h)
    buffer = BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


async def render_stats_webp(stats: dict, accent_hex: str, dark_theme: bool = False, target_h: int = 0) -> bytes:
    """Lossless WebP encode of the same _compose_stats_canvas composition render_stats_png uses -
    deliberately lossless (not a smaller lossy quality setting), same reasoning as the "RECENT
    SALES"/stats text already being lossless PNG: this text got a LOT of attention this session
    for looking "blurry"/"low quality" at small sizes, so this only ever trades file size for
    speed of compression, never for the pixels themselves. Lossless WebP still reliably beats PNG
    by a real margin on this kind of content (flat gradient + soft shadow blur + crisp text)."""
    canvas = await _compose_stats_canvas(stats, accent_hex, dark_theme=dark_theme, target_h=target_h)
    buffer = BytesIO()
    canvas.save(buffer, format="WEBP", lossless=True, method=6)
    return buffer.getvalue()


# The main column beside the icon rail, minus its padding (no stats column in this mode).
_STATS_STRIP_DISPLAY_W, _STATS_STRIP_DISPLAY_H = 610, 58


async def _compose_stats_strip(stats: dict, accent_hex: str, dark_theme: bool = False):
    """Stats with the listings & sales strip switched off (user, 10/15: stats must work on their
    own): the right-hand stats column is taller than a card with no strip, so the same numbers
    sit in a slim strip where the listing would be - "MY STATS" then Volume / Closed / Avg pills
    side by side, white on the stats column's own accent gradient (left to right here)."""
    from PIL import Image, ImageDraw

    true_scale, scale = _STATS_IMG_SCALE, _STATS_IMG_SCALE * _STATS_SUPERSAMPLE
    w, h = round(_STATS_STRIP_DISPLAY_W * scale), round(_STATS_STRIP_DISPLAY_H * scale)
    light, deep = _stats_gradient_stops(accent_hex)
    a, b = _hex_to_rgb(light), _hex_to_rgb(deep)
    grad = Image.new("RGB", (w, 1))
    for x in range(w):
        t = x / max(1, w - 1)
        grad.putpixel((x, 0), tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    canvas = grad.resize((w, h)).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)
    border = (*_hex_to_rgb(_mix_hex(accent_hex, "#FFFFFF", 0.35)), 255)
    pill_bg = (*_hex_to_rgb(_mix_hex(accent_hex, "#FFFFFF", 0.16)), 255)
    from PIL import ImageFilter
    # Soft shadows on their own layer (the stats column's embossed look), crisp text on top.
    shade = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade)
    texts: list = []

    def text(xy, s, font, fill=white):
        shade_draw.text((xy[0], xy[1] + round(1.5 * scale)), s, font=font, fill=(0, 0, 0, 120))
        texts.append((xy, s, font, fill))

    head_font, value_font, label_font = _inter_font(11 * scale, 800), _inter_font(17 * scale, 900), _inter_font(8.5 * scale, 800)
    pad = round(12 * scale)
    # "MY STATS" with the short underline mark the column's header uses
    hb = draw.textbbox((0, 0), "MY STATS", font=head_font)
    mark_w, mark_h = round(14 * scale), max(1, round(2 * scale))
    head_h = (hb[3] - hb[1]) + round(6 * scale) + mark_h
    hy = (h - head_h) / 2
    text((pad - hb[0], hy - hb[1]), "MY STATS", head_font)
    my = hy + (hb[3] - hb[1]) + round(6 * scale)
    draw.rounded_rectangle((pad, my, pad + mark_w, my + mark_h), radius=mark_h / 2, fill=white)

    cells = [("VOLUME", _fmt_compact_money(stats["totalVolume"])), ("CLOSED", str(stats["totalSold"])), ("AVG PRICE", _fmt_compact_money(stats["avgPrice"]))]
    cells = [(lbl, v) for lbl, v in cells if v]
    left = pad + (hb[2] - hb[0]) + round(18 * scale)
    gap = round(8 * scale)
    pill_w = (w - pad - left - gap * (len(cells) - 1)) / max(1, len(cells))
    py0, py1 = round(8 * scale), h - round(8 * scale)
    for i, (lbl, v) in enumerate(cells):
        x0 = left + i * (pill_w + gap)
        draw.rounded_rectangle((x0, py0, x0 + pill_w, py1), radius=round(9 * scale), outline=border, fill=pill_bg, width=max(1, round(scale / 2)))
        vb = draw.textbbox((0, 0), v, font=value_font)
        lb = draw.textbbox((0, 0), lbl, font=label_font)
        block = (vb[3] - vb[1]) + round(4 * scale) + (lb[3] - lb[1])
        top = py0 + ((py1 - py0) - block) / 2
        text((x0 + (pill_w - (vb[2] - vb[0])) / 2 - vb[0], top - vb[1]), v, value_font)
        text((x0 + (pill_w - (lb[2] - lb[0])) / 2 - lb[0], top + (vb[3] - vb[1]) + round(4 * scale) - lb[1]), lbl, label_font)
    canvas = Image.alpha_composite(canvas, shade.filter(ImageFilter.GaussianBlur(round(2 * scale))))
    draw = ImageDraw.Draw(canvas)
    for xy, s, font, fill in texts:
        draw.text(xy, s, font=font, fill=fill)
    draw.rounded_rectangle((pad, my, pad + mark_w, my + mark_h), radius=mark_h / 2, fill=white)
    return canvas.resize((round(_STATS_STRIP_DISPLAY_W * true_scale), round(_STATS_STRIP_DISPLAY_H * true_scale)), Image.LANCZOS).convert("RGB")


async def render_stats_strip_png(stats: dict, accent_hex: str, dark_theme: bool = False) -> bytes:
    canvas = await _compose_stats_strip(stats, accent_hex, dark_theme=dark_theme)
    buffer = BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _stats_strip_html(accent: str, asset_base: str, href: str = "") -> str:
    """The stats strip's chrome - same rounded, accent-edged frame as the listing strip around one
    live image (stats-strip.png), so new sales show without a re-paste."""
    w, h = _STATS_STRIP_DISPLAY_W, _STATS_STRIP_DISPLAY_H
    light, _ = _stats_gradient_stops(accent)
    img = (f'<img src="{asset_base}/stats-strip.png" width="{w}" height="{h}" alt="My sales stats" '
           f'style="display:block;width:100%;max-width:{w}px;height:auto;border:0;" />')
    cell = f'<a href="{_esc(href)}" style="display:block;line-height:0;">{img}</a>' if href else img
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="{w}" bgcolor="{light}" '
        f'style="width:100%;max-width:{w}px;background-color:{light};border:1px solid {accent};border-radius:10px;overflow:hidden;">'
        f'<tr><td style="padding:0;line-height:0;">{cell}</td></tr></table>'
    )


def stats_as_strip(cfg: dict, hub: dict) -> bool:
    """Stats on, nothing in the listings & sales strip -> the stats strip takes its place."""
    return wants_stats(cfg) and _featured(cfg, hub) is None and compute_sales_stats(hub) is not None


def _stats_gradient_stops(accent: str) -> tuple[str, str]:
    """Light-to-dark accent stops (top to bottom) for the stats column - shared by the column's
    <td> background AND its <img>, computed once so both layers stay pixel-identical with no seam
    between them (see _stats_img_html)."""
    return _mix_hex(accent, "#FFFFFF", 0.18), _mix_hex(accent, "#000000", 0.28)


_STATS_IMG_DISPLAY_H = 242


def _stats_img_html(asset_base: str, content_h: Optional[int] = None) -> str:
    """The numbers/pills image, wrapped in a <picture> so clients that support it load the
    smaller lossless stats.webp (see render_stats_webp) instead of stats.png - any client that
    doesn't recognize <picture> (classic Outlook included) just renders the plain <img> fallback,
    exactly as before. The gradient wash it sits on lives on the OUTER stats <td> in
    _build_gallery (via _stats_gradient_stops), not a wrapping table in here, so it always covers
    the cell's real full height (set by the row's tallest cell) instead of only the height of this
    image's own content like a nested table would."""
    # Plain <img>, no <picture>/WebP wrapper any more: per caniemail.com, <picture> works in Apple
    # Mail only - Gmail rewrites the tags to <u></u>, every Outlook ignores them - so the wrapper
    # only ever cost characters against Gmail's 10,000-character signature limit. Explicit height
    # too: Outlook desktop sizes images from the width/height ATTRIBUTES, not CSS.
    img_h = _STATS_IMG_DISPLAY_H if content_h is None else content_h
    return (f'<img src="{asset_base}/stats.png" width="{_STATS_IMG_DISPLAY_W}" height="{img_h}" alt="My sales stats" '
            f'style="display:block;width:{_STATS_IMG_DISPLAY_W}px;height:{img_h}px;border:0;" />')


def _listing_bar_html(accent: str, divider_color: str, asset_base: str, href: str = "", card_bg: str = CARD_BG, dark: bool = False, sales_header: bool = False) -> str:
    """The recent-sale/featured-listing strip: a bordered, accent-tinted container (static
    template chrome, safe as real HTML) wrapping ONE image that has the photo, status pill,
    address, price and specs all baked in together - see render_listing_bar_jpeg/webp and the
    _LISTING_BAR_DISPLAY_W comment for why that has to be one live-fetched image rather than a
    photo plus separate HTML text: only that guarantees an already-installed signature keeps
    showing whichever listing/sale is current, with no re-copy-paste. When `href` is given (the
    stable /go/{owner_id}/sales or /listings redirect) the whole strip is a real link through to
    that public page - previously this image wasn't clickable at all. The background is the same
    two-stop accent wash the Live Preview uses (see _card_wash_bg) instead of a flat tint - a solid
    `bgcolor` fallback (the lighter stop) still covers classic Outlook, which won't render the
    gradient at all.

    Both images here (the cards image, and the header image in sales-header mode) are wrapped in
    a <picture> with a WebP <source> ahead of the plain <img> fallback (explicit ask: "do what you
    want with the webp to save way more space") - in recent-sales mode that WebP is the
    real ANIMATED version (render_recent_sales_carousel_webp), at roughly half the GIF's old file
    size with noticeably CLEANER motion (no palette banding, since WebP isn't stuck with
    GIF's 256-color limit). A client that doesn't understand <picture> or animated WebP - notably
    classic desktop Outlook, which never animated the old GIF either - just renders the plain
    <img>, which is the fully static JPEG (render_recent_sales_carousel_jpeg/render_listing_bar_
    jpeg): no swipe/scroll animation there at all, always exactly 3 sales, which is also the
    explicit ask ("no 4th recent sale swiping in").

    `sales_header` (recent-sales mode only) adds a second, separate <picture> row above the cards
    image for the "RECENT SALES" label - see render_recent_sales_header_png for why that text was
    split out of the shared listing image into its own small lossless PNG/WebP instead of baked
    into the top of it (a GIF's limited palette kept visibly banding/quantizing that text no matter
    how it was drawn). The cards image itself is shorter by exactly that header's own height (see
    render_recent_sales_carousel_gif/jpeg/webp), so the two stacked rows still add up to the same
    total _SALE_MINI_BAR_DISPLAY_H the recent-sales mode uses (its own, slightly taller than the
    featured-listing strip's fixed _LISTING_BAR_DISPLAY_H - see that constant's own comment)."""
    light, dark_stop = _card_wash_bg(accent, dark=dark)
    # Recent-sales mode uses its own narrower/taller _SALE_MINI_BAR_DISPLAY_W/H and a FIXED (not
    # width:100%) outer table - explicit, repeated ask was for this card to end close to where the
    # button row above it ends, not stretch all the way out to the stats column the way the
    # featured-listing strip (which keeps width:100%, unaffected) does.
    w, h = (_SALE_MINI_BAR_DISPLAY_W, _SALE_MINI_BAR_DISPLAY_H) if sales_header else (_LISTING_BAR_DISPLAY_W, _LISTING_BAR_DISPLAY_H)
    outer_width_attr = f'width="{w}"' if sales_header else 'width="100%"'
    outer_width_style = f'width:{w}px;' if sales_header else ''
    header_row = ""
    if sales_header and _SALE_MINI_HEADER_H_DISPLAY > 0:
        header_h = _SALE_MINI_HEADER_H_DISPLAY
        h -= header_h
        header_img = (
            f'<img src="{asset_base}/sales-header.png" width="{w}" height="{header_h}" alt="" '
            f'style="display:block;width:100%;max-width:{w}px;height:auto;border:0;" />'
        )
        header_row = f'<tr><td style="padding:0;line-height:0;">{header_img}</td></tr>'
    img = (
        f'<img src="{asset_base}/listing.jpg" width="{w}" height="{h}" alt="{"View my recent sales" if sales_header else "View my featured listing"}" '
        f'style="display:block;width:100%;max-width:{w}px;height:auto;border:0;" />'
    )
    # Plain <img> (no <picture>/WebP) - see _stats_img_html for why.
    cell = f'<a href="{_esc(href)}" style="display:block;line-height:0;">{img}</a>' if href else img
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" {outer_width_attr} bgcolor="{light}" '
        f'style="{outer_width_style}background:linear-gradient(135deg,{light} 0%,{dark_stop} 100%);background-color:{light};'
        f'border:1px solid {divider_color};border-left:3px solid {accent};border-radius:10px;overflow:hidden;">'
        f'{header_row}<tr><td style="padding:0;line-height:0;">{cell}</td></tr></table>'
    )


def _build_gallery(cfg: dict, hub: dict, owner: User, redirect_base_url: str, asset_base: str, accent: str, listing_link_override: Optional[str] = None) -> str:
    """The single signature design: a left icon rail, a headshot + identity block (name, optional
    verified badge, title/company/team, inline contact row, action buttons), a recent-sale/
    featured-listing strip below that, and an optional right-edge stats column - all inside one
    card. Real <img>/text throughout, no background-image tricks."""
    identity = _resolved_identity(cfg, hub, owner)
    links = _resolved_links(cfg, hub, redirect_base_url)
    font_stack = _font_stack(cfg)
    # Customize tab's dark-mode toggle, read fresh from the saved config on every render (never
    # baked into the install HTML at copy time) - see _theme_colors/theme_card_bg.
    card_bg, card_border, text_color, label_color = _theme_colors(bool(cfg.get("darkMode")))
    luxe_edge = ""
    if cfg.get("luxe"):
        # Luxe: a gold border and a thin gold edge along the top (the Spotlight header's gold
        # sweep). A plain table row with a flat bgcolor - every client draws it; the gradient is
        # a bonus where supported.
        _m = luxe_metal(cfg.get("luxeMetal"))
        card_border = _m["border_dark"] if cfg.get("darkMode") else _m["border_light"]

    photo_html = ""
    if identity["headshot"] or identity["name"]:
        # "Save Contact" is baked directly into the bottom of the photo GIF itself (see
        # render_photo_gif/_bake_save_contact_bar) rather than a separate row below it - so it
        # overlaps the photo with zero added card height. The click target is a plain HTML image
        # map over that same baked-in rect - <map>/<area> is old enough HTML to still work in
        # classic Outlook's Word-based renderer, unlike CSS position:absolute. Links through the
        # same stable /go/{owner_id}/vcard redirect the vcardUrl in _resolved_links describes, so
        # it always hands out whatever the agent's CURRENT Contact Card settings say, with no
        # re-paste ever needed just because a phone number or photo changed in Settings.
        # The whole photo (with its baked-in "SAVE CONTACT" bar) is one plain link to the stable
        # /go/{owner_id}/vcard redirect. It used to be an HTML image map over just the bar - but
        # <map>/<area> is exactly the kind of markup email sanitizers drop, and a plain <a> around an
        # <img> is the single most reliably clickable thing in every client, Gmail and Outlook
        # desktop included. Clicking anywhere on the photo saving the contact is the natural
        # reading anyway. No <picture>/WebP wrapper - see _stats_img_html.
        photo_img = (
            f'<img src="{asset_base}/photo.png" width="{_PHOTO_CANVAS_W}" height="{_PHOTO_CANVAS_H}" '
            f'alt="{_esc(identity["name"])}" style="display:block;width:{_PHOTO_CANVAS_W}px;height:{_PHOTO_CANVAS_H}px;border:0;" />'
        )
        photo_picture = (
            f'<a href="{_esc(links["vcardUrl"])}" style="display:block;line-height:0;">{photo_img}</a>'
            if links.get("vcardUrl") else photo_img
        )
        photo_html = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="{_PHOTO_CANVAS_W}">'
            f'<tr><td>{photo_picture}</td></tr></table>'
        )

    # Name/title, contact row, and buttons all live in the SAME column beside the photo -
    # matching the Live Preview's flex layout (see .gallery-main > .flex:first-child in
    # signaturePreview.ts), instead of contact/buttons dropping to their own full-width row below
    # the photo. That "below the photo" arrangement was a deliberate email-table-width tradeoff at
    # the time, but it made the installed signature's orientation visibly diverge from the design
    # agents actually approve in the Studio preview - contact info and CTAs read as landing under
    # the wrong section (visually attached to the listing card below, not the identity above).
    # _contact_inline_html/_action_row_html already wrap naturally onto their own lines at this
    # narrower ~260px column width (no fixed-width/nowrap assumption spans the photo+column width
    # the way the old below-the-photo full-width version did), so nothing clips.
    identity_parts = []
    if identity["name"]:
        badge_html = (
            ' <span style="display:inline-block;width:15px;height:15px;line-height:15px;text-align:center;'
            'background-color:#1685E5;background-image:linear-gradient(#1685E5,#1685E5);color:#ffffff;border-radius:50%;font-size:9px;font-weight:900;vertical-align:middle;">&#10003;</span>'
        ) if cfg.get("showVerifiedBadge") else ""
        identity_parts.append(f'<div style="font-family:{font_stack};font-size:21px;font-weight:800;letter-spacing:-0.3px;color:{text_color};">{_esc(identity["name"])}{badge_html}</div>')
    sub_bits = [b for b in [identity["title"], identity["company"], identity["team"]] if b]
    if sub_bits:
        identity_parts.append(f'<div style="font-family:{font_stack};font-size:11px;font-weight:600;color:{label_color};margin-top:3px;">{" &middot; ".join(_esc(b) for b in sub_bits)}</div>')

    contact_html = _contact_inline_html(identity, text_color, label_color, card_border, font_stack)
    if contact_html:
        identity_parts.append(f'<div style="margin-top:4px;">{contact_html}</div>')

    ctas = _action_row_html(links, cfg["ctaButtons"], accent, redirect_base_url, font_stack, card_bg=card_bg)
    if ctas:
        # 5/6/7 -> 4/5/5 -> 3/4/4 (explicit ask, repeated: "shrink height quite a good amount") -
        # this identity column (name+subtitle+contact+buttons stacked) is tall enough to be the
        # row's real height driver, past what _PHOTO_CANVAS_H/content_h assumes it is - trimming
        # these three margins pulls its real height down closer to the photo's own, which is what
        # actually lets the row (and everything below it, including the recent-sales bar) sit
        # shorter, not just cosmetic tightening.
        identity_parts.append(f'<div style="margin-top:4px;">{ctas}</div>')

    identity_cells = []
    if photo_html:
        # 16px -> 10px (explicit ask: "name, job, number, email and buttons can all move closer to
        # the photo section") - measured safe: even at 10px the button row still has ~32px of real
        # margin (see _CARD_WIDTH's own comment on that column's bare-minimum width).
        # Left/right padding both 4 now (was 0/10 - too lopsided, then a broken attempt at 8/10
        # that also silently OVER-claimed width: the declared td width only added the +8 left pad,
        # not the existing +10 right pad too, so real content (114+8+10=132) exceeded the declared
        # 122 and some clients expand a table column to fit content that's wider than its own
        # declared width, eating even more from the identity column next to it than intended - the
        # direct cause of "What's My Home Worth" wrapping to its own line. 4/4 is even AND small
        # (explicit ask: "way too much white space... on right and left side now"), and the
        # declared width below is the exact real total (114 canvas + 4 + 4), so there's no longer
        # any silent overflow for a client to "helpfully" expand.
        # Left 4 -> 6, right stays 4 - still a small, even-ish split (total extra padding still
        # just 10px), and the declared width below is the exact real total (114 canvas + 6 + 4),
        # so there's no silent overflow for a client to "helpfully" expand into the identity
        # column next to it. The row's own up/right position nudge lives entirely in the
        # `position:relative` wrapper below now, not in this td's own padding - see
        # _IDENTITY_ROW_OFFSET_TOP/LEFT's own comment.
        identity_cells.append(f'<td valign="top" width="{_PHOTO_CANVAS_W + 10}" style="padding:0 4px 0 6px;">{photo_html}</td>')
    identity_cells.append(f'<td valign="top" style="padding-top:2px;">{"".join(identity_parts)}</td>')
    header_row = f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"><tr>{"".join(identity_cells)}</tr></table>'
    # `position:relative` + a negative top/positive left offset - shifts only where this row PAINTS,
    # not the box it occupies in normal flow, so the recent-sales bar below it (and the rail/stats
    # columns, sized independently via content_h) are completely unaffected by this move. Degrades
    # gracefully in clients with no position support (classic desktop Outlook): the row just renders
    # at its unshifted position instead of a broken layout.
    header_row = f'<div style="position:relative;top:{_IDENTITY_ROW_OFFSET_TOP}px;left:{_IDENTITY_ROW_OFFSET_LEFT}px;">{header_row}</div>'

    below_parts = [header_row]

    featured = _featured(cfg, hub)
    if featured:
        featured_mode, _ = featured
        if featured_mode in ("listing", "sale_feature"):
            # The "smart QR" link, resolved by the caller (email_signature_routes.py) via
            # property_website_service.resolve_listing_link before this HTML is built: the
            # agent's own published Listing Spotlight for this exact property when they have
            # one, else the generic one-page property view (same page the Similar Properties
            # Sheet's per-comp QR codes link to). Falls back to the plain Listings page redirect
            # only if that resolution came back empty (e.g. a manual listing with no Spotlight).
            banner_border = card_border
        else:
            # Recent Sales gets an accent-tinted outline (not the neutral tan/border color the
            # Featured Listing strip uses) - it reads as its own distinct, branded block rather
            # than the same generic card chrome everywhere.
            banner_border = accent
        # Both modes now go through the same <picture> (WebP animated/static + JPEG static
        # fallback) built inside _listing_bar_html - see its own docstring for why GIF's no longer
        # in this path at all (WebP replaced it as the animated format, JPEG replaced it as the
        # universal fallback).
        # One stable link for BOTH modes: /go/{owner_id}/featured works out at click time whether
        # the strip is currently showing a featured listing (-> that listing's Spotlight/property
        # page) or recent sales (-> the Sales page). The strip IMAGE already flips live between the
        # two when a listing sells; a mode-specific href baked in at copy time would keep pointing
        # at the old destination until the agent re-pasted. `listing_link_override` is no longer
        # needed here (kept in the signature for callers).
        banner_href = f"{redirect_base_url}/featured" if redirect_base_url else ""
        banner_html = _listing_bar_html(accent, banner_border, asset_base, banner_href, card_bg=card_bg, dark=bool(cfg.get("darkMode")), sales_header=featured_mode == "sale")
        below_parts.append(f'<div style="margin-top:{_LISTING_MARGIN_TOP}px;">{banner_html}</div>')
    strip = (not featured) and stats_as_strip(cfg, hub)
    if strip:
        strip_href = f"{redirect_base_url}/sales" if redirect_base_url else ""
        below_parts.append(f'<div style="margin-top:{_LISTING_MARGIN_TOP}px;">{_stats_strip_html(accent, asset_base, strip_href)}</div>')

    body_html = "".join(below_parts)

    # Knowable ahead of time from just the photo/listing presence - see side_column_content_height
    # (the /stats.png route uses the same formula independently, since it can't see these locals).
    listing_bar_h = _SALE_MINI_BAR_DISPLAY_H if (featured and featured_mode == "sale") else _LISTING_BAR_DISPLAY_H
    content_h = (_PHOTO_CANVAS_H if photo_html else 0) + (_LISTING_MARGIN_TOP + listing_bar_h if featured else 0) + (_LISTING_MARGIN_TOP + _STATS_STRIP_DISPLAY_H if strip else 0)
    # The strip's own 1px top/bottom border isn't part of listing_bar_h - counted here so every
    # column's height is computed against the row's REAL height, not a couple of pixels short.
    bar_border = 2 if (featured or strip) else 0
    # The row's one true height. The rail pill, the stats column and the main cell are all sized to
    # exactly this - before, the rail's spacer math ignored its own padding/border and came out ~8px
    # taller than everything else, which (being the tallest cell) stretched the whole card and left
    # the rail + stats running on past the bottom of Recent Sales.
    row_h = _MAIN_CELL_PAD_V + content_h + bar_border + _MAIN_CELL_PAD_BOTTOM
    rail_pad_top, rail_pad_bottom = _MAIN_CELL_PAD_V - _RAIL_INSET, 6
    rail_pill_h = row_h - _RAIL_INSET * 2
    rail_icons_h = rail_pill_h - 4 - rail_pad_top - rail_pad_bottom  # minus the pill's 2px border, top+bottom

    rail_html = _rail_icons_html(links, asset_base, card_bg, rail_icons_h)
    # The right-hand stats column only sits beside a listing/sales strip; without one the stats
    # are the strip itself (above).
    stats = compute_sales_stats(hub) if (wants_stats(cfg) and featured) else None
    # Explicit width, not left to the table's own auto-layout - with none of main_cell's own rows
    # (name/contact/buttons) forcing a width, the browser was sizing this column down to whatever
    # its widest row actually needed, so the Recent Sales/Listing strip's own width:100% filled
    # THAT (narrower) measured width instead of the real space left over after the rail and stats
    # columns - visible as a gap of bare card background between it and the stats column that
    # wasn't actually available to anything else. _CARD_WIDTH minus the rail's own width+border and
    # the stats column's width, whichever of those two are actually present.
    main_width = _CARD_WIDTH
    main_padding_left = 20
    if rail_html:
        main_width -= _RAIL_OUTER_W
        main_padding_left = 4  # 20 -> 8 -> 4 (explicit ask: "move the entire save contact and
        # photo section... over to the left a little") - main_width itself is untouched, this just
        # shifts where its content sits within that same width.
    if stats:
        main_width -= _STATS_W
    # _MAIN_CELL_PAD_V top/bottom, tighter than even the Live Preview's own 16px .gallery-main
    # padding (signaturePreview.ts) - part of a broader pass tightening every internal gap (contact/
    # button/listing margins, the photo's own frame padding, the stats pills) to bring the card's
    # overall height down, not just match the preview's own padding number 1:1. Shared with the
    # rail/stats <td>s' own padding below so all three columns' content lines up exactly.
    # Right padding trimmed again, 20 -> 12 -> 6 -> 4 - "brings in" the recent-sales/listing
    # banner's own border even closer to the stats column instead of leaving a gap of bare card
    # background between them (explicit ask: "recent sales then stats right after so not much
    # space there").
    main_cell = f'<td valign="top" width="{main_width}" style="padding:{_MAIN_CELL_PAD_V}px 4px {_MAIN_CELL_PAD_BOTTOM}px {main_padding_left}px;">{body_html}</td>'

    # The icon rail's accent OUTLINE (not filled) and rounded pill shape live on a fixed-HEIGHT
    # inner table (see _RAIL_INSET), not directly on the outer <td> - a <td>'s own padding can't
    # pull its own border away from the card's edge, so the actual bordered pill is nested one level
    # in, sized to the row's own known total height (content_h + top/bottom padding) minus the inset
    # on each side. The outer <td> is plain and card_bg-colored, giving the small gap on 3 sides
    # (top/bottom/left) that keeps the pill's border from touching the card's own outer edge.
    row_cells = []
    if rail_html:
        rail_light, rail_dark = _card_wash_bg(accent, dark=bool(cfg.get("darkMode")))
        rail_h = max(_BADGE_SIZE + 20, rail_pill_h)
        # Same two-layer look as the Site version's rail (_cardWashBg): the diagonal accent wash
        # plus a soft accent glow. Clients that render CSS gradients (Gmail, Apple Mail, new
        # Outlook) get the real thing; classic Outlook ignores the gradients and falls back to
        # `bgcolor`, which is now a flat blend of that glow and the wash - a lighter, accent-tinted
        # solid instead of the plain near-neutral stop it used to fall back to.
        rail_glow = _accent_glow_hex(accent, dark=bool(cfg.get("darkMode")))
        rail_flat = _mix_hex(rail_glow, rail_light, 0.5)
        row_cells.append(
            f'<td valign="top" align="center" width="{_RAIL_OUTER_W}" bgcolor="{card_bg}" style="background:{card_bg};padding:{_RAIL_INSET}px 0 {_RAIL_INSET}px {_RAIL_INSET}px;">'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="{_BADGE_SIZE + 20}" height="{rail_h}" bgcolor="{rail_flat}" '
            f'style="background-color:{rail_flat};background-image:radial-gradient(circle at 14% 50%,{rail_glow} 0%,rgba(0,0,0,0) 58%),linear-gradient(135deg,{rail_light} 0%,{rail_dark} 100%);border:2px solid {accent};border-radius:999px;">'
            # Top padding here is 4px less than the bottom/main_cell's own (_MAIN_CELL_PAD_V) -
            # the rail's OUTER <td> already spends _RAIL_INSET (4px) as a top gap of its own (see
            # above), so without this the rail's actual icon content started 4px lower than the
            # photo beside it (10 vs 14px from the card's top edge) - explicit ask was for the
            # photo to line up with where the rail starts.
            f'<tr><td valign="top" align="center" style="padding:{rail_pad_top}px 6px {rail_pad_bottom}px;">{rail_html}</td></tr></table></td>'
        )
    row_cells.append(main_cell)

    if cfg.get("luxe"):
        _m = luxe_metal(cfg.get("luxeMetal"))
        luxe_edge = (
            f'<tr><td colspan="{len(row_cells) + (1 if stats else 0)}" height="3" bgcolor="{_m["base"]}" '
            f'style="height:3px;line-height:3px;font-size:0;mso-line-height-rule:exactly;background-color:{_m["base"]};'
            f'background-image:linear-gradient(90deg,{_m["deep"]} 0%,{_m["light"]} 50%,{_m["deep"]} 100%);">&nbsp;</td></tr>'
        )

    if stats:
        # No inset on ANY side any more (top/right/bottom all removed in turn - explicit ask: "fill
        # all the space... to the top and bottom and right side" instead of reading as a smaller
        # rectangle floating inside its own column) - the gradient now runs genuinely edge-to-edge
        # against the card's own right/top/bottom border, the exact "resting against the wall" look
        # the original inset was deliberately added to avoid, since that's now what was asked for.
        # stats_h is just the row's own full height, nothing subtracted.
        stats_light, stats_dark = _stats_gradient_stops(accent)
        stats_h = max(1, row_h)
        # stats.png is exactly content_h tall (see side_column_content_height), so whatever's left
        # of the row goes under it - keeps this cell from ever being the one that sets the height.
        stats_pad_bottom = max(0, row_h - _MAIN_CELL_PAD_V - content_h)
        # Explicit ask, repeated ("for the 1 millionth time... accent color should go all the way
        # down to the bottom of signature"): stats_h is only ever a PREDICTION of the row's real
        # height (main_cell's own natural content - photo/identity block/recent-sales banner - can
        # legitimately end up taller than side_column_content_height's estimate, e.g. a longer
        # name/title/company/team line or a 3rd CTA button wrapping). Tables auto-stretch every
        # cell in a row to match the tallest one, so whenever main_cell wins that race, this
        # column's OUTER <td> (below) ends up genuinely taller than the inner nested table that
        # used to be the ONLY thing carrying the gradient - the leftover strip of the outer <td>'s
        # own plain `card_bg` showed through below it, which is exactly the "then shows white or
        # black space at the bottom" bug (card_bg IS white in light theme, near-black in dark).
        # Root-cause fix, not another height guess: the gradient now lives on the OUTER <td> itself
        # too, not just the inner table - a <td>'s own background always covers its real rendered
        # height automatically, with no prediction involved, so there is no longer any height this
        # column could stretch to where the accent doesn't reach. The inner table is kept only for
        # its own explicit height (still needed to space the pills across whatever stats_h predicts)
        # and its rounded right corners; its background is now redundant with the outer one but
        # left in place as the correct-in-the-common-case value, not load-bearing any more.
        row_cells.append(
            f'<td valign="top" align="center" width="{_STATS_W}" bgcolor="{stats_light}" '
            f'style="background-color:{stats_light};background-image:linear-gradient(180deg,{stats_light} 0%,{stats_dark} 100%);padding:0;border-radius:0 15px 15px 0;">'
            # Matching top-right/bottom-right corner radius (15px, 1px less than the outer card's
            # own 16px to sit just inside its border) - without this, the outer card's own
            # `overflow:hidden` rounding was clipping a small rounded triangle out of the stats
            # gradient's own square corners, revealing the plain card_bg color underneath right at
            # the card's own top-right/bottom-right corners ("i still see some non accent color to
            # the right and bottom... i need edge to edge... so i dont see breakup lines", explicit
            # ask - a real rendering bug, not just a style preference).
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="{_STATS_W}" height="{stats_h}" bgcolor="{stats_light}" '
            f'style="background-color:{stats_light};background-image:linear-gradient(180deg,{stats_light} 0%,{stats_dark} 100%);border-radius:0 15px 15px 0;">'
            f'<tr><td valign="top" align="center" style="padding:{_MAIN_CELL_PAD_V}px 8px {stats_pad_bottom}px;text-align:center;">{_stats_img_html(asset_base, content_h)}</td></tr></table></td>'
        )

    return (
        # Both the `width` HTML attribute AND the CSS `width` - Outlook's Word-based renderer
        # ignores CSS max-width on tables entirely and needs the attribute to size correctly;
        # modern clients read the CSS. Together they guarantee the same ~600px card everywhere
        # instead of one client rendering it full-bleed/content-shrunk while others cap it. The
        # box-shadow is a progressive enhancement - ignored harmlessly by clients that don't
        # support it, giving the card real lift in the ones that do (Gmail, Apple Mail, new
        # Outlook, Outlook.com).
        # border-collapse:separate (not collapse) - the icon rail <td> now carries its own real
        # border (see row_cells above), and `collapse` was merging that border with THIS table's
        # own outer border wherever they touched (the rail's left/top/bottom edges), with the
        # outer table's thinner 1px border silently winning there and swallowing the rail's accent
        # outline on 3 of its 4 sides. `separate` with zero spacing renders both independently, with
        # no visible gap between them, instead.
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="{_CARD_WIDTH}" bgcolor="{card_bg}" '
        f'style="background:{card_bg};border-collapse:separate;border-spacing:0;width:{_CARD_WIDTH}px;min-width:{_CARD_WIDTH}px;max-width:{_CARD_WIDTH}px;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;border:1px solid {card_border};'
        f'border-radius:16px;overflow:hidden;box-shadow:0 12px 26px -12px rgba(35,31,24,0.32);">'
        f'{luxe_edge}'
        f'<tr>{"".join(row_cells)}</tr></table>'
    )


def build_signature_html(
    signature_config: dict,
    agent_hub_config: dict,
    owner: User,
    redirect_base_url: str,
    asset_base_url: str,
    listing_link_override: Optional[str] = None,
) -> str:
    """`asset_base_url` is the `.../email-signature/public/{token}` prefix every server-rendered
    image (photo/badges/listing) hangs off of - a single owner-scoped base rather than one URL per
    asset. `redirect_base_url` (`.../email-signature/go/{owner_id}`) is the equivalent base for
    every clickable link (Agent Hub button, form-linked CTA buttons) - see _resolved_links/
    _action_row_html for why these go through a stable redirect instead of a directly-resolved
    URL. `listing_link_override` is the "smart QR" destination for a featured-listing banner -
    resolved by the caller (email_signature_routes.py, which has DB access this pure-rendering
    module deliberately doesn't) via property_website_service.resolve_listing_link - see
    _build_gallery's banner_href."""
    cfg = resolve_signature_config(signature_config)
    hub = agent_hub_config or {}
    accent = cfg.get("accentColor") or DEFAULT_ACCENT
    return _build_gallery(cfg, hub, owner, redirect_base_url, asset_base_url, accent, listing_link_override)


def signature_plan_allowed(owner) -> bool:
    """The agent's plan includes Email Signature (IDX / No-MLS Premium)."""
    from app.utils.auth import EMAIL_SIGNATURE_ACCESS_TIERS
    tier = getattr(owner, "plan_tier", None)
    return (tier.value if hasattr(tier, "value") else tier) in EMAIL_SIGNATURE_ACCESS_TIERS


async def has_signature_card(db, owner) -> bool:
    """True when this agent's public pages can show their Email Signature card: their plan has
    signatures and they've saved one. Blog articles use it in place of the small contact card."""
    if not owner or not signature_plan_allowed(owner):
        return False
    from sqlalchemy import select
    from app.models.database import EmailSignature
    sig = (await db.execute(select(EmailSignature.config).where(EmailSignature.owner_id == owner.id))).scalar_one_or_none()
    return bool(sig)
