import httpx
import os
import urllib.parse
from typing import List, Optional, Dict, Any
import asyncio
from fastapi import HTTPException
from datetime import datetime

from app.schemas.collection_preferences import CollectionPreferences as CollectionPreferencesSchema
from app.models.property import PropertyDetailResponse, ZillowPropertyDetailResponse
from app.config.logging import get_logger

# Get logger from centralized config
logger = get_logger(__name__)

class BrightMlsService:
    """
    Service for interacting with Bright MLS RESO Web API.
    Replaces ZillowWorkingService while maintaining compatibility with existing data structures.
    """

    def __init__(self):
        # Configuration
        self.client_id = os.getenv("BRIGHT_MLS_CLIENT")
        self.client_secret = os.getenv("BRIGHT_MLS_SECRET")
        
        # Determine environment (Prod vs Test) - defaulting to Test for safety
        self.is_prod = os.getenv("BRIGHT_MLS_ENV", "test").lower() == "prod"
        
        if self.is_prod:
            self.token_url = "https://brightmls.okta.com/oauth2/default/v1/token"
            self.api_base_url = "https://bright-reso.brightmls.com/RESO/OData/bright"
        else:
            self.token_url = "https://brightmls-test.okta.com/oauth2/default/v1/token"
            self.api_base_url = "https://bright-reso.tst.brightmls.com/RESO/OData/bright"

        self._access_token = None
        self._token_expires_at = 0

        if not self.client_id or not self.client_secret:
            logger.warning("BRIGHT_MLS_CLIENT_ID or BRIGHT_MLS_SECRET not found in environment")

    async def _get_access_token(self) -> str:
        """
        Retrieves a valid access token, refreshing if necessary.
        """
        import time
        current_time = time.time()
        
        # Reuse valid token if we have one (with 60s buffer)
        if self._access_token and current_time < (self._token_expires_at - 60):
            return self._access_token

        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self.token_url, data=payload)
                response.raise_for_status()
                data = response.json()
                
                self._access_token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)
                self._token_expires_at = current_time + expires_in
                
                return self._access_token
        except Exception as e:
            logger.error(f"Failed to authenticate with Bright MLS: {e}")
            raise HTTPException(status_code=500, detail="MLS Authentication Failed")

    def _build_odata_filter(self, preferences: CollectionPreferencesSchema, city_state_filters: List[str] = None) -> str:
        """
        Constructs an OData $filter string based on user preferences.
        """
        filters = []

        # 1. Location (City/State) - Handled separately or passed in
        if city_state_filters:
            location_conditions = []
            for loc in city_state_filters:
                parts = [p.strip() for p in loc.split(',')]
                if len(parts) >= 2:
                    city = parts[0]
                    state = parts[1]
                    location_conditions.append(f"(City eq '{city}' and StateOrProvince eq '{state}')")
                else:
                    location_conditions.append(f"City eq '{parts[0]}'")
            
            if location_conditions:
                filters.append(f"({' or '.join(location_conditions)})")

        # 2. Status (Active, etc.)
        # Use 'ACTIVE-BRIGHT' as verified by debug script
        status_filter = "MlsStatus eq 'ACTIVE-BRIGHT'" 
        filters.append(status_filter)

        # 3. Price
        if preferences.min_price:
            filters.append(f"ListPrice ge {preferences.min_price}")
        if preferences.max_price:
            filters.append(f"ListPrice le {preferences.max_price}")

        # 4. Beds/Baths
        if preferences.min_beds:
            filters.append(f"BedroomsTotal ge {preferences.min_beds}")
        if preferences.max_beds:
            filters.append(f"BedroomsTotal le {preferences.max_beds}")
            
        if preferences.min_baths:
            filters.append(f"BathroomsTotalInteger ge {int(preferences.min_baths)}")
        if preferences.max_baths:
            filters.append(f"BathroomsTotalInteger le {int(preferences.max_baths)}")

        # 5. Year Built
        if preferences.min_year_built:
            filters.append(f"YearBuilt ge {preferences.min_year_built}")
        if preferences.max_year_built:
            filters.append(f"YearBuilt le {preferences.max_year_built}")

        # 6. Property Type
        type_filters = []
        if preferences.is_single_family or preferences.is_town_house or preferences.is_condo:
            type_filters.append("PropertyType eq 'Residential'")
        
        if preferences.is_multi_family:
            type_filters.append("PropertyType eq 'Multi-Family'")
            
        if preferences.is_apartment:
            # Apartments could be Multi-Family or Residential Lease
            type_filters.append("PropertyType eq 'Residential Lease'")
            
        if preferences.is_lot_land:
            type_filters.append("PropertyType eq 'Land'")
            type_filters.append("PropertyType eq 'Farm'")
        
        if type_filters:
            # Join with OR and deduplicate
            filters.append(f"({' or '.join(sorted(list(set(type_filters))))})")

        return " and ".join(filters)

    def _map_bright_to_app_model(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Maps a Bright MLS 'BrightProperties' item to the application's internal dictionary format.
        """
        listing_key = item.get("ListingKey", "")
        listing_id = item.get("ListingId", "")
        
        # Address
        address = item.get("UnparsedAddress", "")
        city = item.get("City", "")
        state = item.get("StateOrProvince", "")
        zipcode = item.get("PostalCode", "")
        
        # Price
        price = item.get("ListPrice")
        
        # Details
        bedrooms = item.get("BedroomsTotal")
        
        # Safely calculate bathrooms
        baths_full = item.get("BathroomsFull") or 0
        baths_half = item.get("BathroomsHalf") or 0
        bathrooms = item.get("BathroomsTotalInteger") or (baths_full + (baths_half * 0.5))
        
        living_area = item.get("LivingArea")
        lot_size = item.get("LotSizeSquareFeet")
        year_built = item.get("YearBuilt")
        
        # Status Mapping
        status_raw = item.get("MlsStatus", "")
        home_status = "FOR_SALE"
        if "CLOSED" in status_raw.upper():
            home_status = "SOLD"
        elif "PENDING" in status_raw.upper():
            home_status = "PENDING"
            
        # Type Mapping
        prop_type_raw = item.get("PropertyType", "")
        struct_type_raw = item.get("StructureType", "")
        home_type = "SINGLE_FAMILY"
        if "Townhouse" in struct_type_raw:
            home_type = "TOWNHOUSE"
        elif "Unit" in struct_type_raw or "Condo" in prop_type_raw:
            home_type = "CONDO"
            
        # Use ListPictureURL if available (faster than fetching from Media)
        image_url = item.get("ListPictureURL")
        
        return {
            'listing_key': listing_key,
            'mlsId': listing_id,
            'address': address,
            'city': city,
            'state': state,
            'zipcode': zipcode,
            'price': price,
            'bedrooms': bedrooms,
            'bathrooms': bathrooms,
            'living_area': living_area,
            'lot_size': lot_size,
            'home_type': home_type,
            'home_status': home_status,
            'latitude': item.get("Latitude"),
            'longitude': item.get("Longitude"),
            'image_url': image_url,
            'yearBuilt': year_built,
            'listOfficeName': item.get("ListOfficeName"),
            'listOfficePhone': item.get("ListOfficePhone"),
            'listAgentFullName': item.get("ListAgentFullName"),
            'listAgentEmail': item.get("ListAgentEmail")
        }

    def _map_property_details(self, item: Dict[str, Any], images: List[str] = None) -> Dict[str, Any]:
        """
        Maps Bright MLS fields to the PropertyDetails table structure.
        """
        # Format photos for storage
        photos = []
        if images:
            for img_url in images:
                photos.append({
                    "caption": "",
                    "url": img_url,
                    "mixedSources": {
                        "jpeg": [{"url": img_url, "width": 0}],
                        "webp": []
                    }
                })

        return {
            # Narrative & Media
            "description": item.get("PublicRemarks", ""),
            "photos": photos,
            
            # Listing Agent & Office
            "list_agent_full_name": item.get("ListAgentFullName"),
            "list_agent_email": item.get("ListAgentEmail"),
            "list_office_name": item.get("ListOfficeName"),
            "list_office_phone": item.get("ListOfficePhone"),

            # Core Structural & Exterior
            "architectural_style": item.get("ArchitecturalStyle"),
            "construction_materials": item.get("ConstructionMaterials"),
            "roof_type": item.get("Roof"),
            "foundation_details": item.get("FoundationDetails"),
            "structure_type": item.get("StructureType"),
            "levels": item.get("Levels"),
            
            # Interior & Features
            "interior_features": item.get("InteriorFeatures"),
            "exterior_features": item.get("ExteriorFeatures"),
            "flooring": item.get("Flooring"),
            "appliances": item.get("Appliances"),
            "fireplaces": item.get("FireplacesTotal"),
            "fireplace_features": item.get("FireplaceFeatures"),
            "door_features": item.get("DoorFeatures"),
            "window_features": item.get("WindowFeatures"),
            
            # Utilities & Systems
            "cooling": item.get("Cooling"),
            "heating": item.get("Heating"),
            "water_source": item.get("WaterSource"),
            "sewer": item.get("Sewer"),
            "electric": item.get("Electric"),
            "utilities": item.get("Utilities"),
            
            # Parking
            "garage_spaces": item.get("GarageSpaces"),
            "parking_features": item.get("ParkingFeatures"),
            "has_garage": item.get("GarageYN"),
            
            # Community & HOA
            "association_fee": item.get("AssociationFee"),
            "association_fee_frequency": item.get("AssociationFeeFrequency"),
            "association_amenities": item.get("AssociationAmenities"),
            "association_fee_includes": item.get("AssociationFeeIncludes"),
            "has_association": item.get("AssociationYN"),
            
            # Lot & Location
            "lot_features": item.get("LotFeatures"),
            "topography": item.get("Topography"),
            "view": item.get("View"),
            "waterfront_features": item.get("WaterfrontFeatures"),
            "has_waterfront_view": item.get("WaterfrontYN"),
            "has_view": item.get("ViewYN"),
            
            # Tax & Financial
            "tax_annual_amount": item.get("TaxAnnualAmount"),
            "tax_year": item.get("TaxYear"),
            
            # Dates
            "year_built": item.get("YearBuilt"),
            "modification_timestamp": item.get("ModificationTimestamp")
        }

    async def _fetch_property_images(self, listing_key: str, limit: int = 1) -> List[str]:
        """
        Fetches image URLs for a given listing key from BrightMedia.
        """
        if not listing_key:
            return []
            
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        # Query BrightMedia linked to the listing
        # ResourceRecordKey is the standard foreign key to the Property table
        url = f"{self.api_base_url}/BrightMedia"
        params = {
            "$filter": f"ResourceRecordKey eq '{listing_key}' and MediaCategory eq 'Photo'",
            "$orderby": "Order",
            "$top": limit,
            "$select": "MediaURL"
        }
        
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url, headers=headers, params=params)
                if response.status_code == 200:
                    data = response.json()
                    return [item.get("MediaURL") for item in data.get("value", []) if item.get("MediaURL")]
        except Exception as e:
            logger.warning(f"Failed to fetch images for listing {listing_key}: {e}")
        
        return []

    async def get_matching_properties_by_locations(
        self,
        preferences: CollectionPreferencesSchema,
        max_properties: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Search properties in Bright MLS based on cities/townships.
        """
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        # Combine locations
        locations = []
        if preferences.cities:
            locations.extend(preferences.cities)
        if preferences.townships:
            locations.extend(preferences.townships)
            
        if not locations and self.is_prod:
            return []
            
        # Build Filter
        odata_filter = self._build_odata_filter(preferences, city_state_filters=locations)
        
        # Select specific fields to reduce payload
        select_fields = [
            "ListingKey", "ListingId", "ListPrice", "UnparsedAddress", "City", 
            "StateOrProvince", "PostalCode", "BedroomsTotal", "BathroomsTotalInteger", 
            "BathroomsFull", "BathroomsHalf", "LivingArea", "LotSizeSquareFeet", 
            "YearBuilt", "MlsStatus", "PropertyType", "ListPictureURL", 
            #"ListOfficeName", "ListOfficePhone", "ListAgentFullName", 
            "Latitude", "Longitude"
        ]
        
        url = f"{self.api_base_url}/BrightProperties"
        
        if not self.is_prod:
            logger.info("BrightMlsService: Running in TEST mode. Returning top 1 property.")
            params = {
                "$select": ",".join(select_fields),
                "$top": 1
            }
        else:
            params = {
                "$filter": odata_filter,
                "$select": ",".join(select_fields),
                "$top": max_properties if max_properties else 20,
                "$orderby": "ListPrice desc" 
            }
        
        try:
            async with httpx.AsyncClient() as client:
                # Rate limiting removed
                response = await client.get(url, headers=headers, params=params)
                response.raise_for_status()
                data = response.json()
                
                results = []
                # Fetch images concurrently for better performance (only if missing ListPictureURL)
                tasks = []
                mapped_items = []
                
                for item in data.get("value", []):
                    mapped = self._map_bright_to_app_model(item)
                    mapped_items.append(mapped)
                    
                    if not mapped.get('image_url'):
                        # Create a task to fetch image if we don't have one
                        tasks.append(self._fetch_property_images(mapped['listing_key'], limit=1))
                    else:
                        tasks.append(None) # Placeholder to keep index alignment
                
                # Wait for all image fetches to complete
                if any(tasks):
                    # Filter out Nones before gather to avoid errors, or handle results carefully
                    real_tasks = [t for t in tasks if t]
                    if real_tasks:
                        image_results = await asyncio.gather(*real_tasks)
                        
                        # Re-distribute results
                        result_idx = 0
                        for i, mapped in enumerate(mapped_items):
                            if tasks[i]: # If we had a task
                                images = image_results[result_idx]
                                if images:
                                    mapped['image_url'] = images[0]
                                result_idx += 1
                
                results = mapped_items
                
                return results
                
        except Exception as e:
            logger.error(f"Error searching Bright MLS: {e}")
            return []

    async def get_property_by_address(self, address: str, details: bool = False):
        """
        Get property details by address.
        """
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        url = f"{self.api_base_url}/BrightProperties"
        
        if not self.is_prod:
            logger.info(f"BrightMlsService: Running in TEST mode. Ignoring address '{address}' and returning top 1.")
            params = {
                "$top": 1
            }
        else:
            # Construct filter
            # Note: Ideally we parse the address into Number, Street, etc. for better accuracy
            # For now, we try UnparsedAddress
            filter_str = f"UnparsedAddress eq '{address}'"
            params = {
                "$filter": filter_str,
                "$top": 1
            }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, params=params)
                
                if response.status_code == 200:
                    data = response.json()
                    items = data.get("value", [])
                    if not items:
                        raise HTTPException(status_code=404, detail="Property not found")
                    
                    item = items[0]
                    mapped_data = self._map_bright_to_app_model(item)
                    
                    # Fetch images
                    images = await self._fetch_property_images(mapped_data['listing_key'], limit=10)
                    primary_photo = images[0] if images else None
                    
                    # Format photos for ZillowPropertyDetailResponse

                    original_photos = []

                    # TODO
                    image_url = item.get("ListPictureURL") # removes this line in prod
                    images.append(image_url) # remove this line in prod

                    # Map to PropertyDetails structure
                    details_data = self._map_property_details(item, images)
                    
                    # Merge with basic property data for immediate use
                    response_data = {
                        'listing_key': mapped_data['listing_key'],
                        'abbreviatedAddress': mapped_data['address'],
                        'address': {
                            'streetAddress': mapped_data['address'],
                            'city': mapped_data['city'],
                            'state': mapped_data['state'],
                            'zipcode': mapped_data['zipcode']
                        },
                        'price': mapped_data['price'],
                        'bedrooms': mapped_data['bedrooms'],
                        'bathrooms': mapped_data['bathrooms'],
                        'livingArea': mapped_data['living_area'],
                        'yearBuilt': mapped_data['yearBuilt'],
                        'homeStatus': mapped_data['home_status'],
                        'homeType': mapped_data['home_type'],
                        'latitude': mapped_data['latitude'],
                        'longitude': mapped_data['longitude'],
                        'details': details_data # Include full details
                    }

                    return response_data
                else:
                    raise HTTPException(status_code=response.status_code, detail="Provider Error")
        except Exception as e:
            logger.error(f"Error fetching property details: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    async def get_matching_properties(self, preferences: CollectionPreferencesSchema, max_properties: Optional[int] = None) -> List[Dict[str, Any]]:
        return await self.get_matching_properties_by_locations(preferences, max_properties=max_properties)
