from types import SimpleNamespace
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from typing import Optional, List, Dict, Any, Union
from datetime import datetime

from app.utils.listing_dom import dom_fetched_at, resolve_days_on_market

class PropertySummaryResponse(BaseModel):
    """
    SHALLOW model for lists and showcases.
    Optimized for small payload sizes.
    Includes RESO aliases for frontend standard compatibility.
    """
    model_config = ConfigDict(
        alias_generator=to_camel, 
        populate_by_name=True, 
        from_attributes=True
    )
    
    id: str
    listing_key: str = Field(..., alias="ListingKey")
    listing_id: Optional[str] = Field(None, alias="ListingId") # MLS Number
    street_address: str = Field(..., alias="FullStreetAddress")
    unparsed_address: Optional[str] = Field(None, alias="UnparsedAddress")
    full_address: Optional[str] = Field(None, alias="FullAddress")
    city: str = Field(..., alias="City")
    state: str = Field(..., alias="StateOrProvince")
    zipcode: Optional[str] = Field(None, alias="PostalCode")
    
    price: Optional[float] = Field(None, alias="ListPrice")
    bedrooms: Optional[float] = Field(None, alias="BedroomsTotal")
    bathrooms: Optional[float] = Field(None, alias="BathroomsTotal")
    living_area: Optional[float] = Field(None, alias="LivingArea")
    lot_size: Optional[float] = Field(None, alias="LotSizeSquareFeet")
    lot_size_acres: Optional[float] = Field(None, alias="LotSizeAcres")
    home_type: Optional[str] = Field(None, alias="PropertyType")
    
    # Raw MLS data
    mls_property_type: Optional[str] = Field(None, alias="MlsPropertyType")
    mls_structure_design_type: Optional[str] = Field(None, alias="MlsStructureDesignType")
    price_per_square_feet: Optional[float] = Field(None, alias="PricePerSquareFoot")
    mls_incorporated_city_name: Optional[str] = Field(None, alias="IncorporatedCityName")
    
    home_status: str = Field(..., alias="MlsStatus")
    
    subdivision_name: Optional[str] = Field(None, alias="SubdivisionName")
    township: Optional[str] = Field(None, alias="MLSAreaMajor")
    school_district_name: Optional[str] = Field(None, alias="SchoolDistrictName")
    
    img_src: Optional[str] = Field(None, alias="ListPictureURL")
    latitude: Optional[float] = Field(None, alias="Latitude")
    longitude: Optional[float] = Field(None, alias="Longitude")
    google_rooftop_latitude: Optional[float] = None
    google_rooftop_longitude: Optional[float] = None
    google_geocode_location_type: Optional[str] = None
    
    days_on_market: Optional[int] = Field(None, alias="DaysOnMarket")
    year_built: Optional[int] = Field(None, alias="YearBuilt")
    mls_list_date: Optional[datetime] = Field(None, alias="MLSListDate")
    price_change_timestamp: Optional[datetime] = Field(None, alias="PriceChangeTimestamp")
    original_list_price: Optional[float] = Field(None, alias="OriginalListPrice")
    # when Bright's DaysOnMarket was read (see listing_dom.dom_fetched_at) - not sent to the browser;
    # row_updated_at is only the fallback for a row the sync hasn't re-read since dom_read_at existed
    dom_read_at: Optional[datetime] = Field(None, exclude=True, validation_alias="dom_read_at")
    row_updated_at: Optional[datetime] = Field(None, exclude=True, validation_alias="updated_at")
    row_geocoded_at: Optional[datetime] = Field(None, exclude=True, validation_alias="google_geocoded_at")
    row_amenities_at: Optional[datetime] = Field(None, exclude=True, validation_alias="nearby_amenities_cached_at")
    row_modified_at: Optional[datetime] = Field(None, exclude=True, validation_alias="modification_timestamp")
    row_created_at: Optional[datetime] = Field(None, exclude=True, validation_alias="created_at")
    # What a sold home actually sold for. `price` stays the list price; a sold page showed that as
    # "Sold for" (10/19: 345 Holt Ln "Sold for $520,000" - it sold for $601,000). None when Bright
    # never sent a closing price, so the page can say it's the last asking price instead.
    close_price: Optional[float] = Field(None, alias="ClosePrice")
    close_date: Optional[str] = Field(None, alias="CloseDate")
    mls_raw: Optional[dict] = Field(None, exclude=True, validation_alias="raw_mls_data")

    @model_validator(mode='after')
    def calculate_dom(self) -> 'PropertySummaryResponse':
        self.days_on_market = resolve_days_on_market(
            self.days_on_market, self.home_status, self.mls_list_date,
            fetched_at=dom_fetched_at(SimpleNamespace(
                dom_read_at=self.dom_read_at, updated_at=self.row_updated_at, google_geocoded_at=self.row_geocoded_at,
                nearby_amenities_cached_at=self.row_amenities_at, modification_timestamp=self.row_modified_at,
                created_at=self.row_created_at)),
        )
        raw = self.mls_raw or {}
        if self.close_price is None and raw.get("ClosePrice"):
            try:
                self.close_price = float(raw["ClosePrice"]) or None
            except (TypeError, ValueError):
                self.close_price = None
        if self.close_date is None and raw.get("CloseDate"):
            self.close_date = str(raw["CloseDate"])[:10]
        self.mls_raw = None
        return self

class PropertyDetailResponse(PropertySummaryResponse):
    """
    DEEP model for property details page/modal.
    Includes full description, all photos, and detailed features.
    """
    description: Optional[str] = Field(None, alias="PublicRemarks")
    photos: Optional[List[str]] = None
    
    # Listing Agent & Office
    list_agent_full_name: Optional[str] = Field(None, alias="ListAgentFullName")
    list_agent_email: Optional[str] = Field(None, alias="ListAgentEmail")
    list_agent_preferred_phone: Optional[str] = Field(None, alias="ListAgentPreferredPhone")
    list_office_name: Optional[str] = Field(None, alias="ListOfficeName")
    list_office_phone: Optional[str] = Field(None, alias="ListOfficePhone")

    # Structural & Exterior
    architectural_style: Optional[List[str]] = Field(None, alias="ArchitecturalStyle")
    construction_materials: Optional[List[str]] = Field(None, alias="ConstructionMaterials")
    roof_type: Optional[List[str]] = Field(None, alias="Roof")
    foundation_details: Optional[List[str]] = Field(None, alias="FoundationDetails")
    structure_type: Optional[List[str]] = Field(None, alias="StructureType")
    levels: Optional[List[str]] = Field(None, alias="Levels")
    
    # Interior & Features
    interior_features: Optional[List[str]] = Field(None, alias="InteriorFeatures")
    exterior_features: Optional[List[str]] = Field(None, alias="ExteriorFeatures")
    flooring: Optional[List[str]] = Field(None, alias="Flooring")
    appliances: Optional[List[str]] = Field(None, alias="Appliances")
    fireplaces: Optional[int] = Field(None, alias="FireplacesTotal")
    fireplace_features: Optional[List[str]] = Field(None, alias="FireplaceFeatures")
    door_features: Optional[List[str]] = Field(None, alias="DoorFeatures")
    window_features: Optional[List[str]] = Field(None, alias="WindowFeatures")
    
    # Utilities & Systems
    cooling: Optional[List[str]] = Field(None, alias="Cooling")
    heating: Optional[List[str]] = Field(None, alias="Heating")
    water_source: Optional[List[str]] = Field(None, alias="WaterSource")
    sewer: Optional[List[str]] = Field(None, alias="Sewer")
    utilities: Optional[List[str]] = Field(None, alias="Utilities")
    
    # Area Breakdown
    above_grade_finished_area: Optional[float] = Field(None, alias="AboveGradeFinishedArea")
    below_grade_finished_area: Optional[float] = Field(None, alias="BelowGradeFinishedArea")
    basement: Optional[List[str]] = Field(None, alias="Basement")
    accessibility_features: Optional[List[str]] = Field(None, alias="AccessibilityFeatures")

    # Booleans
    has_basement: Optional[bool] = Field(None, alias="BasementYN")
    has_central_air: Optional[bool] = Field(None, alias="CentralAirYN")
    has_fireplace: Optional[bool] = Field(None, alias="FireplaceYN")

    # Parking
    garage_spaces: Optional[float] = Field(None, alias="GarageSpaces")
    parking_features: Optional[List[str]] = Field(None, alias="ParkingFeatures")
    has_garage: Optional[bool] = Field(None, alias="GarageYN")
    
    # Community & HOA
    association_fee: Optional[float] = Field(None, alias="AssociationFee")
    association_fee_frequency: Optional[str] = Field(None, alias="AssociationFeeFrequency")
    association_fee_2: Optional[float] = Field(None, alias="AssociationFee2")
    association_fee_2_frequency: Optional[str] = Field(None, alias="AssociationFee2Frequency")
    association_amenities: Optional[List[str]] = Field(None, alias="AssociationAmenities")
    association_fee_includes: Optional[List[str]] = Field(None, alias="AssociationFeeIncludes")
    has_association: Optional[bool] = Field(None, alias="AssociationYN")
    
    # Lot & Location
    lot_features: Optional[List[str]] = Field(None, alias="LotFeatures")
    view: Optional[List[str]] = Field(None, alias="View")
    waterfront_features: Optional[List[str]] = Field(None, alias="WaterfrontFeatures")
    has_waterfront_view: Optional[bool] = Field(None, alias="WaterfrontViewYN")
    has_view: Optional[bool] = Field(None, alias="ViewYN")
    
    # Tax & Financial
    tax_annual_amount: Optional[float] = Field(None, alias="TaxAnnualAmount")
    tax_year: Optional[int] = Field(None, alias="TaxYear")
    listing_tax_id: Optional[str] = Field(None, alias="ListingTaxID")
    
    # Education
    elementary_school: Optional[str] = Field(None, alias="ElementarySchool")
    middle_or_junior_school: Optional[str] = Field(None, alias="MiddleOrJuniorSchool")
    high_school: Optional[str] = Field(None, alias="HighSchool")
    school_district_name: Optional[str] = Field(None, alias="SchoolDistrictName")
    
    # Neighborhood & Location
    county: Optional[str] = Field(None, alias="County")
    township: Optional[str] = Field(None, alias="MLSAreaMajor")
    directions: Optional[str] = Field(None, alias="Directions")
    zoning: Optional[str] = Field(None, alias="Zoning")
    
    # Financials (More detail)
    tax_assessment_amount: Optional[float] = Field(None, alias="TaxAssessmentAmount")
    assessment_year: Optional[int] = Field(None, alias="AssessmentYear")
    possession: Optional[List[str]] = Field(None, alias="Possession")
    
    # Detailed Features
    cooling_fuel: Optional[List[str]] = Field(None, alias="CoolingFuel")
    heating_fuel: Optional[List[str]] = Field(None, alias="HeatingFuel")
    attached_garage_yn: Optional[bool] = Field(None, alias="AttachedGarageYN")
    new_construction_yn: Optional[bool] = Field(None, alias="NewConstructionYN")
    senior_community_yn: Optional[bool] = Field(None, alias="SeniorCommunityYN")
    pets_allowed: Optional[List[str]] = Field(None, alias="PetsAllowed")
    
    # Listing Intelligence
    original_list_price: Optional[float] = Field(None, alias="OriginalListPrice")
    cumulative_days_on_market: Optional[int] = Field(None, alias="CumulativeDaysOnMarket")
    
    # Structure
    stories: Optional[float] = Field(None, alias="Stories")

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    modification_timestamp: Optional[datetime] = Field(None, alias="ModificationTimestamp")

class NearbyPlace(BaseModel):
    name: str
    address: Optional[str] = None
    distance_miles: Optional[float] = None
    place_type: Optional[str] = None  # e.g. "Hospital", "Urgent Care", "Primary Care", "Rehabilitation", "Medical Clinic" - only set for medical_care entries

class ExploreNearbyPlace(BaseModel):
    place_id: str
    name: str
    category: str
    address: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    distance_miles: Optional[float] = None
    latitude: float
    longitude: float

class ExploreNearbyResponse(BaseModel):
    available: bool = True
    category: str
    origin: Dict[str, float]
    radius_miles: float
    places: List[ExploreNearbyPlace] = []
    cache_hit: bool = False
    source: str = "google_places_nearby_search_new"
    sku: str = "nearby_search_pro"

class ExploreNearbyPlaceDetails(BaseModel):
    place_id: str
    rating: Optional[float] = None
    review_count: Optional[int] = None
    cache_hit: bool = False
    source: str = "google_place_details_new"
    sku: str = "place_details_enterprise"

class NearbyAmenitiesResponse(BaseModel):
    schools: List[NearbyPlace] = []
    private_schools: List[NearbyPlace] = []
    grocery_stores: List[NearbyPlace] = []
    medical_care: List[NearbyPlace] = []  # hospitals, urgent care, primary care, rehab - see NearbyPlace.place_type for which
    parks: List[NearbyPlace] = []
    downtown_areas: List[NearbyPlace] = []  # nearby named town centers, e.g. "Downtown Wayne" - not businesses
    airports: List[NearbyPlace] = []
    # False when nothing could be looked up at all (no API key configured, or the lookup failed
    # with no usable cache) - distinct from an empty list, which means "we looked, found nothing
    # in that one category." The PDF omits the whole amenities section when this is False.
    available: bool = True
    cached_at: Optional[datetime] = None

class PropertyLookupRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    address: Optional[str] = None
    listing_key: Optional[str] = None

class SimilarPropertiesRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    listing_key: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    price: Optional[float] = None
    bedrooms: Optional[float] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    radius: Optional[float] = None
    min_price: Optional[int] = None
    max_price: Optional[int] = None
    min_beds: Optional[int] = None
    min_baths: Optional[float] = None
    min_acres: Optional[float] = None
