from pydantic import BaseModel
from typing import Optional, List, Literal, Any, Dict
from datetime import datetime

from app.schemas.forms import LinkedProperty

BlogStatus = Literal['DRAFT', 'SCHEDULED', 'PUBLISHED', 'UNPUBLISHED', 'ARCHIVED']

BlogBlockType = Literal[
    'heading', 'subheading', 'paragraph', 'quote', 'bulleted_list', 'numbered_list',
    'divider', 'callout', 'button', 'image', 'gallery', 'video', 'youtube', 'map',
    'property_card', 'agent_profile', 'contact_form', 'qr_code', 'cta', 'related_articles', 'disclaimer',
    'lead_form', 'area_spotlight', 'property_showcase', 'video_showcase',
]


class BlogBlock(BaseModel):
    id: str
    type: BlogBlockType
    # Generic payload - shape depends on `type`. Kept loose (like AgentForm.questions) so the
    # editor can evolve block shapes without a migration.
    data: Dict[str, Any] = {}


class BlogResponse(BaseModel):
    id: str
    slug: str
    status: BlogStatus
    title: str
    subtitle: Optional[str] = None
    cover_image_url: Optional[str] = None
    category: Optional[str] = None
    theme: Optional[str] = None
    tags: List[str] = []
    blocks: List[BlogBlock] = []
    linked_properties: List[LinkedProperty] = []
    seo_title: Optional[str] = None
    meta_description: Optional[str] = None
    focus_keyword: Optional[str] = None
    social_title: Optional[str] = None
    social_description: Optional[str] = None
    social_image_url: Optional[str] = None
    is_indexed: bool = True
    agent_hub_visible: bool = True
    agent_hub_featured: bool = False
    idx_visible: bool = True
    use_signature_card: bool = True
    linked_areas: List[str] = []
    luxe_metal: Optional[str] = None
    ai_review: Optional[dict] = None
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


BlogTheme = Literal['luxe', 'editorial', 'modern', 'midnight', 'luxe_plus']


class BlogCreate(BaseModel):
    title: str
    category: Optional[str] = None
    starter_blocks: Optional[List[BlogBlock]] = None
    theme: Optional[BlogTheme] = None


class BlogUpdate(BaseModel):
    title: Optional[str] = None
    subtitle: Optional[str] = None
    cover_image_url: Optional[str] = None
    category: Optional[str] = None
    theme: Optional[BlogTheme] = None
    tags: Optional[List[str]] = None
    blocks: Optional[List[BlogBlock]] = None
    linked_properties: Optional[List[LinkedProperty]] = None
    seo_title: Optional[str] = None
    meta_description: Optional[str] = None
    focus_keyword: Optional[str] = None
    social_title: Optional[str] = None
    social_description: Optional[str] = None
    social_image_url: Optional[str] = None
    is_indexed: Optional[bool] = None
    luxe_metal: Optional[Literal['gold', 'silver', 'bronze']] = None


class BlogStatusUpdate(BaseModel):
    status: BlogStatus
    scheduled_at: Optional[datetime] = None


class BlogHubVisibilityUpdate(BaseModel):
    agent_hub_visible: Optional[bool] = None
    agent_hub_featured: Optional[bool] = None
    idx_visible: Optional[bool] = None
    use_signature_card: Optional[bool] = None
    # IDX neighborhood pages to link (area slugs from GET /studio/blogs/linkable-areas).
    linked_areas: Optional[List[str]] = None


class BlogSlugCheckResponse(BaseModel):
    slug: str
    available: bool


class BlogSlugUpdate(BaseModel):
    slug: str


class BlogListItem(BaseModel):
    id: str
    slug: str
    status: BlogStatus
    title: str
    subtitle: Optional[str] = None
    cover_image_url: Optional[str] = None
    # Cover if there is one, else the first photo in the article (image/gallery block, then a
    # property card) - so a list card shows a real picture instead of an empty box.
    preview_image_url: Optional[str] = None
    category: Optional[str] = None
    theme: Optional[str] = None
    agent_hub_visible: bool
    agent_hub_featured: bool
    idx_visible: bool = True
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_at: datetime
    view_count: int = 0
    # Unfinished AI article: 'interview' (answers saved, never written), 'running', 'failed'.
    ai_state: Optional[str] = None

    class Config:
        from_attributes = True


# ---- Public (visitor-facing) ----

class PublicBlogSummary(BaseModel):
    slug: str
    title: str
    subtitle: Optional[str] = None
    coverImageUrl: Optional[str] = None
    category: Optional[str] = None
    publishedAt: Optional[datetime] = None
    readingTimeMinutes: int = 1
    viewCount: int = 0
    # Lets the Agent Hub home page's "My Blogs" shelf show only (and feature-sort) the same
    # blogs the agent has opted into via Studio's "Show on Agent Hub" / "Featured" toggles,
    # without affecting this full listing page, which shows every published article regardless.
    agentHubVisible: bool = True
    agentHubFeatured: bool = False


class PublicBlogDetail(BaseModel):
    slug: str
    title: str
    subtitle: Optional[str] = None
    coverImageUrl: Optional[str] = None
    category: Optional[str] = None
    tags: List[str] = []
    blocks: List[BlogBlock] = []
    linkedProperties: List[LinkedProperty] = []
    seoTitle: Optional[str] = None
    metaDescription: Optional[str] = None
    socialTitle: Optional[str] = None
    socialDescription: Optional[str] = None
    socialImageUrl: Optional[str] = None
    isIndexed: bool = True
    publishedAt: Optional[datetime] = None
    readingTimeMinutes: int = 1
    agentName: str
    agentBrokerage: str
    agentPhotoUrl: Optional[str] = None
    agentId: Optional[str] = None
    agentTitle: Optional[str] = None
    agentMarket: Optional[str] = None
    theme: Optional[str] = None
    previewImageUrl: Optional[str] = None
    # True = end the article with the agent's Email Signature card instead of the small contact card.
    signatureCard: bool = False
    # Towns the article is about (from its AI research) - the signature card shows listings there first.
    areaTowns: List[str] = []
    # Linked IDX neighborhood pages: {slug, name, url, photo, median_price, active_count}.
    neighborhoodLinks: List[dict] = []
    luxeMetal: Optional[str] = None
    # The agent's live forms ({formType, slug, title}) - the closing section's buttons open them in a
    # window over the article.
    forms: List[dict] = []
    # A few of the agent's other published articles, for the Related Articles block / end of page.
    related: List['PublicBlogSummary'] = []


class BlogLeadCreate(BaseModel):
    """A reader's question from an article's Contact Form block."""
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    message: Optional[str] = None


class BlogEventCreate(BaseModel):
    event_type: Literal['view', 'link_click', 'property_click', 'contact_click', 'video_play', 'qr_scan', 'lead_submit']
    meta: Optional[Dict[str, Any]] = None


class BlogAnalyticsResponse(BaseModel):
    views: int = 0
    link_clicks: int = 0
    property_clicks: int = 0
    contact_clicks: int = 0
    video_plays: int = 0
    qr_scans: int = 0
    lead_submissions: int = 0


# ---- AI Blog Writer (see services/ai/ai_blog_writer_service.py) ----

class BlogAiVideo(BaseModel):
    url: str
    title: Optional[str] = None


class BlogAiBrief(BaseModel):
    topic: str
    goal: Optional[str] = None
    audiences: List[str] = []
    areas: List[str] = []
    key_points: Optional[str] = None
    personal_take: Optional[str] = None
    facts: Optional[str] = None
    include_recent_sales: bool = False
    sale_ids: List[str] = []
    area_slugs: List[str] = []  # towns to add a Town Spotlight section for (Areas slugs)
    listings: List[LinkedProperty] = []
    # One of the agent's videos (YouTube) to tie in: the writer gives it a section of its own and
    # the video is embedded right there.
    video: Optional[BlogAiVideo] = None
    tone: Literal['warm', 'professional', 'luxury', 'energetic', 'straight'] = 'warm'
    length: Literal['short', 'standard', 'long'] = 'standard'
    cta: Optional[str] = 'how_can_i_help'
    category_hint: Optional[str] = None
    # 'max' / 'lean' = the Research -> Write -> Fact-check pipeline (ai_blog_pipeline_service);
    # omitted = the original single-call writer.
    engine: Optional[Literal['max', 'lean']] = None
    # Set to rewrite an existing article in place ("Draft with AI" inside the editor); omitted
    # creates a brand-new DRAFT article.
    blog_id: Optional[str] = None


class BlogAiFact(BaseModel):
    fact: str
    source: str


class BlogAiGenerateResponse(BaseModel):
    blog: BlogResponse
    facts_used: List[BlogAiFact] = []
    numbers_to_verify: List[str] = []
    cover_image_idea: Optional[str] = None
    verification: Optional[dict] = None


class BlogAiRefineRequest(BaseModel):
    title: str = ''
    section_blocks: List[BlogBlock]
    article_blocks: List[BlogBlock] = []
    action: Literal['rewrite', 'expand', 'shorten', 'more_local', 'more_personal', 'simplify', 'custom'] = 'rewrite'
    instruction: Optional[str] = None


class BlogAiRefineResponse(BaseModel):
    blocks: List[BlogBlock]
    numbers_to_verify: List[str] = []
