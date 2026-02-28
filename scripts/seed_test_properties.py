import asyncio
import os
import sys
import time
import httpx
import logging
import re
import json
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import insert
from dotenv import load_dotenv

# Add the app directory to sys.path so we can import internal modules
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from app.database import AsyncSessionLocal
from app.models.database import Property, HomeType

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    """
    Robust address cleaner.
    Prioritizes the first part of the string that starts with a house number.
    Fallback to the first part if no number is found.
    """
    if not address: return ""
    parts = [p.strip() for p in address.split(',')]
    
    for part in parts:
        # Check if the part starts with a number (Standard US House Number)
        if re.match(r'^\d+', part):
            return part
            
    # Fallback: if no part starts with a number, return the first part
    return parts[0]

def map_reso_to_internal(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Exhaustive mapping of RESO fields to our internal snake_case schema.
    Matches all columns in our 'properties' table.
    """

    m_prop = item.get("PropertyType")
    m_design = item.get("StructureDesignType")

    h_type = HomeType.OTHER
    if m_prop == "Residential":
        if m_design == "Detached":
            h_type = HomeType.SINGLE_FAMILY
        elif m_design and ("Townhouse" in m_design or "Row" in m_design or "Twin" in m_design):
            h_type = HomeType.TOWNHOUSE
        elif m_design and ("Unit" in m_design or "Flat" in m_design or "Apartment" in m_design or "Penthouse" in m_design):
            h_type = HomeType.CONDO
        elif m_design and ("Manufactured" in m_design or "Mobile" in m_design):
            h_type = HomeType.SINGLE_FAMILY
        else:
            h_type = HomeType.SINGLE_FAMILY # Default for Residential
    elif m_prop == "Multi-Family":
        h_type = HomeType.MULTI_FAMILY
    elif m_prop == "Land":
        h_type = HomeType.LAND
    elif m_prop == "Farm":
        h_type = HomeType.FARM
    elif m_prop == "Residential Lease":
        h_type = HomeType.RESIDENTIAL_LEASE
    elif m_prop and ("Commercial" in m_prop or "Industrial" in m_prop):
        h_type = HomeType.COMMERCIAL
    elif m_prop == "Business Opportunity":
        h_type = HomeType.OTHER
    else:
        h_type = HomeType.OTHER

    # Bath calculation
    baths_full = item.get("BathroomsFull") or 0
    baths_half = item.get("BathroomsHalf") or 0
    bathrooms = item.get("BathroomsTotalInteger") or (float(baths_full) + (float(baths_half) * 0.5))

    # Photo extraction from batch-fetched media
    photo_urls = item.get("BrightMediaFetched", [])
    
    # If no photos in expanded media, fallback to the primary ListPictureURL
    if not photo_urls and item.get("ListPictureURL"):
        photo_urls = [item.get("ListPictureURL")]

    return {
        "listing_key": str(item.get("ListingKey", "")),
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
        "mls_property_type": m_prop,
        "mls_structure_design_type": m_design,
        
        "home_status": item.get("MlsStatus"),
        
        "latitude": item.get("Latitude"),
        "longitude": item.get("Longitude"),
        "img_src": item.get("ListPictureURL"),
        "description": item.get("PublicRemarks"),
        "photos": photo_urls, 
        
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
        "township": re.sub(r'\s*\(\d+\)$', '', item.get("MLSAreaMajor", "")) if item.get("MLSAreaMajor") else None,
        "directions": item.get("Directions"),
        "zoning": item.get("Zoning"),
        
        "tax_assessment_amount": item.get("TaxAssessmentAmount"),
        "assessment_year": item.get("AssessmentYear"),
        "possession": item.get("Possession"),
        
        "cooling_fuel": item.get("CoolingFuel"),
        "heating_fuel": item.get("HeatingFuel"),
        "lot_size_acres": item.get("LotSizeAcres"),
        "attached_garage_yn": item.get("AttachedGarageYN"),
        "new_construction_yn": item.get("NewConstructionYN"),
        "senior_community_yn": item.get("SeniorCommunityYN"),
        "pets_allowed": item.get("PetsAllowed"),
        
        "original_list_price": item.get("OriginalListPrice"),
        "days_on_market": item.get("DaysOnMarket"),
        "cumulative_days_on_market": item.get("CumulativeDaysOnMarket"),
        
        "subdivision_name": item.get("SubdivisionName"),
        "mls_list_date": datetime.fromisoformat(item.get("MLSListDate").replace('Z', '+00:00')) if item.get("MLSListDate") else None,
        "price_change_timestamp": datetime.fromisoformat(item.get("PriceChangeTimestamp").replace('Z', '+00:00')) if item.get("PriceChangeTimestamp") else None,
        "above_grade_finished_area": item.get("AboveGradeFinishedArea"),
        "below_grade_finished_area": item.get("BelowGradeFinishedArea"),
        "association_fee_2": item.get("AssociationFee2"),
        "association_fee_2_frequency": item.get("AssociationFee2Frequency"),
        "accessibility_features": item.get("AccessibilityFeatures"),
        "basement": item.get("Basement"),
        "has_basement": item.get("BasementYN"),
        "has_central_air": item.get("CentralAirYN"),
        "has_fireplace": item.get("FireplaceYN"),
        "listing_tax_id": item.get("ListingTaxID"),
        "list_agent_preferred_phone": item.get("ListAgentPreferredPhone"),
        
        "stories": item.get("Stories"),
        "modification_timestamp": datetime.fromisoformat(item.get("ModificationTimestamp").replace('Z', '+00:00')) if item.get("ModificationTimestamp") else None
    }

async def seed_real_properties():
    """Fetches a variety of properties (5 of each type) directly from Bright MLS."""
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Error: BRIGHT_MLS_CLIENT or BRIGHT_MLS_SECRET not set in .env")
        return

    # Define categories to fetch
    categories = [
        {"name": "Single Family", "filter": "PropertyType eq 'Residential' and StructureDesignType eq 'Detached'"},
        {"name": "Townhouse", "filter": "PropertyType eq 'Residential' and (contains(StructureDesignType, 'Townhouse') or contains(StructureDesignType, 'Row'))"},
        {"name": "Condo", "filter": "PropertyType eq 'Residential' and (contains(StructureDesignType, 'Unit') or contains(StructureDesignType, 'Flat'))"},
        {"name": "Multi-Family", "filter": "PropertyType eq 'Multi-Family'"},
        {"name": "Land", "filter": "PropertyType eq 'Land'"},
        {"name": "Farm", "filter": "PropertyType eq 'Farm'"},
        {"name": "Rentals", "filter": "PropertyType eq 'Residential Lease'"},
        {"name": "Commercial", "filter": "PropertyType eq 'Commercial Sale'"},
    ]

    async with httpx.AsyncClient(timeout=30.0) as client:
        print("--- Authenticating with Bright MLS ---")
        token = await get_access_token(client)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        select_fields = ",".join([
            "ListingKey", "FullStreetAddress", "UnparsedAddress", "City", "StateOrProvince",
            "PostalCode", "ListPrice", "BedroomsTotal", "BathroomsFull",
            "BathroomsHalf", "BathroomsTotalInteger", "LivingArea", "LotSizeSquareFeet",
            "PropertyType", "StructureDesignType", "MlsStatus", 
            "Latitude", "Longitude", "ListPictureURL", "MLSAreaMajor",
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
            "Zoning", "TaxAssessmentAmount", 
            "AssessmentYear", "Possession", "CoolingFuel", "HeatingFuel", "LotSizeAcres", "AttachedGarageYN",
            "NewConstructionYN", "SeniorCommunityYN", "PetsAllowed", "OriginalListPrice",
            "DaysOnMarket", "CumulativeDaysOnMarket", "Stories",
            "ModificationTimestamp", "SubdivisionName", "MLSListDate",
            "PriceChangeTimestamp", "AboveGradeFinishedArea", "BelowGradeFinishedArea",
            "AssociationFee2", "AssociationFee2Frequency", "AccessibilityFeatures",
            "BasementYN", "Basement", "CentralAirYN", "FireplaceYN", "ListingTaxID",
            "ListAgentPreferredPhone"
        ])

        all_raw_properties = []

        for cat in categories:
            print(f"--- Fetching 5 properties for: {cat['name']} ---")
            # Base filter for active properties
            base_filter = "MlsStatus in ('ACTIVE-BRIGHT', 'COMING SOON-BRIGHT')"
            full_filter = f"{base_filter} and {cat['filter']}"
            
            params = {
                "$filter": full_filter,
                "$top": 5,
                "$select": select_fields
            }
            url = f"{API_BASE_URL}/BrightProperties"
            
            try:
                response = await client.get(url, headers=headers, params=params)
                if response.status_code == 200:
                    props = response.json().get("value", [])
                    print(f"  Found {len(props)} properties.")
                    all_raw_properties.extend(props)
                else:
                    print(f"  Error fetching {cat['name']}: {response.status_code}")
            except Exception as e:
                print(f"  Exception fetching {cat['name']}: {e}")

        if not all_raw_properties:
            print("No properties found across all categories.")
            return

        print(f"Total properties retrieved: {len(all_raw_properties)}")

        # --- Batch Fetch Photos ---
        listing_keys = [str(p["ListingKey"]) for p in all_raw_properties]
        photo_map = {}
        
        # OData might have limits on the length of 'in' lists, so we chunk it if necessary
        # But for 40 properties (8 categories * 5), it should be fine.
        if listing_keys:
            print(f"--- Fetching photos for {len(listing_keys)} properties ---")
            media_params = {
                "$filter": f"ResourceRecordKey in ({','.join(listing_keys)}) and MediaCategory eq 'Photo'",
                "$select": "ResourceRecordKey,MediaURL",
                "$orderby": "MediaDisplayOrder asc"
            }
            media_url = f"{API_BASE_URL}/BrightMedia"
            try:
                media_res = await client.get(media_url, headers=headers, params=media_params)
                if media_res.status_code == 200:
                    media_data = media_res.json().get("value", [])
                    for m in media_data:
                        key = str(m["ResourceRecordKey"])
                        if key not in photo_map:
                            photo_map[key] = []
                        photo_map[key].append(m["MediaURL"])
                else:
                    print(f"Warning: Could not fetch photos ({media_res.status_code})")
            except Exception as e:
                print(f"Warning: Exception fetching photos: {e}")

        # Save raw data for debugging
        with open("mls_output.json", "w") as f:
            json.dump(all_raw_properties, f, indent=4)
        print("✓ Saved raw data to mls_output.json")

        async with AsyncSessionLocal() as db:
            for item in all_raw_properties:
                key = str(item["ListingKey"])
                # Inject fetched photos into the item so map_reso_to_internal can find them
                item["BrightMediaFetched"] = photo_map.get(key, [])
                data = map_reso_to_internal(item)
                
                # PostgreSQL Upsert (ON CONFLICT DO UPDATE)
                stmt = insert(Property).values(**data)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['listing_key'],
                    set_={k: v for k, v in data.items() if k != 'listing_key'}
                )
                
                try:
                    await db.execute(stmt)
                    print(f"✓ Upserted: {data['street_address']} ({data['listing_key']})")
                except Exception as e:
                    print(f"✗ Failed to upsert {data['listing_key']}: {e}")
                    await db.rollback()
                    continue
            
            await db.commit()
            print("--- Seeding Complete ---")

if __name__ == "__main__":
    asyncio.run(seed_real_properties())
