from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from typing import Optional, List, Dict, Any, Union
from datetime import datetime, timezone

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
    city: str = Field(..., alias="City")
    state: str = Field(..., alias="StateOrProvince")
    zipcode: Optional[str] = Field(None, alias="PostalCode")
    
    price: Optional[float] = Field(None, alias="ListPrice")
    bedrooms: Optional[float] = Field(None, alias="BedroomsTotal")
    bathrooms: Optional[float] = Field(None, alias="BathroomsTotal")
    living_area: Optional[float] = Field(None, alias="LivingArea")
    home_type: Optional[str] = Field(None, alias="PropertyType")
    
    # Raw MLS data
    mls_property_type: Optional[str] = Field(None, alias="MlsPropertyType")
    mls_structure_design_type: Optional[str] = Field(None, alias="MlsStructureDesignType")
    price_per_square_feet: Optional[float] = Field(None, alias="PricePerSquareFoot")
    mls_incorporated_city_name: Optional[str] = Field(None, alias="IncorporatedCityName")
    
    home_status: str = Field(..., alias="MlsStatus")
    
    subdivision_name: Optional[str] = Field(None, alias="SubdivisionName")
    township: Optional[str] = Field(None, alias="MLSAreaMajor")
    
    img_src: Optional[str] = Field(None, alias="ListPictureURL")
    latitude: Optional[float] = Field(None, alias="Latitude")
    longitude: Optional[float] = Field(None, alias="Longitude")
    
    days_on_market: Optional[int] = Field(None, alias="DaysOnMarket")
    year_built: Optional[int] = Field(None, alias="YearBuilt")
    mls_list_date: Optional[datetime] = Field(None, alias="MLSListDate")
    price_change_timestamp: Optional[datetime] = Field(None, alias="PriceChangeTimestamp")

    @model_validator(mode='after')
    def calculate_dom(self) -> 'PropertySummaryResponse':
        if self.mls_list_date:
            now = datetime.now(timezone.utc)
            list_date = self.mls_list_date
            if list_date.tzinfo is None:
                list_date = list_date.replace(tzinfo=timezone.utc)
            delta = now - list_date
            self.days_on_market = max(0, delta.days)
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
    lot_size_acres: Optional[float] = Field(None, alias="LotSizeAcres")
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

    @model_validator(mode='after')
    def calculate_dom(self) -> 'PropertyDetailResponse':
        if self.mls_list_date:
            now = datetime.now(timezone.utc)
            list_date = self.mls_list_date
            if list_date.tzinfo is None:
                list_date = list_date.replace(tzinfo=timezone.utc)
            delta = now - list_date
            self.days_on_market = max(0, delta.days)
        return self

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
