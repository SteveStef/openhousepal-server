from pydantic import BaseModel, Field
from typing import Optional, List, Literal, Dict, Any, Union
from datetime import datetime
import uuid

CmaMode = Literal['listing', 'buyer']
CmaStatus = Literal['DRAFT', 'READY', 'SHARED']
CompSource = Literal['mls', 'manual']
CompStatus = Literal['sold', 'pending', 'active_under_contract', 'active']
# Valuation classification. Only three are canonical / ever shown to a client:
#   strong      - a close, reliable local sale the value leans on
#   supporting  - a reasonable comp, useful but with real differences (moderate weight)
#   weak        - a genuine sale but a poor match / different product or market (little/no weight)
# 'primary' / 'context' / 'excluded' are legacy values from an earlier 5-tier scheme still present
# in older saved reports - normalised on read (primary->strong, context/excluded->weak). Never
# emit them for new reports.
CompStrength = Literal['strong', 'supporting', 'weak', 'primary', 'context', 'excluded']
SimpleRating = Literal['below', 'similar', 'above']
AreaRating = Literal['different_from_subject', 'similar_to_subject', 'same_as_subject']


class CmaSubjectProperty(BaseModel):
    listing_key: Optional[str] = None
    source: CompSource = 'mls'
    photo_url: Optional[str] = None
    photos: List[str] = []
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
    above_grade_finished_area: Optional[float] = None
    below_grade_finished_area: Optional[float] = None
    lot_size: Optional[float] = None
    year_built: Optional[int] = None
    property_type: Optional[str] = None
    architectural_style: Optional[Union[List[str], str]] = None
    levels: Optional[Union[List[str], str]] = None
    township: Optional[str] = None
    subdivision_name: Optional[str] = None
    garage: Optional[str] = None
    basement: Optional[str] = None
    days_on_market: Optional[int] = None
    mls_status: Optional[str] = None
    last_sale_date: Optional[str] = None
    last_sale_price: Optional[float] = None
    school_district: Optional[str] = None
    annual_taxes: Optional[float] = None
    water_source: Optional[Union[List[str], str]] = None
    sewer: Optional[Union[List[str], str]] = None
    # MLS listing description (PublicRemarks) - fed to the AI analysis alongside the structured
    # fields and photos (see cma_ai_service.py _SUBJECT_FIELDS).
    public_remarks: Optional[str] = None
    property_condition: Optional[Any] = None   # Bright PropertyCondition, e.g. ["Excellent"] / ["Major Rehab Needed"]
    # The agent's framing of the subject itself, shown before any comps in the client-facing
    # presentation - distinct from the pricing conclusion, which comes at the very end.
    agent_notes: Optional[str] = None
    # Typed in by the agent on the manual-entry path (a home that isn't on the MLS yet) - an MLS
    # subject usually leaves these empty. `baths` stays the total the engine reads (full + 0.5 per
    # half, the same way Bright's BathroomsTotal counts them); garage_spaces and stories are read
    # by the engine ahead of the garage text / style (_v10_garage_spaces, _relevance_level_class).
    full_baths: Optional[int] = None
    half_baths: Optional[int] = None
    garage_spaces: Optional[float] = None
    stories: Optional[float] = None
    county: Optional[str] = None
    heating: Optional[str] = None
    cooling: Optional[str] = None
    condition: Optional[str] = None
    recent_updates: Optional[str] = None
    # Where photo_url came from on a manual subject: 'upload' | 'street_view' | 'aerial'.
    photo_source: Optional[str] = None


class CmaQualityAssessment(BaseModel):
    finish_quality: Optional[SimpleRating] = None
    interior: Optional[SimpleRating] = None
    exterior: Optional[SimpleRating] = None
    # 'n_a' = a condo / unit has no lot of its own (10/21) - without it every condo report failed to load
    lot: Optional[Literal['below', 'similar', 'above', 'n_a']] = None
    amenities: Optional[SimpleRating] = None
    layout: Optional[SimpleRating] = None
    area: Optional[AreaRating] = None
    # This comp's street/positional character vs. the subject (cul-de-sac/quiet street vs. a major
    # road/highway) - a DIRECTIONAL rating (below/similar/above), the same shape as the other
    # quality dimensions above and as the AI tool schema + _derive_street_context in
    # cma_ai_service.py: 'above' = comp's street setting is quieter/more desirable than the
    # subject's, 'below' = busier/less desirable. NOT AreaRating - that's the area-match field.
    street_context: Optional[SimpleRating] = None


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

    sale_price: Optional[float] = None       # ONLY a verified closed price (never a list/pending fallback)
    list_price: Optional[float] = None
    original_list_price: Optional[float] = None
    close_price: Optional[float] = None
    close_date: Optional[str] = None
    sale_date: Optional[str] = None
    # True only when the transaction is confirmed closed AND the closing price is known. When
    # False this comp must not feed any valuation math (see effectiveCompPrice / valuationComps).
    price_verified: bool = True
    days_on_market: Optional[int] = None
    cdom: Optional[int] = None

    sqft: Optional[float] = None
    above_grade_finished_area: Optional[float] = None
    below_grade_finished_area: Optional[float] = None
    lot_size: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    year_built: Optional[int] = None
    architectural_style: Optional[Union[List[str], str]] = None
    levels: Optional[Union[List[str], str]] = None
    material_issues: Optional[List[str]] = None  # deal-breakers on record (cesspool, "as-is", flood zone...)
    township: Optional[str] = None
    subdivision_name: Optional[str] = None
    garage: Optional[str] = None
    basement: Optional[str] = None
    price_per_sqft: Optional[float] = None
    annual_taxes: Optional[float] = None
    school_district: Optional[str] = None
    water_source: Optional[Union[List[str], str]] = None
    sewer: Optional[Union[List[str], str]] = None
    # MLS listing description (PublicRemarks) - fed to the AI analysis alongside the structured
    # fields and photos (see cma_ai_service.py _COMP_FIELDS).
    public_remarks: Optional[str] = None
    property_condition: Optional[Any] = None   # Bright PropertyCondition, e.g. ["Excellent"] / ["Major Rehab Needed"]
    property_type: Optional[str] = None
    home_type: Optional[str] = None
    # Deterministic continuous relevance percentage. CMA generation recomputes this from the
    # immutable authoritative snapshot; pre-generation clients mirror the same shared formula.
    match_score: Optional[int] = None
    similarity_details: Optional[Dict[str, Any]] = None
    # OpenStreetMap road/street classification for this comp's location (road_context_service.py) -
    # {verdict: quiet|neutral|through, road_types, max_speed_mph, nearby_road_names, summary}.
    # `road_context_note` is the same thing pre-formatted for the AI prompt + agent display.
    road_context: Optional[Dict[str, Any]] = None
    road_context_note: Optional[str] = None

    quality: CmaQualityAssessment = Field(default_factory=CmaQualityAssessment)
    strength: Optional[CompStrength] = None
    # Claude's proposed role is audit-only after generation. `strength` is the deterministic role
    # derived from continuous relevance and is the role shown to clients.
    ai_strength: Optional[CompStrength] = None
    # Derived after the anchor is locked; tells renderers whether the comp carried valuation weight.
    valuation_weight: Optional[float] = None
    valuation_role: Optional[Literal['valuation_comp', 'context_only']] = None
    # v10 Fix 2 - the AI's machine-checkable "keep this Strong despite a different market" override.
    # Validated deterministically against structured evidence in cma_ai_service._v10_location_class_review;
    # a thin/unsupported override is ignored and the comp is demoted to supporting.
    strong_location_override: Optional[bool] = None
    strong_location_override_reason: Optional[str] = None
    market_equivalence_evidence: Optional[List[str]] = None
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
    # Listing mode - the seller mirror of offer_suggestion_notes / inspection_concerns below.
    # AI-filled once (non-destructively) from ai_estimate.listing_strategy_considerations /
    # listing_prep_concerns; the agent's own edits always win.
    listing_strategy_notes: Optional[str] = None
    listing_prep_notes: Optional[str] = None
    # Buyer mode
    estimated_fair_value: Optional[float] = None
    suggested_offer_min: Optional[float] = None
    suggested_offer_max: Optional[float] = None
    strong_offer: Optional[float] = None
    max_recommended_price: Optional[float] = None
    # AI-auto-filled (once, non-destructively) with its valuation reasoning in buyer mode - see
    # cma_ai_service.py. Kept as "offer_strategy_commentary" for backward compatibility even
    # though the buyer-mode UI now labels it "Buyer Valuation - Agent Notes".
    offer_strategy_commentary: Optional[str] = None
    # Buyer mode - inspection/property-condition concerns separated from offer positioning so the
    # public CMA/PDF can present due diligence separately from negotiating strategy.
    inspection_concerns: Optional[str] = None
    # Agent-only, never touched by AI - how the agent would actually structure an offer on this
    # home (separate from the AI's fair-value reasoning above).
    offer_suggestion_notes: Optional[str] = None
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
    ai_generated: bool = False


class CmaPresentationSettings(BaseModel):
    hidden_sections: List[str] = []
    hidden_comp_ids: List[str] = []
    cover_photo_url: Optional[str] = None
    # Which theme the public share link opens in for a visitor by default - None means "dark"
    # (the site's own current default), since that's the common case and not worth forcing every
    # existing report to have this field set. The IDX-embedded view ignores this entirely and is
    # always light regardless, since a client shouldn't see a dark page inside the agent's own
    # branded site just because the agent picked dark as their default.
    default_theme: Optional[Literal["light", "dark"]] = None


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
    use_signature_card: bool = True
    generation_status: Optional[Literal["generating", "complete", "failed"]] = None
    generation_started_at: Optional[datetime] = None
    generation_error: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CmaReportListItem(BaseModel):
    id: str
    client_name: Optional[str] = None
    mode: CmaMode
    status: CmaStatus
    share_slug: Optional[str] = None
    subject_address: Optional[str] = None
    subject_photo_url: Optional[str] = None
    comparable_count: int = 0
    fair_value: Optional[float] = None
    value_range_min: Optional[float] = None
    value_range_max: Optional[float] = None
    default_theme: Optional[Literal["light", "dark"]] = None
    export_allowed: bool = True
    qa_blocking_count: int = 0
    generation_status: Optional[Literal["generating", "complete", "failed"]] = None
    generation_started_at: Optional[datetime] = None
    generation_error: Optional[str] = None
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
    use_signature_card: Optional[bool] = None


class CmaThemeUpdate(BaseModel):
    default_theme: Literal["light", "dark"]


class CmaGenerateAiAnalysisRequest(BaseModel):
    max_photos_per_property: Optional[int] = None
    include_map_images: bool = False
    force_regenerate: bool = False
    # True = start it on the server and answer right away ('generating'); the page polls. A run
    # takes minutes - a phone or a proxy dropping one long request showed "Network error" while
    # the report was in fact being finished.
    background: bool = False


class CmaCompSearchRequest(BaseModel):
    listing_key: Optional[str] = None
    subject: Optional[CmaSubjectProperty] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    subject_sqft: Optional[float] = None
    property_type: Optional[str] = None
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
    school_district: Optional[str] = None
    city: Optional[str] = None
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
    sale_price: Optional[float] = None       # ONLY a verified closed price
    list_price: Optional[float] = None
    original_list_price: Optional[float] = None
    close_price: Optional[float] = None
    close_date: Optional[str] = None
    sale_date: Optional[str] = None
    price_verified: bool = True
    days_on_market: Optional[int] = None
    cdom: Optional[int] = None
    sqft: Optional[float] = None
    above_grade_finished_area: Optional[float] = None
    below_grade_finished_area: Optional[float] = None
    lot_size: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    year_built: Optional[int] = None
    architectural_style: Optional[Union[List[str], str]] = None
    levels: Optional[Union[List[str], str]] = None
    township: Optional[str] = None
    subdivision_name: Optional[str] = None
    garage: Optional[str] = None
    basement: Optional[str] = None
    price_per_sqft: Optional[float] = None
    school_district: Optional[str] = None
    water_source: Optional[Union[List[str], str]] = None
    sewer: Optional[Union[List[str], str]] = None
    public_remarks: Optional[str] = None
    property_condition: Optional[Any] = None   # Bright PropertyCondition, e.g. ["Excellent"] / ["Major Rehab Needed"]
    annual_taxes: Optional[float] = None
    property_type: Optional[str] = None
    similarity_context: Optional[Dict[str, Any]] = None
    # Computed by the authoritative backend relevance engine for this exact subject snapshot.
    # CMA clients display this value verbatim and never recreate it in the browser.
    match_score: Optional[int] = None
    similarity_details: Optional[Dict[str, Any]] = None


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
    idx_site_url: Optional[str] = None


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
    # Set when the web view should end with the agent's Email Signature card (site version):
    # the report's switch is on, the plan has signatures and one is saved. The PDF ignores it.
    signature_hub_slug: Optional[str] = None
