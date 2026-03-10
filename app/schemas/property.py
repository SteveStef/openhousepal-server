from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from typing import Optional, List, Dict, Any
from datetime import datetime

class PropertyBase(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, 
        populate_by_name=True, 
        from_attributes=True
    )
    
    listing_key: Optional[str] = Field(None, alias="ListingKey")
    street_address: Optional[str] = Field(None, alias="FullStreetAddress")
    unparsed_address: Optional[str] = Field(None, alias="UnparsedAddress")
    city: Optional[str] = Field(None, alias="City")
    state: Optional[str] = Field(None, alias="StateOrProvince")
    zipcode: Optional[str] = Field(None, alias="PostalCode")

class PropertyCreate(PropertyBase):
    price: Optional[float] = Field(None, alias="ListPrice")
    bedrooms: Optional[float] = Field(None, alias="BedroomsTotal")
    bathrooms: Optional[float] = Field(None, alias="BathroomsTotal")
    living_area: Optional[float] = Field(None, alias="LivingArea")
    lot_size: Optional[float] = Field(None, alias="LotSizeSquareFeet")
    year_built: Optional[int] = Field(None, alias="YearBuilt")
    home_type: Optional[str] = Field(None, alias="PropertyType")
    home_status: Optional[str] = Field(None, alias="MlsStatus")
    latitude: Optional[float] = Field(None, alias="Latitude")
    longitude: Optional[float] = Field(None, alias="Longitude")
    img_src: Optional[str] = Field(None, alias="ListPictureURL")
    
    # Raw MLS data
    mls_property_type: Optional[str] = Field(None, alias="MlsPropertyType")
    mls_structure_design_type: Optional[str] = Field(None, alias="MlsStructureDesignType")
    price_per_square_feet: Optional[float] = Field(None, alias="PricePerSquareFoot")
    mls_incorporated_city_name: Optional[str] = Field(None, alias="IncorporatedCityName")

class PropertyUpdate(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    
    price: Optional[float] = None
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    living_area: Optional[float] = None
    home_status: Optional[str] = None
    img_src: Optional[str] = None

class PropertySummary(PropertyBase):
    id: str
    price: Optional[float] = Field(None, alias="ListPrice")
    bedrooms: Optional[float] = Field(None, alias="BedroomsTotal")
    bathrooms: Optional[float] = Field(None, alias="BathroomsTotal")
    living_area: Optional[float] = Field(None, alias="LivingArea")
    home_type: Optional[str] = Field(None, alias="PropertyType")
    home_status: Optional[str] = Field(None, alias="MlsStatus")
    img_src: Optional[str] = Field(None, alias="ListPictureURL")

class Property(PropertyBase):
    id: str
    price: Optional[float] = Field(None, alias="ListPrice")
    bedrooms: Optional[float] = Field(None, alias="BedroomsTotal")
    bathrooms: Optional[float] = Field(None, alias="BathroomsTotal")
    living_area: Optional[float] = Field(None, alias="LivingArea")
    lot_size: Optional[float] = Field(None, alias="LotSizeSquareFeet")
    year_built: Optional[int] = Field(None, alias="YearBuilt")
    home_type: Optional[str] = Field(None, alias="PropertyType")
    home_status: Optional[str] = Field(None, alias="MlsStatus")
    latitude: Optional[float] = Field(None, alias="Latitude")
    longitude: Optional[float] = Field(None, alias="Longitude")
    img_src: Optional[str] = Field(None, alias="ListPictureURL")
    description: Optional[str] = Field(None, alias="PublicRemarks")
    photos: Optional[List[str]] = None
    
    # Raw MLS data
    mls_property_type: Optional[str] = Field(None, alias="MlsPropertyType")
    mls_structure_design_type: Optional[str] = Field(None, alias="MlsStructureDesignType")
    price_per_square_feet: Optional[float] = Field(None, alias="PricePerSquareFoot")
    mls_incorporated_city_name: Optional[str] = Field(None, alias="IncorporatedCityName")
    
    # Detailed fields
    architectural_style: Optional[List[str]] = None
    construction_materials: Optional[List[str]] = None
    interior_features: Optional[List[str]] = None
    exterior_features: Optional[List[str]] = None
    garage_spaces: Optional[float] = None
    has_garage: Optional[bool] = None
    association_fee: Optional[float] = None
    
    created_at: datetime
    updated_at: Optional[datetime] = None

class AddPropertyToCollection(BaseModel):
    collection_id: str
    property_id: Optional[str] = None
    listing_key: Optional[str] = None
