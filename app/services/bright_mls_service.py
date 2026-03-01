import httpx
import os
import asyncio
import time
import math
import logging
import re
from typing import Dict, Any, List, Optional
from fastapi import HTTPException
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class BrightMlsService:
    """
    Lean Service for interacting with Bright MLS RESO Web API.
    Focused on high-speed data extraction and standardized mapping.
    """

    def __init__(self):
        # Configuration
        self.client_id = os.getenv("BRIGHT_MLS_CLIENT")
        self.client_secret = os.getenv("BRIGHT_MLS_SECRET")
        self.is_prod = os.getenv("BRIGHT_MLS_ENV", "test").lower() == "prod"
        
        if self.is_prod:
            self.token_url = os.getenv("BRIGHT_TOKEN_URL")
            self.api_base_url = os.getenv("BRIGHT_BASE_URL")
        else:
            # Test/SandBox URLs
            self.token_url = "https://brightmls-test.okta.com/oauth2/default/v1/token"
            self.api_base_url = "https://bright-reso.tst.brightmls.com/RESO/OData/bright"

        self._access_token = None
        self._token_expires_at = 0
        
        # Persistent client for connection pooling
        self.client = httpx.AsyncClient(timeout=60.0)

        if not self.client_id or not self.client_secret:
            logger.warning("BRIGHT_MLS_CLIENT or BRIGHT_MLS_SECRET not found in environment")

    async def close(self):
        """Close the underlying httpx client"""
        await self.client.aclose()

    async def _get_access_token(self) -> str:
        current_time = time.time()
        if self._access_token and current_time < (self._token_expires_at - 60):
            return self._access_token

        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            response = await self.client.post(self.token_url, data=payload)
            response.raise_for_status()
            data = response.json()
            
            self._access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            self._token_expires_at = current_time + expires_in
            
            return self._access_token
        except Exception as e:
            logger.error(f"Failed to authenticate with Bright MLS: {e}")
            raise HTTPException(status_code=500, detail="MLS Authentication Failed")

    async def _make_request(self, endpoint: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        endpoint = endpoint.lstrip('/')
        url = f"{self.api_base_url}/{endpoint}"

        try:
            response = await self.client.get(url, headers=headers, params=params)

            if response.status_code != 200:
                logger.error(f"MLS API Error: {response.status_code} - {response.text}")
                if response.status_code == 404:
                    return {"value": []}
                response.raise_for_status()

            return response.json()

        except Exception as e:
            logger.error(f"Request failed to {endpoint}: {e}")
            raise HTTPException(status_code=500, detail=f"MLS Request Failed: {str(e)}")

    def _get_full_field_list(self) -> str:
        """Centralized list of all 90+ fields we track for the Local Mirror."""
        return ",".join([
            "ListingKey", "FullStreetAddress", "UnparsedAddress", "City", "StateOrProvince",
            "PostalCode", "ListPrice", "BedroomsTotal", "BathroomsFull", "BathroomsHalf",
            "BathroomsTotalInteger", "LivingArea", "LotSizeSquareFeet", "PropertyType",
            "StructureDesignType", "MlsStatus", "Latitude", "Longitude", "ListPictureURL",
            "MLSAreaMajor", "IncorporatedCityName", "PublicRemarks", "ListAgentFullName", "ListAgentEmail",

            "ListOfficeName", "ListOfficePhone", "ArchitecturalStyle", "ConstructionMaterials",
            "Roof", "FoundationDetails", "Levels", "InteriorFeatures", "ExteriorFeatures",
            "Flooring", "Appliances", "FireplacesTotal", "FireplaceFeatures", "DoorFeatures",
            "WindowFeatures", "Cooling", "Heating", "WaterSource", "Sewer", "Utilities",
            "GarageSpaces", "ParkingFeatures", "GarageYN", "AssociationFee",
            "AssociationFeeFrequency", "AssociationAmenities", "AssociationFeeIncludes",
            "AssociationYN", "LotFeatures", "View", "WaterfrontFeatures", "ViewYN",
            "TaxAnnualAmount", "TaxYear", "YearBuilt", "ElementarySchool", "MiddleOrJuniorSchool",
            "HighSchool", "SchoolDistrictName", "County", "Directions", "Zoning",
            "TaxAssessmentAmount", "AssessmentYear", "Possession", "ListingTaxID",
            "CoolingFuel", "HeatingFuel", "AboveGradeFinishedArea", "BelowGradeFinishedArea",
            "Basement", "AccessibilityFeatures", "BasementYN", "CentralAirYN", "FireplaceYN",
            "AssociationFee2", "AssociationFee2Frequency", "ListAgentPreferredPhone",
            "LotSizeAcres", "AttachedGarageYN", "NewConstructionYN", "SeniorCommunityYN",
            "PetsAllowed", "OriginalListPrice", "DaysOnMarket", "CumulativeDaysOnMarket",
            "Stories", "SubdivisionName", "MLSListDate", "PriceChangeTimestamp", "ModificationTimestamp"
        ])

    def _clean_address(self, address: str) -> str:
        if not address: return ""
        parts = [p.strip() for p in address.split(',')]
        for part in parts:
            if re.match(r'^\d+', part): return part
        return parts[0]

    def map_reso_to_internal(self, item: Dict[str, Any], photo_map: Dict[str, List[str]] = None) -> Dict[str, Any]:
        """
        The Master Mapper. Converts RESO Web API fields to our internal snake_case database schema.
        Handles complex logic like HomeType detection and date parsing.
        """
        listing_key = str(item.get("ListingKey", ""))
        
        # 1. HomeType Mapping
        m_prop = item.get("PropertyType")
        m_design = item.get("StructureDesignType")
        home_type = "OTHER"
        if m_prop == "Residential":
            if m_design == "Detached": home_type = "SINGLE_FAMILY"
            elif m_design and any(x in m_design for x in ["Townhouse", "Row", "Twin"]): home_type = "TOWNHOUSE"
            elif m_design and any(x in m_design for x in ["Unit", "Flat", "Apartment", "Penthouse"]): home_type = "CONDO"
            else: home_type = "SINGLE_FAMILY"
        elif m_prop == "Multi-Family": home_type = "MULTI_FAMILY"
        elif m_prop == "Land": home_type = "LAND"
        elif m_prop == "Farm": home_type = "FARM"
        elif m_prop == "Residential Lease": home_type = "RESIDENTIAL_LEASE"
        elif m_prop and ("Commercial" in m_prop or "Industrial" in m_prop): home_type = "COMMERCIAL"

        # 2. Bath calculation
        baths_full = item.get("BathroomsFull") or 0
        baths_half = item.get("BathroomsHalf") or 0
        bathrooms = item.get("BathroomsTotalInteger") or (float(baths_full) + (float(baths_half) * 0.5))

        # 3. Photo processing (Simplified list of strings)
        fetched_photos = []
        if photo_map and listing_key in photo_map:
            fetched_photos = photo_map[listing_key]
        elif item.get("ListPictureURL"):
            fetched_photos = [item.get("ListPictureURL")]

        # 4. Township Logic: IncorporatedCityName (cleaner) -> MLSAreaMajor (fallback)
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

        # 5. School District
        school_district = item.get("SchoolDistrictName")
        if school_district:
            school_district = school_district.strip().upper()

        # 6. Date Parsing
        def parse_dt(dt_str: str):
            if not dt_str: return None
            try:
                return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
            except:
                return None

        return {
            "listing_key": listing_key,
            "street_address": self._clean_address(item.get("FullStreetAddress") or item.get("UnparsedAddress")),
            "unparsed_address": item.get("UnparsedAddress"),
            "city": item.get("City"),
            "state": item.get("StateOrProvince"),
            "zipcode": item.get("PostalCode"),
            "price": item.get("ListPrice"),
            "bedrooms": item.get("BedroomsTotal"),
            "bathrooms": bathrooms,
            "living_area": item.get("LivingArea"),
            "lot_size": item.get("LotSizeSquareFeet"),
            "home_type": home_type,
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
            "modification_timestamp": parse_dt(item.get("ModificationTimestamp"))
        }

    async def get_properties_modified_since(self, since_timestamp: str, top: int = 200, skip: int = 0) -> List[Dict[str, Any]]:
        """Fetch properties modified since a specific timestamp."""
        params = {
            "$filter": f"MlsStatus in ('ACTIVE-BRIGHT', 'COMING SOON-BRIGHT') and ModificationTimestamp gt {since_timestamp}",
            "$top": top,
            "$skip": skip,
            "$select": self._get_full_field_list(),
            "$orderby": "ModificationTimestamp asc"
        }
        data = await self._make_request("BrightProperties", params=params)
        return data.get("value", [])

    async def get_media_for_properties(self, listing_keys: List[str]) -> Dict[str, List[str]]:
        """Batch fetch photos for multiple properties."""
        if not listing_keys: return {}
        photo_map = {}
        chunk_size = 50
        for i in range(0, len(listing_keys), chunk_size):
            chunk = listing_keys[i:i + chunk_size]
            params = {
                "$filter": f"ResourceRecordKey in ({','.join(chunk)}) and MediaCategory eq 'Photo'",
                "$select": "ResourceRecordKey,MediaURL",
                "$orderby": "MediaDisplayOrder asc"
            }
            try:
                data = await self._make_request("BrightMedia", params=params)
                for m in data.get("value", []):
                    key = str(m["ResourceRecordKey"])
                    if key not in photo_map: photo_map[key] = []
                    photo_map[key].append(m["MediaURL"])
            except Exception as e:
                logger.warning(f"Failed to fetch media chunk: {e}")
        return photo_map

    async def get_property_by_id(self, listing_key: str) -> Optional[Dict[str, Any]]:
        """Fetch a single property by ListingKey with full details."""
        params = {
            "$filter": f"ListingKey eq '{listing_key}'", 
            "$top": 1,
            "$select": self._get_full_field_list()
        }
        data = await self._make_request("BrightProperties", params=params)
        if not data.get("value"): return None
        item = data["value"][0]
        photo_map = await self.get_media_for_properties([listing_key])
        return self.map_reso_to_internal(item, photo_map)

    async def get_property_by_address(self, address: str) -> Optional[Dict[str, Any]]:
        """Search by address and return full internal map (Fallback)."""
        parts = [p.strip() for p in address.split(',')]
        street_part = parts[0]
        zip_code = None
        for part in reversed(parts):
            zip_match = re.search(r'\b\d{5}\b', part)
            if zip_match: zip_code = zip_match.group(0); break
        
        street_match = re.match(r'^(\d+)\s+(.*)$', street_part)
        if street_match:
            number, full_name = street_match.group(1), street_match.group(2).strip()
            first_word = full_name.split(' ')[0]
            cond = [f"StreetNumber eq '{number}'"]
            if first_word: cond.append(f"contains(StreetName, '{first_word}')")
            if zip_code: cond.append(f"PostalCode eq '{zip_code}'")
            params = {"$filter": " and ".join(cond), "$top": 1, "$select": self._get_full_field_list()}
            data = await self._make_request("BrightProperties", params=params)
            if data.get("value"):
                item = data["value"][0]
                photo_map = await self.get_media_for_properties([item["ListingKey"]])
                return self.map_reso_to_internal(item, photo_map)

        fallback_filter = f"contains(UnparsedAddress, '{street_part}')"
        if zip_code: fallback_filter += f" and PostalCode eq '{zip_code}'"
        params = {"$filter": fallback_filter, "$top": 1, "$select": self._get_full_field_list()}
        data = await self._make_request("BrightProperties", params=params)
        if data.get("value"):
            item = data["value"][0]
            photo_map = await self.get_media_for_properties([item["ListingKey"]])
            return self.map_reso_to_internal(item, photo_map)

        return None

    async def bright_mls_id_exists(self, mls_id: str) -> bool:
        """Validates if a Bright MLS ID or Listing Key exists via API."""
        params = {
            "$filter": f"ListingId eq '{mls_id}' or ListingKey eq '{mls_id}'",
            "$top": 1,
            "$select": "ListingKey"
        }
        data = await self._make_request("BrightProperties", params=params)
        return len(data.get("value", [])) > 0

bright_mls_service = BrightMlsService()
