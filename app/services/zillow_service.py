import httpx
import os
from typing import List, Optional, Dict, Any
import logging
from datetime import datetime

from app.models.database import CollectionPreferences, Property, Collection
from app.schemas.collection_preferences import CollectionPreferences as CollectionPreferencesSchema

# Setup logging
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
            'doz': 'any'
        }
        
        # Add price range
        if preferences.min_price:
            params['price_min'] = preferences.min_price
        if preferences.max_price:
            params['price_max'] = preferences.max_price
        
        # Add bed range
        if preferences.min_beds:
            params['beds_min'] = preferences.min_beds
        if preferences.max_beds:
            params['beds_max'] = preferences.max_beds
        
        # Add bath range
        if preferences.min_baths:
            params['baths_min'] = int(preferences.min_baths)
        if preferences.max_baths:
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
                'year_built': zillow_data.get('yearBuilt'),
                'days_on_market': zillow_data.get('daysOnZillow', -1),
                'image_url': zillow_data.get('imgSrc', ''),
                'zestimate': zillow_data.get('zestimate'),
                'rent_zestimate': zillow_data.get('rentZestimate'),
                'tax_assessed_value': zillow_data.get('taxAssessedValue'),
                'is_featured': zillow_data.get('isFeatured', False),
                'is_premier_builder': zillow_data.get('isPremierBuilder', False),
                'last_updated': datetime.now().isoformat()
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
