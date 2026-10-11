from pydantic import BaseModel
from typing import Any, Optional
from datetime import datetime

class CollectionPreferencesBase(BaseModel):
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None
    min_baths: Optional[float] = None
    max_baths: Optional[float] = None
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_year_built: Optional[int] = None
    max_year_built: Optional[int] = None
    lat: Optional[float] = None
    long: Optional[float] = None
    address: Optional[str] = None

    cities: list[str] = []
    townships: list[str] = []
    school_districts: list[str] = []
    county: Optional[str] = None
    counties: list[str] = []
    state: Optional[str] = None

    diameter: Optional[float] = None
    # Agent-drawn "Draw on Map" search area - GeoJSON Polygon/MultiPolygon, WGS84 lng/lat, same
    # shape google.maps.Polygon.getPath() yields. Mutually exclusive with address/diameter and
    # cities/townships/school_districts. Filtered via areas/geography.py's ray-casting SQL.
    search_polygon: Optional[dict[str, Any]] = None
    min_acres: Optional[float] = None
    max_acres: Optional[float] = None
    min_sqft: Optional[float] = None
    max_sqft: Optional[float] = None
    special_features: str = ""

    # Home type preferences
    is_town_house: Optional[bool] = False
    is_lot_land: Optional[bool] = False
    is_condo: Optional[bool] = False
    is_multi_family: Optional[bool] = False
    is_single_family: Optional[bool] = False
    is_apartment: Optional[bool] = False
    is_commercial: Optional[bool] = False
    is_farm: Optional[bool] = False

    # Visitor form data
    visiting_reason: Optional[str] = None
    has_agent: Optional[str] = None

    # Match-score comparison ("X% Match" badge) - see the model column's own comment.
    compare_property_listing_key: Optional[str] = None
    show_match_score: bool = True

class CollectionPreferencesCreate(CollectionPreferencesBase):
    collection_id: str

class CollectionPreferencesUpdate(BaseModel):
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None
    min_baths: Optional[float] = None
    max_baths: Optional[float] = None
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_year_built: Optional[int] = None
    max_year_built: Optional[int] = None
    lat: Optional[float] = None
    long: Optional[float] = None
    address: Optional[str] = None

    cities: Optional[list[str]] = None
    townships: Optional[list[str]] = None
    school_districts: Optional[list[str]] = None
    county: Optional[str] = None
    counties: Optional[list[str]] = None
    state: Optional[str] = None

    diameter: Optional[float] = None
    search_polygon: Optional[dict[str, Any]] = None
    min_acres: Optional[float] = None
    max_acres: Optional[float] = None
    min_sqft: Optional[float] = None
    max_sqft: Optional[float] = None
    special_features: Optional[str] = None

    is_town_house: Optional[bool] = None
    is_lot_land: Optional[bool] = None
    is_condo: Optional[bool] = None
    is_multi_family: Optional[bool] = None
    is_single_family: Optional[bool] = None
    is_apartment: Optional[bool] = None
    is_commercial: Optional[bool] = None
    is_farm: Optional[bool] = None

    # Visitor form data
    visiting_reason: Optional[str] = None
    has_agent: Optional[str] = None

    compare_property_listing_key: Optional[str] = None
    show_match_score: Optional[bool] = None

class ShowcaseSelfServePreferencesUpdate(BaseModel):
    """Narrow, visitor-facing subset of CollectionPreferencesUpdate - the public
    showcase page lets a visitor tune their own location/price/beds/baths and towns/
    school districts, but not the remaining agent-only fields (property type flags,
    townships, sqft/acres, special features)."""
    address: Optional[str] = None
    lat: Optional[float] = None
    long: Optional[float] = None
    diameter: Optional[float] = None
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_beds: Optional[int] = None
    max_beds: Optional[int] = None
    min_baths: Optional[float] = None
    max_baths: Optional[float] = None
    cities: Optional[list[str]] = None
    school_districts: Optional[list[str]] = None
    # Still accepted so older pages don't 422, but the visitor save route drops it (and
    # show_match_score): only the agent changes which home matches are scored against.
    compare_property_listing_key: Optional[str] = None
    # A visitor clearing the compare-property picker when there was no explicit override to revert
    # to (the showcase was only ever comparing to its open-house default) turns comparison off
    # entirely, rather than being a no-op that silently keeps showing the same default.
    show_match_score: Optional[bool] = None

class CollectionPreferences(CollectionPreferencesBase):
    id: str
    collection_id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True
