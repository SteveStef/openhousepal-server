from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class PropertyWebsiteCreate(BaseModel):
    listing_key: Optional[str] = None
    manual_property: Optional[Dict[str, Any]] = None
    template_id: str = "cinematic_luxury"


class PropertyWebsiteDraftUpdate(BaseModel):
    """Partial patch merged into draft_data - only the keys present are touched, same
    exclude_unset convention as SiteDraftUpdate."""
    hero_image: Optional[str] = None
    # Switch between the two Spotlight templates - applied to the live page on publish (see
    # property_website_service.publish), like every other draft edit.
    template_id: Optional[str] = None
    photo_order: Optional[List[int]] = None
    description_override: Optional[str] = None
    featured_amenities: Optional[List[str]] = None
    video_asset_id: Optional[str] = None
    virtual_tour_url: Optional[str] = None
    floor_plans: Optional[List[Dict[str, Any]]] = None
    documents: Optional[List[Dict[str, Any]]] = None
    agents: Optional[List[Dict[str, Any]]] = None
    open_houses: Optional[List[Dict[str, Any]]] = None
    colors: Optional[Dict[str, Any]] = None
    section_visibility: Optional[Dict[str, bool]] = None
    # Short chip-style headline facts with no structured field of their own (e.g. "44-Car
    # Automotive Estate", "45,000-Gallon Pool") - agent-entered, shown alongside the real
    # structured stats in the Row 1 marquee. Max ~3, enforced in property_website_routes.py.
    notable_facts: Optional[List[str]] = None
    # Short marketing phrases (max ~8) the agent authors or approves from suggestions extracted
    # from the listing's own description - feeds the Row 2 marquee AND the Features & Amenities
    # highlight cards, never a raw MLS scrape.
    highlights: Optional[List[str]] = None
    # Agent-controlled room/section photo mapping for the chapter storytelling layout - an
    # ordered list of {id, type, label, enabled, photos: [url,...], copy}. See
    # property_website_service.CHAPTER_TYPES for the standard type ids.
    chapters: Optional[List[Dict[str, Any]]] = None
    # The Story section's text source: 'short' (the AI-polished version kept in story_short) or
    # 'original' (the MLS/listing description). description_override always holds what's shown.
    story_mode: Optional[str] = None
    story_short: Optional[str] = None
    # "About the town" section: {name, body, hidden} - AI-drafted once, then the agent's to edit.
    town_story: Optional[Dict[str, Any]] = None
    # Set once the editor's first-open automatic setup (featured amenities, highlights, story,
    # town) has run, so deleting something later never makes it come back on its own.
    auto_setup_done: Optional[bool] = None
    # The page header: {mode: photo|cycle|video, motion: in|out|both|none, video: {asset_id,
    # style, photo, url, poster}} - see property_website_service._clean_header.
    header: Optional[Dict[str, Any]] = None
    # The photo beside "The residence" (story) section - the agent's pick; unset = the 3rd photo.
    story_photo: Optional[str] = None


class PropertyWebsiteOut(BaseModel):
    id: str
    owner_id: str
    listing_key: Optional[str] = None
    template_id: str
    slug: str
    status: str
    property_snapshot: Dict[str, Any]
    draft_data: Dict[str, Any]
    published_snapshot: Dict[str, Any]
    published_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    public_url: Optional[str] = None

    class Config:
        from_attributes = True


class PropertyWebsiteViewStats(BaseModel):
    unique_visitors: int = 0
    total_views: int = 0
    # Totals only: distinct people who asked for a tour / sent a question from a Spotlight page.
    leads: int | None = None


class PropertyWebsiteAnalyticsOut(BaseModel):
    """Visitor stats for the Listing Spotlight Pages list - the agent's own views are never
    counted (the public beacon skips any page opened from Studio). `sites` is keyed by site id;
    a site with no visits yet is simply absent."""
    totals: PropertyWebsiteViewStats
    sites: Dict[str, PropertyWebsiteViewStats]


class PropertyWebsiteInquiryCreate(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=3, max_length=200)
    phone: Optional[str] = Field(None, max_length=40)
    message: Optional[str] = Field(None, max_length=2000)
    # Where the visitor came from ({src, oh, ref} - see services/visit_source.py).
    came_from: Optional[dict] = None


class PropertyWebsiteAlertCreate(BaseModel):
    """"Tell me about price changes and open houses" - just a name and an email."""
    name: str = Field("", max_length=120)
    email: str = Field(..., min_length=3, max_length=200)
    came_from: Optional[dict] = None


class PropertyWebsiteTourRequestCreate(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=3, max_length=200)
    phone: Optional[str] = Field(None, max_length=40)
    tour_type: Literal["in_person", "virtual"] = "in_person"
    requested_date: Optional[str] = Field(None, max_length=40)
    preferred_time: Optional[str] = Field(None, max_length=40)
    notes: Optional[str] = Field(None, max_length=2000)
    came_from: Optional[dict] = None
