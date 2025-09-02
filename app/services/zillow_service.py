import httpx
import os
from typing import List, Optional, Dict, Any
import logging
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

from app.models.database import CollectionPreferences, Property, Collection, OpenHouseEvent
from app.schemas.collection_preferences import CollectionPreferences as CollectionPreferencesSchema
from datetime import datetime
from app.models.property import PropertyDetailResponse, PropertySaveResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ZillowService:
    def __init__(self):
        self.api_key = os.getenv("RAPID_API_KEY")
        self.base_url = "https://zillow56.p.rapidapi.com"
        
        if not self.api_key:
            logger.warning("RAPID_API_KEY not found in environment variables")
    
    async def search_properties_by_coordinates(
        self, 
        preferences: CollectionPreferencesSchema
    ) -> Dict[str, Any]:
        """
        Search properties using Zillow API with coordinate-based search
        """
        if not self.api_key:
            raise ValueError("Zillow API key not configured")
        
        if not preferences.lat or not preferences.long:
            raise ValueError("Latitude and longitude are required for coordinate search")
        
        headers = {
            'x-rapidapi-key': self.api_key,
            'x-rapidapi-host': "zillow56.p.rapidapi.com"
        }
        
        # Build query parameters based on preferences
        params = {
            'lat': preferences.lat,
            'long': preferences.long,
            'd': preferences.diameter,  # diameter in miles
            'status': 'forSale',
            'output': 'json',
            'sort': 'priorityscore',
            'listing_type': 'by_agent',
            'doz': 'any',
            'isTownhouse': preferences.is_town_house or False,
            'isLotLand': preferences.is_lot_land or False,
            'isCondo': preferences.is_condo or False,
            'isMultiFamily': preferences.is_multi_family or False,
            'isSingleFamily': preferences.is_single_family or False,
            'isApartment': preferences.is_apartment or False,
        }
        
        # Add price range
        if preferences.min_price:
            params['price_min'] = preferences.min_price
        if preferences.max_price:
            params['price_max'] = preferences.max_price

        # Add bed range
        if preferences.min_beds:
            params['beds_min'] = preferences.min_beds
        if preferences.max_beds > 0:
            params['beds_max'] = preferences.max_beds

        # Add bath range
        if preferences.min_baths:
            params['baths_min'] = int(preferences.min_baths)
        if preferences.max_baths > 0:
            params['baths_max'] = int(preferences.max_baths)

        url = f"{self.base_url}/search_coordinates"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers, params=params)
                
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Zillow API returned {len(data.get('results', []))} properties")
                    
                    return data
                elif response.status_code == 401:
                    raise ValueError("Invalid Zillow API key")
                elif response.status_code == 429:
                    raise ValueError("Zillow API rate limit exceeded")
                else:
                    logger.error(f"Zillow API error: {response.status_code} - {response.text}")
                    raise ValueError(f"Zillow API error: {response.status_code}")
                    
        except httpx.TimeoutException:
            raise ValueError("Zillow API request timed out")
        except httpx.RequestError as e:
            logger.error(f"Failed to connect to Zillow API: {str(e)}")
            raise ValueError(f"Failed to connect to Zillow API: {str(e)}")
    
    def parse_zillow_property(self, zillow_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse Zillow property data into our internal Property format
        """
        try:
            # Extract basic property information
            property_data = {
                'zpid': str(zillow_data.get('zpid', '')),
                'address': zillow_data.get('streetAddress', ''),
                'city': zillow_data.get('city', ''),
                'state': zillow_data.get('state', ''),
                'zipcode': zillow_data.get('zipcode', ''),
                'price': zillow_data.get('price') or zillow_data.get('priceForHDP'),
                'bedrooms': zillow_data.get('bedrooms'),
                'bathrooms': zillow_data.get('bathrooms'),
                'living_area': zillow_data.get('livingArea'),
                'lot_size': zillow_data.get('lotAreaValue'),
                'home_type': zillow_data.get('homeType', ''),
                'home_status': zillow_data.get('homeStatus', ''),
                'latitude': zillow_data.get('latitude'),
                'longitude': zillow_data.get('longitude'),
                'image_url': zillow_data.get('imgSrc', ''),
                'zestimate': zillow_data.get('zestimate'),
            }
            
            return property_data
            
        except Exception as e:
            logger.error(f"Error parsing Zillow property data: {str(e)}")
            return {}
    
    async def get_matching_properties(
        self, 
        preferences: CollectionPreferencesSchema
    ) -> List[Dict[str, Any]]:
        """
        Get matching properties from Zillow based on collection preferences
        """
        try:
            # Search properties using Zillow API
            zillow_response = await self.search_properties_by_coordinates(preferences)
            
            # Parse and return property data
            properties = []
            for zillow_property in zillow_response.get('results', []):
                parsed_property = self.parse_zillow_property(zillow_property)
                if parsed_property:  # Only add if parsing was successful
                    properties.append(parsed_property)
            
            return properties
            
        except Exception as e:
            logger.error(f"Error fetching matching properties: {str(e)}")
            raise e
    
    async def get_property_by_address(self, address: str) -> PropertyDetailResponse:
        """
        Get property details from Zillow API by address
        """
        if not self.api_key:
            raise HTTPException(status_code=500, detail="RapidAPI key not configured")
        
        headers = {
            'x-rapidapi-key': self.api_key,
            'x-rapidapi-host': "zillow56.p.rapidapi.com"
        }
        
        params = {
            "address": address
        }
        
        url = f"{self.base_url}/search_address"
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, params=params, timeout=30.0)
                
                if response.status_code == 200:
                    data = response.json()
                    return PropertyDetailResponse(**data)
                elif response.status_code == 401:
                    raise HTTPException(status_code=401, detail="Invalid RapidAPI key")
                elif response.status_code == 404:
                    raise HTTPException(status_code=404, detail="Property not found")
                elif response.status_code == 429:
                    raise HTTPException(status_code=429, detail="Rate limit exceeded")
                else:
                    raise HTTPException(status_code=response.status_code, detail=f"External API error: {response.text}")
                    
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Request to external API timed out")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Failed to connect to external API: {str(e)}")
        except ValueError as e:
            raise HTTPException(status_code=502, detail=f"Invalid response from external API: {str(e)}")
    
# Removed unused methods: create_property_and_open_house and create_property_and_open_house_from_data
    # These are no longer needed since open houses now store metadata directly instead of creating Property records
