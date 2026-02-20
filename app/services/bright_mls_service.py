import httpx
import os
import asyncio
import time
import math
import logging
import re
from typing import Dict, Any, List, Optional
from fastapi import HTTPException
from app.schemas.collection_preferences import CollectionPreferencesBase as CollectionPreferencesSchema
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class BrightMlsService:
    """
    Service for interacting with Bright MLS RESO Web API.
    Optimized for performance and standardized for frontend/database compatibility.
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
            self.token_url = "https://brightmls-test.okta.com/oauth2/default/v1/token"
            self.api_base_url = "https://bright-reso.tst.brightmls.com/RESO/OData/bright"

        self._access_token = None
        self._token_expires_at = 0
        
        # Persistent client for connection pooling
        self.client = httpx.AsyncClient(timeout=30.0)

        if not self.client_id or not self.client_secret:
            logger.warning("BRIGHT_MLS_CLIENT_ID or BRIGHT_MLS_SECRET not found in environment")

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

    def _select_fields_for_properties(self) -> str:
        """
        Standard fields to retrieve for property LISTS (shallow view).
        Optimized to reduce JSON size during search.
        """
        return ",".join([
            "ListingKey", "ListingId", "ListPrice", "UnparsedAddress", "FullStreetAddress", "City", 
            "StateOrProvince", "PostalCode", "BedroomsTotal", "BathroomsTotalInteger", 
            "BathroomsFull", "BathroomsHalf", "LivingArea", "LotSizeSquareFeet", 
            "YearBuilt", "MlsStatus", "PropertyType", "ListPictureURL", 
            "Latitude", "Longitude", "DaysOnMarket", "ListOfficeName", "ListAgentFullName",
            "PublicRemarks"
        ])

    def _map_to_app_model(self, item: Dict[str, Any]) -> Dict[str, Any]:
        raw_status = item.get("MlsStatus", "").upper()
        home_status = "FOR_SALE"
        if "CLOSED" in raw_status or "SOLD" in raw_status:
            home_status = "SOLD"
        elif "PENDING" in raw_status or "UNDER CONTRACT" in raw_status:
            home_status = "PENDING"

        prop_type = item.get("PropertyType", "")
        struct_type = item.get("StructureType", "")
        home_type = "SINGLE_FAMILY"
        
        if any(x in struct_type or x in prop_type for x in ["Townhouse", "Town Home"]):
            home_type = "TOWNHOUSE"
        elif any(x in struct_type or x in prop_type for x in ["Condo", "Condominium", "Unit"]):
            home_type = "CONDO"
        elif "Multi-Family" in prop_type:
            home_type = "MULTI_FAMILY"
        elif any(x in prop_type for x in ["Land", "Farm", "Acreage"]):
            home_type = "LOT_LAND"

        baths_full = item.get("BathroomsFull") or 0
        baths_half = item.get("BathroomsHalf") or 0
        bathrooms = item.get("BathroomsTotalInteger") or (baths_full + (baths_half * 0.5))

        return {
            "listing_key": str(item.get("ListingKey", "")),
            "mls_id": item.get("ListingId"),
            "address": item.get("UnparsedAddress") or item.get("FullStreetAddress"),
            "city": item.get("City"),
            "state": item.get("StateOrProvince"),
            "zipcode": item.get("PostalCode"),
            "price": item.get("ListPrice"),
            "bedrooms": item.get("BedroomsTotal"),
            "bathrooms": bathrooms,
            "living_area": item.get("LivingArea"),
            "lot_size": item.get("LotSizeSquareFeet"),
            "home_type": home_type,
            "home_status": home_status,
            "year_built": item.get("YearBuilt"),
            "image_url": item.get("ListPictureURL"),
            "latitude": item.get("Latitude"),
            "longitude": item.get("Longitude"),
            "days_on_market": item.get("DaysOnMarket"),
            "list_office_name": item.get("ListOfficeName"),
            "list_agent_full_name": item.get("ListAgentFullName")
        }

    def _map_to_full_details(self, item: Dict[str, Any], images: List[str] = []) -> Dict[str, Any]:
        """
        Maps a raw MLS item to a clean, flat dictionary structure.
        Ensures NO nested 'details' blob exists here to prevent data recursion.
        """
        base_info = self._map_to_app_model(item)
        
        formatted_photos = []
        for img_url in images:
            formatted_photos.append({
                "caption": "",
                "url": img_url,
                "mixedSources": {"jpeg": [{"url": img_url, "width": 0}], "webp": []}
            })

        reso_facts = {
            "description": item.get("PublicRemarks", ""),
            "standard_status": item.get("MlsStatus"),
            "home_status": base_info["home_status"],
            "interior_features": item.get("InteriorFeatures", []),
            "flooring": item.get("Flooring", []),
            "appliances": item.get("Appliances", []),
            "fireplaces": item.get("FireplacesTotal"),
            "levels": item.get("Levels", []),
            "architectural_style": item.get("ArchitecturalStyle", []),
            "construction_materials": item.get("ConstructionMaterials", []),
            "roof_type": item.get("Roof", []),
            "structure_type": item.get("StructureType", []),
            "cooling": item.get("Cooling", []),
            "heating": item.get("Heating", []),
            "water_source": item.get("WaterSource", []),
            "sewer": item.get("Sewer", []),
            "garage_spaces": item.get("GarageSpaces"),
            "parking_features": item.get("ParkingFeatures", []),
            "has_garage": item.get("GarageYN"),
            "association_fee": item.get("AssociationFee"),
            "association_fee_frequency": item.get("AssociationFeeFrequency"),
            "association_amenities": item.get("AssociationAmenities", []),
            "has_association": item.get("AssociationYN"),
            "school_district_name": item.get("SchoolDistrictName"),
            "elementary_school": item.get("ElementarySchool"),
            "high_school": item.get("HighSchool"),
            "county": item.get("County"),
            "zoning": item.get("Zoning"),
            "living_area": base_info["living_area"],
            "year_built": base_info["year_built"],
            "tax_annual_amount": item.get("TaxAnnualAmount"),
            "price_per_square_foot": round(base_info["price"] / base_info["living_area"], 2) if base_info.get("price") and base_info.get("living_area") else None,
            "days_on_market": base_info["days_on_market"],
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        return {
            **base_info,
            "description": reso_facts["description"],
            "list_agent_email": item.get("ListAgentEmail"),
            "list_office_phone": item.get("ListOfficePhone"),
            "photos": formatted_photos,
            "original_photos": formatted_photos,
            "reso_facts": reso_facts,
            "abbreviated_address": base_info["address"]
        }

    async def _fetch_all_media(self, listing_key: str) -> List[str]:
        params = {
            "$filter": f"ResourceRecordKey eq {listing_key} and MediaCategory eq 'Photo'",
            "$orderby": "MediaDisplayOrder",
            "$select": "MediaURL"
        }
        try:
            data = await self._make_request("BrightMedia", params=params)
            return [i.get("MediaURL") for i in data.get("value", []) if i.get("MediaURL")]
        except:
            return []

    def _build_filter_from_preferences(self, preferences: CollectionPreferencesSchema) -> str:
        filters = []
        combined_locations = (preferences.cities or []) + (preferences.townships or [])
        if combined_locations:
            state_map = {}
            for loc in combined_locations:
                parts = [s.strip() for s in loc.split(',')]
                city = parts[0]
                state = parts[1] if len(parts) >= 2 else "PA"
                if state not in state_map: state_map[state] = []
                state_map[state].append(f"'{city}'")
            
            loc_filters = []
            for state, cities in state_map.items():
                if len(cities) > 1: loc_filters.append(f"(City in ({','.join(cities)}) and StateOrProvince eq '{state}')")
                else: loc_filters.append(f"(City eq {cities[0]} and StateOrProvince eq '{state}')")
            
            if len(loc_filters) > 1: filters.append(f"({' or '.join(loc_filters)})")
            else: filters.append(loc_filters[0].strip('()'))
                
        elif preferences.lat and preferences.long and preferences.diameter:
            lat_offset = float(preferences.diameter) / 69.0
            cos_lat = math.cos(math.radians(float(preferences.lat)))
            long_offset = float(preferences.diameter) / (69.0 * cos_lat) if abs(cos_lat) > 0.0001 else lat_offset
            filters.append(f"Latitude ge {float(preferences.lat) - lat_offset} and Latitude le {float(preferences.lat) + lat_offset}")
            filters.append(f"Longitude ge {float(preferences.long) - long_offset} and Longitude le {float(preferences.long) + long_offset}")

        filters.append("MlsStatus eq 'ACTIVE-BRIGHT'")
        if preferences.min_price: filters.append(f"ListPrice ge {preferences.min_price}")
        if preferences.max_price: filters.append(f"ListPrice le {preferences.max_price}")
        if preferences.min_beds: filters.append(f"BedroomsTotal ge {preferences.min_beds}")
        if preferences.max_beds: filters.append(f"BedroomsTotal le {preferences.max_beds}")
        if preferences.min_baths: filters.append(f"BathroomsTotalInteger ge {int(preferences.min_baths)}")
        if preferences.max_baths: filters.append(f"BathroomsTotalInteger le {int(preferences.max_baths)}")
        if preferences.min_year_built: filters.append(f"YearBuilt ge {preferences.min_year_built}")

        type_options = []
        if preferences.is_single_family or preferences.is_town_house or preferences.is_condo: type_options.append("'Residential'")
        if preferences.is_multi_family: type_options.append("'Multi-Family'")
        if preferences.is_apartment: type_options.append("'Residential Lease'")
        if preferences.is_lot_land: 
            type_options.append("'Land'")
            type_options.append("'Farm'")
        
        if type_options:
            unique_types = sorted(list(set(type_options)))
            if len(unique_types) > 1: filters.append(f"PropertyType in ({','.join(unique_types)})")
            else: filters.append(f"PropertyType eq {unique_types[0]}")

        return " and ".join(filters)

    async def get_property_by_id(self, listing_key: str) -> Optional[Dict[str, Any]]:
        # NO $select here - Fetch FULL object
        params = {"$filter": f"ListingKey eq {listing_key}", "$top": 1}
        data = await self._make_request("BrightProperties", params=params)
        if not data.get("value"): return None
        item = data["value"][0]
        images = await self._fetch_all_media(listing_key)
        return self._map_to_full_details(item, images)

    async def get_property_by_address(self, address: str) -> Dict[str, Any]:
        parts = [p.strip() for p in address.split(',')]
        street_part = parts[0]
        zip_code = None
        for part in reversed(parts):
            zip_match = re.search(r'\b\d{5}\b', part)
            if zip_match: zip_code = zip_match.group(0); break
        
        # Try Strategy 1: Precise Match (No $select)
        street_match = re.match(r'^(\d+)\s+(.*)$', street_part)
        if street_match:
            number, full_name = street_match.group(1), street_match.group(2).strip()
            first_word = full_name.split(' ')[0]
            cond = [f"StreetNumber eq '{number}'"]
            if first_word: cond.append(f"contains(StreetName, '{first_word}')")
            if zip_code: cond.append(f"PostalCode eq '{zip_code}'")
            filter_str = " and ".join(cond)
            data = await self._make_request("BrightProperties", params={"$filter": filter_str, "$top": 1})
            if data.get("value"):
                item = data["value"][0]
                return self._map_to_full_details(item, await self._fetch_all_media(item["ListingKey"]))

        # Fallback (No $select)
        fallback_filter = f"contains(UnparsedAddress, '{street_part}')"
        if zip_code: fallback_filter += f" and PostalCode eq '{zip_code}'"
        data = await self._make_request("BrightProperties", params={"$filter": fallback_filter, "$top": 1})
        if data.get("value"):
            item = data["value"][0]
            return self._map_to_full_details(item, await self._fetch_all_media(item["ListingKey"]))

        raise HTTPException(status_code=404, detail="Address not found in MLS records.")

    async def get_properties_by_keys(self, listing_keys: List[str]) -> List[Dict[str, Any]]:
        if not listing_keys: return []
        odata_filter = f"ListingKey in ({','.join([str(k) for k in listing_keys])})"
        # Lists use $select for performance
        data = await self._make_request("BrightProperties", params={"$filter": odata_filter, "$select": self._select_fields_for_properties()})
        return [self._map_to_app_model(item) for item in data.get("value", [])]

    async def get_properties_by_preferences(self, preferences: CollectionPreferencesSchema, max_properties: int = 50) -> List[Dict[str, Any]]:
        odata_filter = self._build_filter_from_preferences(preferences)
        # Searches use $select for performance
        params = {"$filter": odata_filter, "$top": max_properties, "$select": self._select_fields_for_properties(), "$orderby": "ListPrice desc"}
        data = await self._make_request("BrightProperties", params=params)
        return [self._map_to_app_model(item) for item in data.get("value", [])]

    async def run_diagnostic_tests(self):
        try:
            await self._get_access_token()
            await self.get_property_by_address("300 Valley Pl, Radnor, PA 19087")
        except Exception as e:
            logger.error(f"❌ Diagnostic failed: {str(e)}")

if __name__ == "__main__":
    async def main():
        service = BrightMlsService()
        try: await service.run_diagnostic_tests()
        finally: await service.close()
    asyncio.run(main())
