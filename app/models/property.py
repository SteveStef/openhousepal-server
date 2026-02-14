from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from typing import Optional, List, Dict, Any, Union
from datetime import datetime
from enum import Enum

class HomeStatus(str, Enum):
    FOR_SALE = "forSale"
    FOR_RENT = "forRent"
    RECENTLY_SOLD = "recentlySold"

class HomeType(str, Enum):
    SINGLE_FAMILY = "SINGLE_FAMILY"
    TOWNHOUSE = "TOWNHOUSE"
    CONDO = "CONDO"
    MULTI_FAMILY = "MULTI_FAMILY"
    LOT = "LOT"

class ListingSubType(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    is_FSBA: Optional[bool] = None
    is_open_house: Optional[bool] = None

class OpenHouseShowing(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    open_house_end: Optional[int] = None
    open_house_start: Optional[int] = None

class OpenHouseInfo(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    open_house_showing: Optional[List[OpenHouseShowing]] = None

class PropertyListing(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    bathrooms: Optional[float] = None
    bedrooms: Optional[int] = None
    city: Optional[str] = None
    country: Optional[str] = None
    currency: Optional[str] = None
    date_price_changed: Optional[int] = None
    days_on_zillow: Optional[int] = None
    home_status: Optional[str] = None
    home_status_for_hdp: Optional[str] = None
    home_type: Optional[str] = None
    img_src: Optional[str] = None
    is_featured: Optional[bool] = None
    is_non_owner_occupied: Optional[bool] = None
    is_preforeclosure_auction: Optional[bool] = None
    is_premier_builder: Optional[bool] = None
    is_showcase_listing: Optional[bool] = None
    is_unmappable: Optional[bool] = None
    is_zillow_owned: Optional[bool] = None
    latitude: Optional[float] = None
    listing_sub_type: Optional[ListingSubType] = None
    living_area: Optional[Union[int, float]] = None
    longitude: Optional[float] = None
    lot_area_unit: Optional[str] = None
    lot_area_value: Optional[float] = None
    open_house: Optional[str] = None
    open_house_info: Optional[OpenHouseInfo] = None
    price: Optional[Union[int, float]] = None
    price_change: Optional[int] = None
    price_for_hdp: Optional[int] = None
    price_reduction: Optional[str] = None
    rent_zestimate: Optional[int] = None
    should_highlight: Optional[bool] = None
    state: Optional[str] = None
    street_address: Optional[str] = None
    tax_assessed_value: Optional[int] = None
    time_on_zillow: Optional[int] = None
    unit: Optional[str] = None
    zestimate: Optional[int] = None
    zipcode: Optional[str] = None
    listing_key: Optional[Union[str, int]] = None

class ZillowSearchResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    results: List[PropertyListing]
    results_per_page: int
    total_pages: int
    total_result_count: int

class PropertyAddress(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    city: Optional[str] = None
    community: Optional[str] = None
    neighborhood: Optional[str] = None
    state: Optional[str] = None
    street_address: Optional[str] = None
    subdivision: Optional[str] = None
    zipcode: Optional[str] = None

class ImageSource(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    url: Optional[str] = None
    width: Optional[int] = None

class MixedSources(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    jpeg: Optional[List[ImageSource]] = None
    webp: Optional[List[ImageSource]] = None

class OriginalPhoto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    caption: Optional[str] = None
    mixed_sources: Optional[MixedSources] = None

class AtAGlanceFact(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    fact_label: Optional[str] = None
    fact_value: Optional[str] = None

class OtherFact(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    name: Optional[str] = None
    value: Optional[str] = None

class ResoFacts(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    above_grade_finished_area: Optional[Union[List[str], str]] = None
    accessibility_features: Optional[List[str]] = None
    additional_fee_info: Optional[Union[List[str], str]] = None
    additional_parcels_description: Optional[Union[List[str], str]] = None
    appliances: Optional[List[str]] = None
    architectural_style: Optional[Union[List[str], str]] = None
    association_amenities: Optional[Union[str, List[str]]] = None
    association_fee: Optional[Union[List[str], str, int]] = None
    association_fee_2: Optional[Union[List[str], str]] = None
    association_fee_includes: Optional[List[str]] = None
    association_name: Optional[Union[List[str], str]] = None
    association_name_2: Optional[Union[List[str], str]] = None
    association_phone: Optional[Union[List[str], str]] = None
    association_phone_2: Optional[Union[List[str], str]] = None
    associations: Optional[List[Dict[str, Any]]] = None
    at_a_glance_facts: Optional[List[Any]] = None
    attic: Optional[Union[List[str], str]] = None
    availability_date: Optional[Union[List[str], str, int]] = None
    basement: Optional[Union[List[str], str]] = None
    basement_yn: Optional[bool] = None
    bathrooms: Optional[Union[int, float]] = None
    bathrooms_float: Optional[float] = None
    bathrooms_full: Optional[Union[int, float]] = None
    bathrooms_half: Optional[Union[int, float]] = None
    bathrooms_one_quarter: Optional[Union[int, float]] = None
    bathrooms_partial: Optional[Union[int, float]] = None
    bathrooms_three_quarter: Optional[Union[int, float]] = None
    bedrooms: Optional[Union[int, float]] = None
    below_grade_finished_area: Optional[Union[List[str], str]] = None
    body_type: Optional[Union[List[str], str]] = None
    builder_model: Optional[Union[List[str], str]] = None
    builder_name: Optional[Union[List[str], str]] = None
    building_area: Optional[Union[List[str], str]] = None
    building_area_source: Optional[Union[List[str], str]] = None
    building_features: Optional[Union[List[str], str]] = None
    building_name: Optional[Union[List[str], str]] = None
    can_raise_horses: Optional[bool] = None
    carport_parking_capacity: Optional[Union[int, float]] = None
    city_region: Optional[Union[List[str], str]] = None
    common_walls: Optional[Union[List[str], str]] = None
    community_features: Optional[List[str]] = None
    compensation_based_on: Optional[Union[List[str], str]] = None
    construction_materials: Optional[List[str]] = None
    contingency: Optional[Union[List[str], str]] = None
    cooling: Optional[List[str]] = None
    covered_parking_capacity: Optional[Union[int, float]] = None
    crops_included_yn: Optional[bool] = None
    cumulative_days_on_market: Optional[Union[str, List[str]]] = None
    development_status: Optional[Union[List[str], str]] = None
    door_features: Optional[List[str]] = None
    electric: Optional[List[str]] = None
    elementary_school: Optional[Union[List[str], str]] = None
    elementary_school_district: Optional[Union[List[str], str]] = None
    elevation: Optional[Union[List[str], str]] = None
    elevation_units: Optional[Union[List[str], str]] = None
    entry_level: Optional[Union[List[str], str]] = None
    entry_location: Optional[Union[List[str], str]] = None
    exclusions: Optional[List[str]] = None
    exterior_features: Optional[List[str]] = None
    fees_and_dues: Optional[List[Dict[str, Any]]] = None
    fencing: Optional[Union[List[str], str]] = None
    fireplace_features: Optional[List[str]] = None
    fireplaces: Optional[Union[int, float]] = None
    flooring: Optional[List[str]] = None
    foundation_area: Optional[Union[List[str], str]] = None
    foundation_details: Optional[List[str]] = None
    frontage_length: Optional[Union[List[str], str]] = None
    frontage_type: Optional[Union[List[str], str]] = None
    furnished: Optional[bool] = None
    garage_parking_capacity: Optional[Union[int, float]] = None
    gas: Optional[Union[List[str], str]] = None
    green_building_verification_type: Optional[Union[List[str], str]] = None
    green_energy_efficient: Optional[Union[List[str], str]] = None
    green_energy_generation: Optional[Union[List[str], str]] = None
    green_indoor_air_quality: Optional[Union[List[str], str]] = None
    green_sustainability: Optional[Union[List[str], str]] = None
    green_water_conservation: Optional[Union[List[str], str]] = None
    has_additional_parcels: Optional[bool] = None
    has_association: Optional[bool] = None
    has_attached_garage: Optional[bool] = None
    has_attached_property: Optional[bool] = None
    has_carport: Optional[bool] = None
    has_cooling: Optional[bool] = None
    has_electric_on_property: Optional[bool] = None
    has_fireplace: Optional[bool] = None
    has_garage: Optional[bool] = None
    has_heating: Optional[bool] = None
    has_home_warranty: Optional[bool] = None
    has_land_lease: Optional[bool] = None
    has_open_parking: Optional[bool] = None
    has_pets_allowed: Optional[bool] = None
    has_private_pool: Optional[bool] = None
    has_rent_control: Optional[bool] = None
    has_spa: Optional[bool] = None
    has_view: Optional[bool] = None
    has_waterfront_view: Optional[bool] = None
    heating: Optional[List[str]] = None
    high_school: Optional[Union[List[str], str]] = None
    high_school_district: Optional[Union[List[str], str]] = None
    hoa_fee: Optional[Union[List[str], str, int, float]] = None
    hoa_fee_total: Optional[Union[List[str], str]] = None
    home_type: Optional[Union[List[str], str]] = None
    horse_amenities: Optional[Union[str, List[str]]] = None
    horse_yn: Optional[bool] = None
    inclusions: Optional[List[str]] = None
    income_includes: Optional[Union[List[str], str]] = None
    interior_features: Optional[List[str]] = None
    irrigation_water_rights_acres: Optional[float] = None
    irrigation_water_rights_yn: Optional[bool] = None
    is_new_construction: Optional[bool] = None
    is_senior_community: Optional[bool] = None
    land_lease_amount: Optional[float] = None
    land_lease_expiration_date: Optional[Union[List[str], str]] = None
    laundry_features: Optional[List[str]] = None
    lease_term: Optional[Union[List[str], str]] = None
    levels: Optional[Union[List[str], str]] = None
    list_aor: Optional[Union[List[str], str]] = None
    listing_id: Optional[Union[List[str], str]] = None
    listing_terms: Optional[Union[List[str], str]] = None
    living_area: Optional[Union[List[str], str, int, float]] = None
    living_area_range: Optional[Union[List[str], str]] = None
    living_area_range_units: Optional[Union[List[str], str]] = None
    living_quarters: Optional[List[str]] = None
    lot_features: Optional[List[str]] = None
    lot_size: Optional[Union[List[str], str, int, float]] = None
    lot_size_dimensions: Optional[Union[List[str], str]] = None
    main_level_bathrooms: Optional[Union[int, float]] = None
    main_level_bedrooms: Optional[Union[int, float]] = None
    marketing_type: Optional[Union[List[str], str]] = None
    media: Optional[List[Dict[str, Any]]] = None
    middle_or_junior_school: Optional[Union[List[str], str]] = None
    middle_or_junior_school_district: Optional[Union[List[str], str]] = None
    municipality: Optional[Union[List[str], str]] = None
    number_of_units_in_community: Optional[Union[int, float]] = None
    number_of_units_vacant: Optional[Union[int, float]] = None
    offer_review_date: Optional[Union[List[str], str]] = None
    on_market_date: Optional[datetime] = None
    open_parking_capacity: Optional[Union[int, float]] = None
    other_equipment: Optional[Union[str, List[str]]] = None
    other_facts: Optional[List[OtherFact]] = None
    other_parking: Optional[list[str]] = None
    other_structures: Optional[List[str]] = None
    ownership: Optional[Union[List[str], str]] = None
    ownership_type: Optional[Union[List[str], str]] = None
    parcel_number: Optional[Union[List[str], str]] = None
    park_name: Optional[Union[List[str], str]] = None
    parking_capacity: Optional[Union[int, float]] = None
    parking_features: Optional[List[str]] = None
    patio_and_porch_features: Optional[List[str]] = None
    pets_max_weight: Optional[Union[int, float]] = None
    pool_features: Optional[List[str]] = None
    price_per_square_foot: Optional[Union[int, float]] = None
    property_condition: Optional[Union[List[str], str]] = None
    property_sub_type: Optional[List[str]] = None
    road_surface_type: Optional[List[str]] = None
    roof_type: Optional[Union[List[str], str]] = None
    room_types: Optional[List[str]] = None
    rooms: Optional[List[Dict[str, Any]]] = None
    security_features: Optional[List[str]] = None
    sewer: Optional[List[str]] = None
    spa_features: Optional[Union[str, List[str]]] = None
    special_listing_conditions: Optional[Union[List[str], str]] = None
    stories: Optional[Union[int, float]] = None
    stories_decimal: Optional[float] = None
    stories_total: Optional[Union[int, float]] = None
    structure_type: Optional[Union[List[str], str]] = None
    subdivision_name: Optional[Union[List[str], str]] = None
    tax_annual_amount: Optional[float] = None
    tax_assessed_value: Optional[float] = None
    tenant_pays: Optional[Union[List[str], str]] = None
    topography: Optional[Union[List[str], str]] = None
    total_actual_rent: Optional[float] = None
    utilities: Optional[List[str]] = None
    vegetation: Optional[Union[List[str], str]] = None
    view: Optional[List[str]] = None
    virtual_tour: Optional[Union[List[str], str]] = None
    water_body_name: Optional[Union[List[str], str]] = None
    water_source: Optional[List[str]] = None
    water_view: Optional[Union[List[str], str]] = None
    water_view_yn: Optional[bool] = None
    waterfront_features: Optional[Union[str, List[str]]] = None
    window_features: Optional[List[str]] = None
    wooded_area: Optional[Union[List[str], str]] = None
    year_built: Optional[Union[int, float]] = None
    year_built_effective: Optional[Union[int, float]] = None
    zoning: Optional[Union[List[str], str]] = None
    zoning_description: Optional[Union[List[str], str]] = None
    
    # New Standardized Fields
    elementary_school: Optional[str] = None
    middle_or_junior_school: Optional[str] = None
    high_school: Optional[str] = None
    school_district_name: Optional[str] = None
    county: Optional[str] = None
    directions: Optional[str] = None
    cross_street: Optional[str] = None
    walk_score: Optional[Union[int, float]] = None
    direction_faces: Optional[str] = None
    tax_assessment_amount: Optional[float] = None
    land_assessment_amount: Optional[float] = None
    improvement_assessment_amount: Optional[float] = None
    assessment_year: Optional[Union[int, float]] = None
    capital_contribution_fee: Optional[float] = None
    possession: Optional[List[str]] = None
    cooling_fuel: Optional[List[str]] = None
    heating_fuel: Optional[List[str]] = None
    lot_size_acres: Optional[float] = None
    attached_garage_yn: Optional[bool] = None
    new_construction_yn: Optional[bool] = None
    senior_community_yn: Optional[bool] = None
    pets_allowed: Optional[List[str]] = None
    original_list_price: Optional[float] = None
    days_on_market: Optional[Union[int, float]] = None
    cumulative_days_on_market: Optional[Union[int, float]] = None
    standard_status: Optional[str] = None
    
    updated_at: Optional[Union[datetime, str]] = None

class TaxHistoryEntry(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    tax_increase_rate: Optional[float] = None
    tax_paid: Optional[float] = None
    time: Optional[int] = None
    value: Optional[int] = None
    value_increase_rate: Optional[float] = None


class PriceHistoryEntry(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    buyer_agent: Optional[Dict[str, Any]] = None
    date: Optional[str] = None
    event: Optional[str] = None
    posting_is_rental: Optional[bool] = None
    price: Optional[int] = None
    price_change_rate: Optional[float] = None
    price_per_square_foot: Optional[int] = None
    seller_agent: Optional[Dict[str, Any]] = None
    show_county_link: Optional[bool] = None
    source: Optional[str] = None
    time: Optional[int] = None

class PropertyDetailResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    
    # Core property information
    abbreviated_address: Optional[str] = None
    address: Optional[PropertyAddress] = None
    bathrooms: Optional[Union[int, float]] = None
    bedrooms: Optional[Union[int, float]] = None
    city: Optional[str] = None
    home_status: Optional[str] = None
    home_type: Optional[str] = None
    living_area: Optional[Union[int, float]] = None
    lot_size: Optional[Union[int, float]] = None
    price: Optional[Union[int, float]] = None
    zestimate: Optional[Union[int, float]] = None
    year_built: Optional[Union[int, float]] = None
    listing_key: Optional[Union[str, int]] = None
    mls_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    description: Optional[str] = None
    list_office_name: Optional[str] = None
    list_office_phone: Optional[str] = None
    list_agent_full_name: Optional[str] = None
    list_agent_email: Optional[str] = None
    
    # Financial and tax information
    property_tax_rate: Optional[float] = None
    tax_history: Optional[List[TaxHistoryEntry]] = None
    price_history: Optional[List[PriceHistoryEntry]] = None
    
    # Media and schedule information
    original_photos: Optional[List[OriginalPhoto]] = None
    open_house_schedule: Optional[List[Dict[str, Any]]] = None
    days_on_zillow: Optional[int] = None
    days_on_market: Optional[int] = None
    standard_status: Optional[str] = None
    
    # Construction and facts
    new_construction_type: Optional[str] = None
    reso_facts: Optional[ResoFacts] = None

class PropertySaveResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    property_id: str
    listing_key: Optional[Union[str, int]] = None
    abbreviated_address: Optional[str] = None
    price: Optional[Union[int, float]] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    living_area: Optional[int] = None
    year_built: Optional[int] = None
    home_type: Optional[str] = None
    lot_size: Optional[int] = None
    original_photos: Optional[List[OriginalPhoto]] = None

class PropertyLookupRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)
    address: Optional[str] = None
    listing_key: Optional[str] = None
    new_construction_type: Optional[str] = None
    reso_facts: Optional[ResoFacts] = None
