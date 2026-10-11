"""Template 1 - the one detailed, fixed website template (see Website Builder Phase 3 spec:
"one highly polished fixed design system and page structure that can be applied to any agent").

This is NOT AI-generated per agent - it's a deterministic composition of the same real OHP data
adapters every other part of Website Builder already uses (content_adapters.py), arranged into a
fixed page/block sequence. Every agent using Template 1 gets the identical structure; only the
data filling it in differs.

Regeneration safety: every block this composer writes is tagged `data._template_generated: true`.
Re-applying the template (e.g. after an agent adds new served areas) only touches blocks that are
STILL tagged that way - the moment an agent hand-edits a block in the editor
(BlockEditorPanel.updateBlockData/updateBlock), the frontend clears the flag, and this composer
then leaves that block alone on every future re-apply. Blocks/pages an agent adds that aren't
part of the template at all are never touched either way.
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import User
from app.services.website import content_adapters

# Stable ids (not random) so re-applying the template can recognize "this is the same generated
# block as last time" vs. "the agent added this themselves" or "the agent customized this one".
_HOME_PAGE_ID = "page-home"
_ABOUT_PAGE_ID = "page-about"
_SOLD_PAGE_ID = "page-sold"
_CONTACT_PAGE_ID = "page-contact"


def _block(block_id: str, block_type: str, data: dict | None = None) -> dict:
    return {"id": block_id, "type": block_type, "hidden": False, "data": {**(data or {}), "_template_generated": True}}


def _page(page_id: str, slug: str, title: str, is_home: bool, blocks: list[dict]) -> dict:
    return {"id": page_id, "slug": slug, "title": title, "is_home": is_home, "blocks": blocks}


async def _has_content(coro) -> bool:
    items = await coro
    return bool(items)


async def compose_starter_pages(db: AsyncSession, user: User) -> list[dict]:
    """Builds the fixed Template 1 page set, including a block only where there's real content
    behind it (or an evergreen block like Hero/About/Contact that's always meaningful)."""
    profile = await content_adapters.get_agent_profile(db, user)
    has_listings = await _has_content(content_adapters.get_active_listings(db, user, 1))
    has_sales = await _has_content(content_adapters.get_recent_sales(db, user, 1))
    has_open_houses = await _has_content(content_adapters.get_open_houses(db, user, 1))
    has_blog = await _has_content(content_adapters.get_blog_posts(db, user, 1))
    has_areas = await _has_content(content_adapters.get_served_areas_summary(db, user, 1))
    has_spotlights = await _has_content(content_adapters.get_published_spotlights(db, user, 1))
    forms = await content_adapters.get_forms_list(db, user)
    # The Contact page's form is the general "How Can I Help?" premade form (the one that fits a
    # visitor who hasn't said whether they're buying or selling), then the Buying/Selling/Home
    # Value forms, then whatever the agent has.
    contact_form = (
        next((f for f in forms if f["form_type"] == "how_can_i_help"), None)
        or next((f for f in forms if f["form_type"] in ("buying", "selling", "home_value")), None)
        or (forms[0] if forms else None)
    )

    home_blocks = [
        _block("b-hero", "hero", {
            "headline": "Find Your Dream Home",
            # No default subheadline - the eyebrow above the headline already shows the agent's
            # market text (see content_adapters.hydrate_page_blocks' hero branch), so defaulting
            # this to the same text would repeat it. Still a real, agent-editable field - just
            # empty until they choose to put something else here.
            "subheadline": "",
            "photo_source_mode": "auto",
        }),
    ]
    if has_listings:
        home_blocks.append(_block("b-featured-listings", "featured_listings", {"headline": "Featured Listings"}))
    if has_spotlights:
        home_blocks.append(_block("b-spotlights", "spotlights", {"headline": "Listing Spotlights"}))
    if has_areas:
        home_blocks.append(_block("b-communities", "communities", {"headline": "Communities We Serve"}))
    home_blocks.append(_block("b-about-agent", "about_agent"))
    if has_sales:
        home_blocks.append(_block("b-recent-sales", "recent_sales", {"headline": "Recently Sold"}))
    if has_open_houses:
        home_blocks.append(_block("b-open-houses", "open_houses", {"headline": "Upcoming Open Houses"}))
    if has_blog:
        home_blocks.append(_block("b-blog-posts", "blog_posts", {"headline": "Latest Articles", "limit": 3}))
    # No Contact Band on the home page - the site footer's "Let's talk about your next move" band
    # closes every home page (owner's call, 10/07; SitePageRenderer also skips one there).

    about_blocks = [
        _block("b-about-hero", "about_agent"),
        # Present but OFF by default - the agent flips it on in the editor once they have reviews
        # (client-submitted or imported from Google). Renders nothing while there are none.
        {**_block("b-about-testimonials", "testimonials", {"headline": "What Clients Say"}), "hidden": True},
        _block("b-about-contact", "contact_cta", {"headline": "Book a Consultation", "phone": profile.get("phone") or "", "email": profile.get("email") or ""}),
    ]

    sold_blocks = [
        _block("b-sold-heading", "heading_text", {"text": "Recently Sold", "level": "h1"}),
        _block("b-sold-sales", "recent_sales", {"limit": 12}),
    ]

    contact_blocks = [
        _block("b-contact-heading", "heading_text", {"text": "Get in Touch", "level": "h1"}),
    ]
    if contact_form:
        contact_blocks.append(_block("b-contact-form", "form", {"form_id": contact_form["id"]}))
    else:
        contact_blocks.append(_block("b-contact-cta-page", "contact_cta", {"phone": profile.get("phone") or "", "email": profile.get("email") or ""}))

    pages = [
        _page(_HOME_PAGE_ID, "", "Home", True, home_blocks),
        _page(_ABOUT_PAGE_ID, "about", "About", False, about_blocks),
        _page(_CONTACT_PAGE_ID, "contact", "Contact", False, contact_blocks),
    ]
    if has_sales:
        pages.append(_page(_SOLD_PAGE_ID, "sold", "Sold", False, sold_blocks))
    return pages


def _merge_page(existing: dict | None, generated: dict) -> dict:
    """Regeneration-safety merge for one page: a block the agent hand-edited (its
    `_template_generated` flag was cleared, see BlockEditorPanel) is kept exactly as the agent
    left it; anything else generated is refreshed; any extra block the agent added that isn't
    part of the template at all is preserved and kept after the generated ones."""
    if not existing:
        return generated

    existing_blocks_by_id = {b["id"]: b for b in existing.get("blocks", [])}
    generated_ids = {b["id"] for b in generated["blocks"]}

    merged_blocks = []
    for gen_block in generated["blocks"]:
        current = existing_blocks_by_id.get(gen_block["id"])
        if current and current.get("data", {}).get("_template_generated") is False:
            merged_blocks.append(current)  # agent hand-edited this one - never overwrite it
        else:
            merged_blocks.append(gen_block)

    # Blocks the agent added that aren't part of the template at all - keep them, appended.
    for block in existing.get("blocks", []):
        if block["id"] not in generated_ids:
            merged_blocks.append(block)

    merged = dict(existing)
    merged["blocks"] = merged_blocks
    return merged


async def apply_starter_template(db: AsyncSession, site, user: User) -> dict:
    """Returns the new draft_data (caller is responsible for saving it - see site_routes.py).
    Merges into whatever draft already exists rather than blindly overwriting, so re-applying
    (e.g. after adding a newly served area) is always safe to call."""
    generated_pages = await compose_starter_pages(db, user)
    existing_pages_by_id = {p["id"]: p for p in (site.draft_data or {}).get("pages", [])}

    merged_pages = [_merge_page(existing_pages_by_id.get(p["id"]), p) for p in generated_pages]

    # Any existing page that isn't one of the template's own (a fully custom page the agent
    # built themselves) is preserved untouched, appended after the template pages.
    generated_page_ids = {p["id"] for p in generated_pages}
    for page in (site.draft_data or {}).get("pages", []):
        if page["id"] not in generated_page_ids:
            merged_pages.append(page)

    draft = dict(site.draft_data or {})
    draft["pages"] = merged_pages
    # First-time setup only: an agent with a thin personal inventory gets a Featured page built
    # from every home for sale in the towns they serve (their own listings first) instead of a
    # near-empty page - they can switch it on the Featured tab any time.
    if not draft.get("featured_settings"):
        own = await content_adapters.get_active_listings(db, user, 3)
        served = await content_adapters.get_served_areas_summary(db, user, 1)
        draft["featured_settings"] = {
            "mode": "served_areas" if (len(own) < 3 and served) else "my_listings",
            "min_price": None, "max_price": None, "towns": [], "school_districts": [],
            "sort": "newest", "prioritize_my_listings": True,
        }
    return draft
