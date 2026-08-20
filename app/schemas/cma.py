from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict, Any
from datetime import datetime
import uuid

CmaMode = Literal['listing', 'buyer']
CmaStatus = Literal['DRAFT', 'READY', 'SHARED']
CompSource = Literal['mls', 'manual']
CompStatus = Literal['sold', 'pending', 'active_under_contract', 'active']
CompStrength = Literal['primary', 'strong', 'supporting', 'weak']
QualityRating = Literal['significantly_below', 'below', 'similar', 'above', 'significantly_above']
SimpleRating = Literal['below', 'similar', 'above']
LocationRating = Literal['inferior', 'similar', 'superior']


class CmaSubjectProperty(BaseModel):
    listing_key: Optional[str] = None
    source: CompSource = 'mls'
    photo_url: Optional[str] = None
    street_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    list_price: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[float] = None
    lot_size: Optional[float] = None
    year_built: Optional[int] = None
    property_type: Optional[str] = None
    garage: Optional[str] = None
    basement: Optional[str] = None
    days_on_market: Optional[int] = None
    mls_status: Optional[str] = None
    last_sale_date: Optional[str] = None
    last_sale_price: Optional[float] = None
    school_district: Optional[str] = None
    annual_taxes: Optional[float] = None
    # The agent's framing of the subject itself, shown before any comps in the client-facing
    # presentation - distinct from the pricing conclusion, which comes at the very end.
    agent_notes: Optional[str] = None


class CmaQualityAssessment(BaseModel):
    finish_quality: Optional[QualityRating] = None
    interior: Optional[SimpleRating] = None
    exterior: Optional[SimpleRating] = None
    lot: Optional[SimpleRating] = None
    amenities: Optional[SimpleRating] = None
    layout: Optional[SimpleRating] = None
    location: Optional[LocationRating] = None


class CmaComparable(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    listing_key: Optional[str] = None
    source: CompSource = 'mls'
    is_included: bool = True
    order_index: int = 0
    status: CompStatus = 'sold'

    photo_url: Optional[str] = None
    photos: List[str] = []
    street_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    distance_miles: Optional[float] = None

    sale_price: Optional[float] = None
    list_price: Optional[float] = None
    sale_date: Optional[str] = None
    days_on_market: Optional[int] = None

    sqft: Optional[float] = None
    lot_size: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    year_built: Optional[int] = None
    garage: Optional[str] = None
    basement: Optional[str] = None
    price_per_sqft: Optional[float] = None
    annual_taxes: Optional[float] = None
    # Computed client-side by the same Similar Properties matching formula (not recomputed
    # server-side) - persisted here purely for display in the analysis view and presentation.
    match_score: Optional[int] = None

    quality: CmaQualityAssessment = Field(default_factory=CmaQualityAssessment)
    strength: Optional[CompStrength] = None
    commentary: Optional[str] = None
    # AI-drafted note for this comp (see cma_ai_service.py) - deliberately separate from
    # `commentary` above, never auto-written into it. The agent's own analysis is never silently
    # overwritten; the frontend offers a one-click "Use this" that copies this into `commentary`,
    # still editable after.
    ai_commentary: Optional[str] = None


class CmaPricing(BaseModel):
    # Listing mode
    comparable_suggested_min: Optional[float] = None
    comparable_suggested_max: Optional[float] = None
    recommended_min: Optional[float] = None
    recommended_max: Optional[float] = None
    most_likely_value: Optional[float] = None
    recommended_list_price: Optional[float] = None
    pricing_commentary: Optional[str] = None
    # Buyer mode
    estimated_fair_value: Optional[float] = None
    suggested_offer_min: Optional[float] = None
    suggested_offer_max: Optional[float] = None
    strong_offer: Optional[float] = None
    max_recommended_price: Optional[float] = None
    offer_strategy_commentary: Optional[str] = None
    # AI-proposed value + range for the subject (see cma_ai_service.py) - same non-destructive
    # pattern as CmaComparable.ai_commentary: the agent applies it into the real pricing fields
    # above via an explicit "Apply" action, it's never written there automatically.
    ai_estimate: Optional[Dict[str, Any]] = None


class CmaMarketContext(BaseModel):
    recent_sales_count: Optional[int] = None
    median_sale_price: Optional[float] = None
    median_price_per_sqft: Optional[float] = None
    highest_recent_sale: Optional[float] = None
    highest_price_per_sqft_sale: Optional[float] = None
    avg_days_on_market: Optional[float] = None
    highlight_statements: List[str] = []
    hidden: bool = False


class CmaImprovement(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: Optional[str] = None
    description: Optional[str] = None
    approx_size: Optional[str] = None
    estimated_value_context: Optional[str] = None
    agent_notes: Optional[str] = None


class CmaPresentationSettings(BaseModel):
    hidden_sections: List[str] = []
    hidden_comp_ids: List[str] = []
    cover_photo_url: Optional[str] = None


class CmaAiAnalysisMeta(BaseModel):
    """Bookkeeping for the last AI generation on this report - lets a re-open of the page tell
    "nothing changed since last time" (input_fingerprint) so it never silently re-bills, and gives
    the agent visibility into what was actually sent/spent (see cma_ai_service.py)."""
    generated_at: Optional[str] = None
    model: Optional[str] = None
    cost_usd: Optional[float] = None
    input_fingerprint: Optional[str] = None
    images_sent: Optional[Dict[str, List[str]]] = None
    warnings: List[str] = []


class CmaReportResponse(BaseModel):
    id: str
    client_name: Optional[str] = None
    mode: CmaMode = 'listing'
    status: CmaStatus = 'DRAFT'
    subject: CmaSubjectProperty = Field(default_factory=CmaSubjectProperty)
    comparables: List[CmaComparable] = []
    pricing: CmaPricing = Field(default_factory=CmaPricing)
    market_context: CmaMarketContext = Field(default_factory=CmaMarketContext)
    improvements: List[CmaImprovement] = []
    presentation: CmaPresentationSettings = Field(default_factory=CmaPresentationSettings)
    ai_analysis: Optional[CmaAiAnalysisMeta] = None
    share_slug: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CmaReportListItem(BaseModel):
    id: str
    client_name: Optional[str] = None
    mode: CmaMode
    status: CmaStatus
    subject_address: Optional[str] = None
    subject_photo_url: Optional[str] = None
    comparable_count: int = 0
    value_range_min: Optional[float] = None
    value_range_max: Optional[float] = None
    updated_at: Optional[datetime] = None
    created_at: datetime


class CmaReportCreate(BaseModel):
    client_name: Optional[str] = None
    mode: CmaMode = 'listing'
    subject: Optional[CmaSubjectProperty] = None


class CmaReportUpdate(BaseModel):
    client_name: Optional[str] = None
    mode: Optional[CmaMode] = None
    status: Optional[CmaStatus] = None
    subject: Optional[CmaSubjectProperty] = None
    comparables: Optional[List[CmaComparable]] = None
    pricing: Optional[CmaPricing] = None
    market_context: Optional[CmaMarketContext] = None
    improvements: Optional[List[CmaImprovement]] = None
    presentation: Optional[CmaPresentationSettings] = None


class CmaGenerateAiAnalysisRequest(BaseModel):
    max_photos_per_property: Optional[int] = None
    include_map_images: bool = False
    force_regenerate: bool = False


class CmaCompSearchRequest(BaseModel):
    listing_key: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_miles: float = 1.0
    statuses: List[CompStatus] = ['sold']
    sale_date_from: Optional[str] = None
    sale_date_to: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_sqft: Optional[float] = None
    max_sqft: Optional[float] = None
    min_lot_size: Optional[float] = None
    max_lot_size: Optional[float] = None
    min_beds: Optional[float] = None
    min_baths: Optional[float] = None
    min_year_built: Optional[int] = None
    max_year_built: Optional[int] = None
    exclude_listing_keys: List[str] = []


class CmaCompCandidate(BaseModel):
    listing_key: str
    source: Literal['mls'] = 'mls'
    status: CompStatus
    photo_url: Optional[str] = None
    photos: List[str] = []
    street_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    distance_miles: Optional[float] = None
    sale_price: Optional[float] = None
    list_price: Optional[float] = None
    sale_date: Optional[str] = None
    days_on_market: Optional[int] = None
    sqft: Optional[float] = None
    lot_size: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    year_built: Optional[int] = None
    garage: Optional[str] = None
    basement: Optional[str] = None
    price_per_sqft: Optional[float] = None
    school_district: Optional[str] = None
    public_remarks: Optional[str] = None
    annual_taxes: Optional[float] = None


class CmaShareResponse(BaseModel):
    share_slug: str
    status: CmaStatus
    public_path: str


class CmaPublicAgent(BaseModel):
    name: str
    headshot_url: Optional[str] = None
    brokerage: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    agent_hub_slug: Optional[str] = None


class CmaPublicResponse(BaseModel):
    client_name: Optional[str] = None
    mode: CmaMode
    subject: CmaSubjectProperty
    comparables: List[CmaComparable]
    pricing: CmaPricing
    market_context: CmaMarketContext
    improvements: List[CmaImprovement]
    presentation: CmaPresentationSettings
    agent: CmaPublicAgent
    updated_at: Optional[datetime] = None
