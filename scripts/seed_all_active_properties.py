import asyncio
import os
import sys
import httpx
import logging
import re
from typing import Dict, Any, List
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from dotenv import load_dotenv

# Add the app directory to sys.path so we can import internal modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property, HomeType

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("property_seed")

# --- Configuration ---
CLIENT_ID = os.getenv("BRIGHT_MLS_CLIENT")
CLIENT_SECRET = os.getenv("BRIGHT_MLS_SECRET")
IS_PROD = os.getenv("BRIGHT_MLS_ENV", "test").lower() == "prod"

if IS_PROD:
    TOKEN_URL = os.getenv("BRIGHT_TOKEN_URL")
    API_BASE_URL = os.getenv("BRIGHT_BASE_URL")
else:
    TOKEN_URL = "https://brightmls-test.okta.com/oauth2/default/v1/token"
    API_BASE_URL = "https://bright-reso.tst.brightmls.com/RESO/OData/bright"

PAGE_SIZE = 200  # Number of properties to fetch per request

# --- Helper Functions ---

async def get_access_token(client: httpx.AsyncClient) -> str:
    """Authenticates with Bright MLS to get an access token."""
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    response = await client.post(TOKEN_URL, data=payload)
    response.raise_for_status()
    return response.json().get("access_token")

def clean_address(address: str) -> str:
    if not address: return ""
    parts = [p.strip() for p in address.split(',')]
    for part in parts:
        if re.match(r'^\d+', part):
            return part
    return parts[0]

def parse_dt(dt_str: str) -> datetime | None:
    if not dt_str: return None
    try:
        return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    except Exception:
        return None

def map_reso_to_internal(item: Dict[str, Any], photo_map: Dict[str, List[str]]) -> Dict[str, Any]:
    """
    Exhaustive mapping of RESO fields to our internal snake_case schema.
    """
    listing_key = str(item.get("ListingKey", ""))
    
    # HomeType Mapping
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

    # Bath calculation
    baths_full = item.get("BathroomsFull") or 0
    baths_half = item.get("BathroomsHalf") or 0
    bathrooms = item.get("BathroomsTotalInteger") or (float(baths_full) + (float(baths_half) * 0.5))

    # Photo processing
    fetched_photos = photo_map.get(listing_key, [])
    if not fetched_photos and item.get("ListPictureURL"):
        fetched_photos = [item.get("ListPictureURL")]
    
    # Township logic: IncorporatedCityName (cleaner) -> MLSAreaMajor (fallback)
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

    return {
        "listing_key": listing_key,
        "street_address": clean_address(item.get("FullStreetAddress") or item.get("UnparsedAddress")),
        "unparsed_address": item.get("UnparsedAddress"),
        "city": item.get("City"),
        "state": item.get("StateOrProvince"),
        "zipcode": item.get("PostalCode"),
        "price": item.get("ListPrice"),
        "bedrooms": item.get("BedroomsTotal"),
        "bathrooms": bathrooms,
        "living_area": item.get("LivingArea"),
        "lot_size": item.get("LotSizeSquareFeet"),
        "home_type": h_type.value,
        "home_status": item.get("MlsStatus"),
        "mls_property_type": m_prop,
        "mls_structure_design_type": m_design,
        "latitude": item.get("Latitude"),
        "longitude": item.get("Longitude"),
        "img_src": item.get("ListPictureURL"),
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
        "school_district_name": item.get("SchoolDistrictName"),
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
        "modification_timestamp": parse_dt(item.get("ModificationTimestamp"))
    }

async def fetch_media_for_keys(client: httpx.AsyncClient, headers: Dict[str, str], keys: List[str]) -> Dict[str, List[str]]:
    if not keys: return {}
    photo_map = {}
    chunk_size = 50
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i:i + chunk_size]
        media_params = {
            "$filter": f"ResourceRecordKey in ({','.join(chunk)}) and MediaCategory eq 'Photo'",
            "$select": "ResourceRecordKey,MediaURL",
            "$orderby": "MediaDisplayOrder asc"
        }
        try:
            res = await client.get(f"{API_BASE_URL}/BrightMedia", headers=headers, params=media_params)
            if res.status_code == 200:
                for m in res.json().get("value", []):
                    key = str(m["ResourceRecordKey"])
                    if key not in photo_map: photo_map[key] = []
                    photo_map[key].append(m["MediaURL"])
        except Exception as e:
            logger.warning(f"Media fetch failed for chunk: {e}")
    return photo_map

async def seed_properties():
    if not CLIENT_ID or not CLIENT_SECRET:
        logger.error("Missing Bright MLS credentials.")
        return

    async with httpx.AsyncClient(timeout=60.0) as client:
        logger.info("Authenticating...")
        token = await get_access_token(client)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        # Simple paging logic: no time-based filter.
        base_filter = "MlsStatus in ('ACTIVE-BRIGHT', 'COMING SOON-BRIGHT')"
        
        select_fields = ",".join([
            "ListingKey", "FullStreetAddress", "UnparsedAddress", "City", "StateOrProvince",
            "PostalCode", "ListPrice", "BedroomsTotal", "BathroomsFull",
            "BathroomsHalf", "BathroomsTotalInteger", "LivingArea", "LotSizeSquareFeet",
            "PropertyType", "StructureDesignType", "MlsStatus", 
            "Latitude", "Longitude", "ListPictureURL", "MLSAreaMajor", "IncorporatedCityName",
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
            "MLSListDate", "PriceChangeTimestamp", "ModificationTimestamp"
        ])

        skip = 0
        total_seeded = 0
        
        while True:
            logger.info(f"Seeding batch (skip={skip})...")
            # Ordering by ListingKey to ensure stable pagination
            params = {
                "$filter": base_filter,
                "$top": PAGE_SIZE,
                "$skip": skip,
                "$select": select_fields,
                "$orderby": "ListingKey asc"
            }
            
            try:
                response = await client.get(f"{API_BASE_URL}/BrightProperties", headers=headers, params=params)
                if response.status_code != 200:
                    logger.error(f"Error: {response.status_code} - {response.text}")
                    break
                
                items = response.json().get("value", [])
                if not items:
                    logger.info("Done seeding all properties.")
                    break

                listing_keys = [str(item["ListingKey"]) for item in items]
                photo_map = await fetch_media_for_keys(client, headers, listing_keys)

                async with AsyncSessionLocal() as db:
                    for item in items:
                        mapped_data = map_reso_to_internal(item, photo_map)
                        stmt = insert(Property).values(**mapped_data)
                        update_dict = {k: v for k, v in mapped_data.items() if k not in ['id', 'listing_key']}
                        stmt = stmt.on_conflict_do_update(index_elements=['listing_key'], set_=update_dict)
                        await db.execute(stmt)
                    await db.commit()
                
                total_seeded += len(items)
                logger.info(f"✓ Seeded {len(items)} (Total: {total_seeded})")
                
                if len(items) < PAGE_SIZE:
                    break
                skip += PAGE_SIZE

            except Exception as e:
                logger.exception(f"Seed failed: {e}")
                break

if __name__ == "__main__":
    logger.info("Starting One-Time Seed (All fields included, sorted by ListingKey)...")
    asyncio.run(seed_properties())
