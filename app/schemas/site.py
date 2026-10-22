from pydantic import BaseModel
from typing import Optional, List, Literal, Any, Dict
from datetime import datetime

SiteStatus = Literal['DRAFT', 'PUBLISHED']
SiteDomainStatus = Literal['PENDING', 'VERIFIED', 'ACTIVE', 'FAILED']

# Open string in the DB (see Site.draft_data), but constrained here so the editor and public
# renderer both reject anything unrecognized. New block types slot in later as new Literal
# members, no schema change (block.data stays a free JSON dict - see SiteBlock below).
#
# Phase 2 adds OHP-data-backed blocks (see backend/app/services/website/content_adapters.py).
# Each one reads block.data for:
#   source_mode: 'automatic' | 'selected' | 'hidden'  (default 'automatic')
#   limit: int                                        (default varies by type, see hydrate_site_content)
#   selected_ids: list[str]                            (listing_keys / blog ids / open house ids - only used when source_mode == 'selected')
# 'about_agent' additionally supports an `overrides` dict (bio/phone/email/photo_url) - see
# content_adapters.get_agent_profile for the source values an override replaces.
# 'form' uses `form_id` (no source_mode - it references exactly one AgentForm).
# 'showcase' uses `collection_id` (no source_mode - references exactly one public Collection).
# 'testimonials' (2026-09-08 reviews) was missing here while the starter template already adds one
# to the About page, so GET /studio/sites/me 500'd for every site that had it.
SiteBlockType = Literal[
    'hero', 'heading_text', 'rich_text', 'image', 'about_agent', 'contact_cta', 'divider',
    'featured_listings', 'recent_sales', 'open_houses', 'blog_posts', 'form', 'showcase',
    'communities', 'newly_listed', 'testimonials', 'spotlights',
]


class SiteBlock(BaseModel):
    id: str
    type: SiteBlockType
    hidden: bool = False
    data: Dict[str, Any] = {}


class SitePage(BaseModel):
    id: str
    slug: str = ''  # '' is the home page's slug
    title: str
    is_home: bool = False
    blocks: List[SiteBlock] = []


class SiteTheme(BaseModel):
    primary_color: str = '#111827'
    secondary_color: str = '#C9A24D'
    heading_font: str = 'serif'
    body_font: str = 'sans'
    logo_url: Optional[str] = None
    # Source + override pattern for branding (see content_adapters.get_brand_kit_theme): while
    # True, the public renderer resolves colors/fonts/logo live from StudioBrandKit on every
    # request instead of trusting these stored values, so a Brand Kit edit shows up on the
    # website automatically. Flipped to False the moment an agent edits theme fields directly in
    # the website editor - the stored values above become an intentional website-only override.
    follow_brand_kit: bool = True
    # Two looks built on the same sections: 'luxe' is the original cinematic charcoal-and-metal
    # design (every site published before this field existed stays exactly as it was), 'default'
    # is the cleaner, lighter look new sites start on. `mode` is what visitors see first; they
    # can flip light/dark themselves in the header unless `allow_mode_toggle` is off.
    template: Literal['default', 'luxe'] = 'luxe'
    mode: Literal['light', 'dark'] = 'light'
    allow_mode_toggle: bool = True
    # The agent's Email Signature card as the footer's agent block on every page (the one place
    # the signature lives on the website). Only shows when the plan has signatures and one is
    # saved - see content_adapters.signature_hub_slug_for.
    footer_signature: bool = False  # off unless the agent turns it on (user, 10/18)
    # Luxe's metal finish - the same gold / silver / bronze as the Email Signature and Agent Hub
    # (frontend lib/luxeMetals). Classic uses secondary_color (any non-metal accent) instead.
    luxe_metal: Optional[Literal['gold', 'silver', 'bronze']] = None
    # The site's loading mark (frontend SiteLoader): this logo if uploaded, else the agent's initials.
    loader_logo_url: Optional[str] = None


class FeaturedSettings(BaseModel):
    """Drives the public /properties ("Featured") page - a full nav tab, not a page-builder
    block, so it lives at the site level rather than inside any one page's blocks. 'my_listings'
    (default) is today's behavior: just this agent's own active listings, unfiltered. 'filtered'
    hands the page over to an agent-defined full-MLS search (real IDX behavior, same full-MLS
    exception as content_adapters.get_hero_photos/get_search_results) so an agent can showcase,
    e.g., only $1.5M-$4M listings across a few towns/school districts, sorted highest price first
    - see content_adapters.get_featured_page_listings. `prioritize_my_listings` only matters in
    'filtered' mode: when true (default), this agent's own matching listings are pulled to the
    front of the filtered results rather than sorting purely by the full-MLS pool."""
    # 'served_areas': every home for sale in the towns from the Areas tab, the agent's own first.
    mode: Literal['my_listings', 'served_areas', 'filtered'] = 'my_listings'
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    towns: List[str] = []
    school_districts: List[str] = []
    sort: Literal['newest', 'price_desc', 'price_asc'] = 'newest'
    prioritize_my_listings: bool = True
    # What the page is made of, as separate tick boxes (setup + website editor, 10/22). None = an
    # older save that only has `mode` - content_adapters.featured_sources derives them from it.
    # `prioritize_my_listings` is the order: True = "my listings first, then the rest", False =
    # "mix them together".
    show_mine: Optional[bool] = None          # the agent's own active listings
    show_area_homes: Optional[bool] = None    # homes for sale in their towns (Areas tab)
    show_custom: Optional[bool] = None        # a hand-picked search: towns / school districts
    area_towns: List[str] = []                # which of their towns (empty = all of them)


class MediaSettings(BaseModel):
    """Drives the public /blog ("Media") page's non-blog content. Blog posts always come from
    the real Blog table (see content_adapters.get_blog_posts) - not configurable here, there's
    nothing to sync. Videos/tours are read-only mirrors of this agent's own Agent Hub
    (AgentHub.config.videoLinks/houseTourLinks - the same real data their Agent Hub "Videos"/
    "House Tours" sections already show), never a second copy the agent maintains twice; these
    two flags just control whether the website's Media page also surfaces them."""
    show_videos: bool = True
    show_tours: bool = True


class SiteSeoPage(BaseModel):
    """One page's Google listing: its blue title and the grey line under it."""
    title: Optional[str] = None
    description: Optional[str] = None


class SiteSeo(BaseModel):
    """How the site shows up in Google and the browser tab (IDX editor -> Google tab). `pages` is
    keyed by page slug, plus the built-in sections: search, properties, neighborhoods, blog, about,
    contact. These become each page's <title>/<meta description> - what Google uses for the result
    and its sub-links ("sitelinks", which Google chooses to show on its own)."""
    site_name: str = ''
    home_title: str = ''
    description: str = ''
    favicon_url: Optional[str] = None
    share_image_url: Optional[str] = None
    allow_indexing: bool = True
    # Hand Google the star rating (only ever sent when it's honest and 4.0+ from 3+ reviews -
    # see content_adapters.google_rating). Off = never sent.
    share_rating: bool = True
    pages: Dict[str, SiteSeoPage] = {}


class SiteContent(BaseModel):
    """The shape shared by Site.draft_data and Site.published_snapshot - publishing is a copy
    of one into the other, not a transform. See site_service.publish_site."""
    pages: List[SitePage] = []
    theme: SiteTheme = SiteTheme()
    featured_settings: FeaturedSettings = FeaturedSettings()
    media_settings: MediaSettings = MediaSettings()
    seo: SiteSeo = SiteSeo()


class SiteResponse(BaseModel):
    id: str
    subdomain_slug: str
    status: SiteStatus
    is_enabled: bool = True
    draft_data: SiteContent
    published_snapshot: SiteContent
    published_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    # Agent Hub slug the editor's preview uses to show the real signature card in the footer -
    # None when the plan has no signatures / none saved (set by GET /studio/sites/me only).
    signature_hub_slug: Optional[str] = None

    class Config:
        from_attributes = True


class SiteDraftUpdate(BaseModel):
    """Full-blob replace for whichever of pages/theme/featured_settings/media_settings is
    provided - same pattern as DesignProjectUpdate.blocks."""
    pages: Optional[List[SitePage]] = None
    theme: Optional[SiteTheme] = None
    featured_settings: Optional[FeaturedSettings] = None
    media_settings: Optional[MediaSettings] = None
    seo: Optional[SiteSeo] = None


class SitePreviewRequest(BaseModel):
    """Unsaved draft blocks from the editor's in-memory state - hydrated the exact same way the
    public renderer hydrates a published page (see content_adapters.hydrate_page_blocks), so the
    editor's live preview shows real resolved data (rotating hero photos, agent profile, featured
    listings, etc.) instead of the raw, unresolved block data the draft actually stores.

    featured_settings is likewise the editor's current in-memory (possibly unsaved) Featured-tab
    state, not whatever's already on the draft/published site - the featured_listings block reads
    directly from these settings (see hydrate_page_blocks), so a preview built from stale/saved
    settings would show the OLD Featured criteria while the agent is actively editing the new
    ones on the Featured tab."""
    blocks: List[SiteBlock] = []
    featured_settings: Optional[FeaturedSettings] = None


class DnsInstruction(BaseModel):
    type: str  # 'CNAME' | 'FORWARD' (registrar-level apex forwarding, no DNS record to add)
    name: str  # e.g. 'www' or '@'
    value: str
    note: Optional[str] = None


class SiteDomainResponse(BaseModel):
    id: str
    site_id: str
    hostname: str
    status: SiteDomainStatus
    verification_token: Optional[str] = None
    is_primary: bool
    verified_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None
    redirect_target_domain_id: Optional[str] = None
    created_at: datetime
    instructions: List[DnsInstruction] = []  # populated by the route, not stored

    class Config:
        from_attributes = True


class SiteDomainCreate(BaseModel):
    hostname: str
