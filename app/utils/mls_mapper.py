import re
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List
from app.models.database import HomeType

logger = logging.getLogger(__name__)

# Centralized list of fields to select from Bright MLS BrightProperties endpoint
BRIGHT_PROPERTY_SELECT_FIELDS = [
    "ListingKey", "ListingId", "FullStreetAddress", "UnparsedAddress", "City", "StateOrProvince",
    "PostalCode", "ListPrice", "PricePerSquareFoot", "BedroomsTotal", "BathroomsFull",
    "BathroomsHalf", "BathroomsTotalInteger", "LivingArea", "LotSizeSquareFeet",
    "PropertyType", "StructureDesignType", "MlsStatus", 
    "Latitude", "Longitude", "ListPictureURL", "ListPicture2URL", "ListPicture3URL",
    "MLSAreaMajor", "IncorporatedCityName",
    "PublicRemarks", "ListAgentFullName", "ListAgentEmail",
    "ListOfficeName", "ListOfficePhone", "ArchitecturalStyle", "ConstructionMaterials",
    "Roof", "FoundationDetails", "Levels", "InteriorFeatures",
    "ExteriorFeatures", "Flooring", "Appliances", "FireplacesTotal", "FireplaceFeatures",
    "DoorFeatures", "WindowFeatures", "Cooling", "Heating", "WaterSource",
    "Sewer", "Utilities", "GarageSpaces", "ParkingFeatures",
    "GarageYN", "AssociationFee", "AssociationFeeFrequency", "AssociationAmenities",
    "AssociationFeeIncludes", "AssociationYN", "LotFeatures", 
    "View", "WaterfrontFeatures", "ViewYN", "TaxAnnualAmount",
    "TaxYear", "YearBuilt", "ElementarySchool", "MiddleOrJuniorSchool",
    "HighSchool", "SchoolDistrictName", "County", "Directions",
    "Zoning", "TaxAssessmentAmount", "AssessmentYear", "Possession", 
    "ListingTaxID", "CoolingFuel", "HeatingFuel", "AboveGradeFinishedArea", 
    "BelowGradeFinishedArea", "Basement", "AccessibilityFeatures", "BasementYN", 
    "CentralAirYN", "FireplaceYN", "AssociationFee2", "AssociationFee2Frequency", 
    "ListAgentPreferredPhone", "LotSizeAcres", "AttachedGarageYN", 
    "NewConstructionYN", "SeniorCommunityYN", "PetsAllowed", "OriginalListPrice", 
    "DaysOnMarket", "CumulativeDaysOnMarket", "Stories", "SubdivisionName", 
    "MLSListDate", "PriceChangeTimestamp", "ModificationTimestamp", 
    "OneBedroomUnits", "TwoBedroomUnits", "ThreeBedroomUnits"
]

def clean_address(address: str) -> str:
    """Extracts the primary street address from a full address string."""
    if not address: return ""
    parts = [p.strip() for p in address.split(',')]
    for part in parts:
        if re.match(r'^\d+', part):
            return part
    return parts[0]

def get_best_photo_url(media_item: Dict[str, Any]) -> str | None:
    """Returns the best available photo URL from a BrightMedia item, prioritizing HD."""
    return (
        media_item.get("MediaURLHD") or 
        media_item.get("MediaURLHiRes") or 
        media_item.get("MediaURLFull") or 
        media_item.get("MediaURL")
    )

def parse_dt(dt_str: str) -> datetime | None:
    """Parses an ISO format date string from MLS into a UTC datetime object."""
    if not dt_str: return None
    try:
        # Standardizing 'Z' to '+00:00' for fromisoformat compatibility in all Python versions
        return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    except Exception:
        return None

def map_reso_to_internal(item: Dict[str, Any], photo_map: Dict[str, List[str]] = None) -> Dict[str, Any]:
    """
    The Master Mapper. Converts RESO Web API fields to our internal snake_case database schema.
    Handles complex logic like HomeType detection, bath calculation, and township normalization.
    """
    listing_key = str(item.get("ListingKey", ""))
    
    # 1. HomeType Mapping
    m_prop = item.get("PropertyType")
    m_design = item.get("StructureDesignType")
    h_type = HomeType.OTHER
    if m_prop == "Residential":
        if m_design == "Detached": h_type = HomeType.SINGLE_FAMILY
        elif m_design and ("Townhouse" in m_design or "Row" in m_design or "Twin" in m_design): h_type = HomeType.TOWNHOUSE
        elif m_design and ("Unit" in m_design or "Flat" in m_design or "Apartment" in m_design or "Penthouse" in m_design): h_type = HomeType.CONDO
        else: h_type = HomeType.SINGLE_FAMILY
    elif m_prop == "Multi-Family": h_type = HomeType.MULTI_FAMILY
    elif m_prop == "Land": h_type = HomeType.LAND
    elif m_prop == "Farm": h_type = HomeType.FARM
    elif m_prop == "Residential Lease": h_type = HomeType.RESIDENTIAL_LEASE
    elif m_prop and ("Commercial" in m_prop or "Industrial" in m_prop): h_type = HomeType.COMMERCIAL

    # 2. Bath calculation
    b_full = item.get("BathroomsFull")
    b_half = item.get("BathroomsHalf")
    if b_full is not None or b_half is not None:
        bathrooms = float(b_full or 0) + (float(b_half or 0) * 0.5)
    else:
        bathrooms = float(item.get("BathroomsTotalInteger") or 0)

    # 3. Bedroom calculation (Fallback for multi-unit properties)
    bedrooms = item.get("BedroomsTotal")
    if bedrooms is None:
        u1 = item.get("OneBedroomUnits") or 0
        u2 = item.get("TwoBedroomUnits") or 0
        u3 = item.get("ThreeBedroomUnits") or 0
        if u1 or u2 or u3:
            bedrooms = float((1 * u1) + (2 * u2) + (3 * u3))

    # 4. Photo processing (Simplified list of strings)
    fetched_photos = []
    if photo_map and listing_key in photo_map:
        fetched_photos = photo_map[listing_key]
    else:
        # Fallback to the primary photo field if no photo_map provided
        # Use HD/HiRes versions if they happen to be in the main item
        primary = get_best_photo_url(item) or item.get("ListPicture3URL") or item.get("ListPictureURL")
        if primary:
            fetched_photos = [primary]
    
    # 5. Township Logic: IncorporatedCityName (cleaner) -> MLSAreaMajor (fallback)
    township = item.get("IncorporatedCityName")
    if not township:
        township = item.get("MLSAreaMajor", "")
        if township:
            # Strip numeric codes like (10436)
            township = re.sub(r'\s*\(\d+\)$', '', township).strip()
    
    if township:
        # Final cleanup: Strip suffixes and convert to UPPERCASE for robust matching
        township = re.sub(r'\s+(Twp|Township|Boro|Borough|City|Town)$', '', township, flags=re.I).strip()
        township = township.upper()
    else:
        township = None

    # 6. School District
    school_district = school_district.strip().upper() if (school_district := item.get("SchoolDistrictName")) else None

    # 7. Construct Full Address
    street = clean_address(item.get("FullStreetAddress") or item.get("UnparsedAddress"))
    city = item.get("City", "")
    state = item.get("StateOrProvince", "")
    zipcode = item.get("PostalCode", "")
    full_address = f"{street}, {city}, {state} {zipcode}".strip()

    return {
        "listing_key": listing_key,
        "listing_id": item.get("ListingId"),
        "street_address": street,
        "unparsed_address": item.get("UnparsedAddress"),
        "city": city,
        "state": state,
        "zipcode": zipcode,
        "full_address": full_address,
        "price": item.get("ListPrice"),
        "price_per_square_feet": item.get("PricePerSquareFoot"),
        "mls_incorporated_city_name": item.get("IncorporatedCityName"),
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "living_area": item.get("LivingArea"),
        "lot_size": item.get("LotSizeSquareFeet"),
        "home_type": h_type.value,
        "home_status": item.get("MlsStatus"),
        "mls_property_type": m_prop,
        "mls_structure_design_type": m_design,
        "latitude": item.get("Latitude"),
        "longitude": item.get("Longitude"),
        "img_src": item.get("ListPicture3URL") or item.get("ListPictureURL"),
        "description": item.get("PublicRemarks"),
        "photos": fetched_photos,
        "list_agent_full_name": item.get("ListAgentFullName"),
        "list_agent_email": item.get("ListAgentEmail"),
        "list_office_name": item.get("ListOfficeName"),
        "list_office_phone": item.get("ListOfficePhone"),
        "architectural_style": item.get("ArchitecturalStyle"),
        "construction_materials": item.get("ConstructionMaterials"),
        "roof_type": item.get("Roof"),
        "foundation_details": item.get("FoundationDetails"),
        "structure_type": item.get("StructureType"),
        "levels": item.get("Levels"),
        "interior_features": item.get("InteriorFeatures"),
        "exterior_features": item.get("ExteriorFeatures"),
        "flooring": item.get("Flooring"),
        "appliances": item.get("Appliances"),
        "fireplaces": item.get("FireplacesTotal"),
        "fireplace_features": item.get("FireplaceFeatures"),
        "door_features": item.get("DoorFeatures"),
        "window_features": item.get("WindowFeatures"),
        "cooling": item.get("Cooling"),
        "heating": item.get("Heating"),
        "water_source": item.get("WaterSource"),
        "sewer": item.get("Sewer"),
        "utilities": item.get("Utilities"),
        "garage_spaces": item.get("GarageSpaces"),
        "parking_features": item.get("ParkingFeatures"),
        "has_garage": item.get("GarageYN"),
        "association_fee": item.get("AssociationFee"),
        "association_fee_frequency": item.get("AssociationFeeFrequency"),
        "association_amenities": item.get("AssociationAmenities"),
        "association_fee_includes": item.get("AssociationFeeIncludes"),
        "has_association": item.get("AssociationYN"),
        "lot_features": item.get("LotFeatures"),
        "view": item.get("View"),
        "waterfront_features": item.get("WaterfrontFeatures"),
        "has_waterfront_view": item.get("WaterfrontViewYN"),
        "has_view": item.get("ViewYN"),
        "tax_annual_amount": item.get("TaxAnnualAmount"),
        "tax_year": item.get("TaxYear"),
        "year_built": item.get("YearBuilt"),
        "elementary_school": item.get("ElementarySchool"),
        "middle_or_junior_school": item.get("MiddleOrJuniorSchool"),
        "high_school": item.get("HighSchool"),
        "school_district_name": school_district,
        "county": item.get("County"),
        "township": township,
        "directions": item.get("Directions"),
        "zoning": item.get("Zoning"),
        "tax_assessment_amount": item.get("TaxAssessmentAmount"),
        "assessment_year": item.get("AssessmentYear"),
        "possession": item.get("Possession"),
        "listing_tax_id": item.get("ListingTaxID"),
        "cooling_fuel": item.get("CoolingFuel"),
        "heating_fuel": item.get("HeatingFuel"),
        "above_grade_finished_area": item.get("AboveGradeFinishedArea"),
        "below_grade_finished_area": item.get("BelowGradeFinishedArea"),
        "basement": item.get("Basement"),
        "accessibility_features": item.get("AccessibilityFeatures"),
        "has_basement": item.get("BasementYN"),
        "has_central_air": item.get("CentralAirYN"),
        "has_fireplace": item.get("FireplaceYN"),
        "association_fee_2": item.get("AssociationFee2"),
        "association_fee_2_frequency": item.get("AssociationFee2Frequency"),
        "list_agent_preferred_phone": item.get("ListAgentPreferredPhone"),
        "lot_size_acres": item.get("LotSizeAcres"),
        "attached_garage_yn": item.get("AttachedGarageYN"),
        "new_construction_yn": item.get("NewConstructionYN"),
        "senior_community_yn": item.get("SeniorCommunityYN"),
        "pets_allowed": item.get("PetsAllowed"),
        "original_list_price": item.get("OriginalListPrice"),
        "days_on_market": item.get("DaysOnMarket"),
        "cumulative_days_on_market": item.get("CumulativeDaysOnMarket"),
        "stories": item.get("Stories"),
        "subdivision_name": item.get("SubdivisionName"),
        "mls_list_date": parse_dt(item.get("MLSListDate")),
        "price_change_timestamp": parse_dt(item.get("PriceChangeTimestamp")),
        "modification_timestamp": parse_dt(item.get("ModificationTimestamp")),
        "raw_mls_data": item
    }
