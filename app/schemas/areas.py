from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

EnrichmentStatus = str  # 'PENDING' | 'RESEARCHING' | 'ENRICHED' | 'PARTIAL' | 'FAILED' - open string, matches Site.status convention


class MarketAreaResponse(BaseModel):
    id: str
    name: str
    state: str
    slug: str
    county: Optional[str] = None
    municipality_type: Optional[str] = None
    area_type: str = "city"  # 'city' | 'neighborhood'
    parent_area_name: Optional[str] = None  # e.g. "Philadelphia" for a 'neighborhood' area
    zip_codes: List[str] = []
    geography: Dict[str, Any] = {}
    facts: Dict[str, Any] = {}
    narrative: Optional[Dict[str, Any]] = None
    enrichment_status: EnrichmentStatus
    last_enriched_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    research_version: Optional[int] = None

    class Config:
        from_attributes = True


class AgentMarketAreaCreate(BaseModel):
    name: str
    state: str
    # 'city' (default) - the existing city/town autocomplete flow, matched/created exactly as
    # before. 'neighborhood' - the "Add Market Area / Neighborhood" flow: `name`/`state` are the
    # agent's free-text input, resolved server-side (see geocoding.resolve_named_place) to a
    # canonical name/parent city/centroid before the MarketArea is looked up or created.
    area_type: str = "city"


class AgentMarketAreaResponse(BaseModel):
    id: str
    area: MarketAreaResponse
    created_at: datetime

    class Config:
        from_attributes = True
