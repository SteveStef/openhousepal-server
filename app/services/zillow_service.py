import httpx
import os
from typing import List, Optional, Dict, Any
import asyncio
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

from app.models.database import CollectionPreferences, Property, Collection, OpenHouseEvent
from app.schemas.collection_preferences import CollectionPreferences as CollectionPreferencesSchema
from datetime import datetime
from app.models.property import PropertyDetailResponse, PropertySaveResponse, ZillowPropertyDetailResponse
from app.utils.rate_limiter import RateLimiter
from app.config.logging import get_logger

# Get logger from centralized config
logger = get_logger(__name__)

class ZillowService:
    # Maximum properties to collect for location-based searches
    MAX_LOCATION_SEARCH_PROPERTIES = 41

    def __init__(self):
        self.api_key = os.getenv("RAPID_API_KEY")
        self.base_url = "https://zillow56.p.rapidapi.com"
        self.rate_limiter = RateLimiter()

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
        if preferences.max_beds and preferences.max_beds > 0:
            params['beds_max'] = preferences.max_beds

        # Add bath range
        if preferences.min_baths:
            params['baths_min'] = int(preferences.min_baths)
        if preferences.max_baths and preferences.max_baths > 0:
            params['baths_max'] = int(preferences.max_baths)

        # Add year built range
        if preferences.min_year_built:
            params['built_min'] = preferences.min_year_built
        if preferences.max_year_built and preferences.max_year_built > 0:
            params['built_max'] = preferences.max_year_built

        if preferences.special_features:
            params["keywords"] = preferences.special_features

        url = f"{self.base_url}/search_coordinates"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await self.rate_limiter.acquire_token()
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
                    # Enhanced error logging with full details
                    logger.error(f"Zillow API error: {response.status_code}")
                    logger.error(f"Response text: {response.text}")
                    logger.error(f"Request URL: {url}")
                    logger.error(f"Request params: {params}")
                    logger.error(f"Response headers: {dict(response.headers)}")

                    # Try to parse JSON error response
                    try:
                        error_json = response.json()
                        logger.error(f"Response JSON: {error_json}")
                    except:
                        pass

                    raise ValueError(f"Zillow API error: {response.status_code}")
                    
        except httpx.TimeoutException:
            raise ValueError("Zillow API request timed out")
        except httpx.RequestError as e:
            logger.error("Failed to connect to Zillow API", exc_info=True)
            raise ValueError(f"Failed to connect to Zillow API: {str(e)}")
    
    async def search_properties_by_location(
        self, 
        location: str,
        preferences: CollectionPreferencesSchema
    ) -> Dict[str, Any]:
        """
        Search properties using Zillow API with location-based search
        """
        if not self.api_key:
            raise ValueError("Zillow API key not configured")
        
        if not location or not location.strip():
            raise ValueError("Location is required for location search")
        
        headers = {
            'x-rapidapi-key': self.api_key,
            'x-rapidapi-host': "zillow56.p.rapidapi.com"
        }
        
        # Build query parameters based on preferences (same as coordinates search, minus lat/long/diameter)
        params = {
            'location': location.strip(),
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
        if preferences.max_beds and preferences.max_beds > 0:
            params['beds_max'] = preferences.max_beds

        # Add bath range
        if preferences.min_baths:
            params['baths_min'] = int(preferences.min_baths)
        if preferences.max_baths and preferences.max_baths > 0:
            params['baths_max'] = int(preferences.max_baths)

        # Add year built range
        if preferences.min_year_built:
            params['built_min'] = preferences.min_year_built
        if preferences.max_year_built and preferences.max_year_built > 0:
            params['built_max'] = preferences.max_year_built

        if preferences.special_features:
            params["keywords"] = preferences.special_features

        url = f"{self.base_url}/search"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await self.rate_limiter.acquire_token()
                response = await client.get(url, headers=headers, params=params)
                
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Zillow API returned {len(data.get('results', []))} properties for location: {location}")
                    
                    return data
                elif response.status_code == 401:
                    raise ValueError("Invalid Zillow API key")
                elif response.status_code == 429:
                    raise ValueError("Zillow API rate limit exceeded")
                else:
                    # Enhanced error logging with full details
                    logger.error(f"Zillow API error for location {location}: {response.status_code}")
                    logger.error(f"Response text: {response.text}")
                    logger.error(f"Request URL: {url}")
                    logger.error(f"Request params: {params}")
                    logger.error(f"Response headers: {dict(response.headers)}")

                    # Try to parse JSON error response
                    try:
                        error_json = response.json()
                        logger.error(f"Response JSON: {error_json}")
                    except:
                        pass

                    raise ValueError(f"Zillow API error: {response.status_code}")
                    
        except httpx.TimeoutException:
            raise ValueError(f"Zillow API request timed out for location: {location}")
        except httpx.RequestError as e:
            logger.error(f"Failed to connect to Zillow API for location {location}", exc_info=True, extra={"location": location})
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
            logger.error("Error parsing Zillow property data", exc_info=True)
            return {}
    
    async def get_matching_properties_by_locations(
        self,
        preferences: CollectionPreferencesSchema,
        max_properties: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get matching properties from Zillow based on cities and townships in preferences.
        Makes separate API calls for each location with rate limiting and retry logic.
        """
        all_properties = []
        seen_zpids = set()  # Track zpids to avoid duplicates

        # Use provided limit or default to class constant
        property_limit = max_properties if max_properties is not None else self.MAX_LOCATION_SEARCH_PROPERTIES
        
        # Combine cities and townships into single list
        locations = []
        if preferences.cities:
            locations.extend(preferences.cities)
        if preferences.townships:
            locations.extend(preferences.townships)
        
        if not locations:
            logger.info("No cities or townships specified for location search")
            return []
        
        logger.info(f"Starting location search for {len(locations)} locations: {locations}")
        
        for i, location in enumerate(locations):
            try:
                logger.info(f"Searching location {i+1}/{len(locations)}: {location}")
                
                # First attempt
                try:
                    zillow_response = await self.search_properties_by_location(location, preferences)
                except Exception as e:
                    logger.warning(f"First attempt failed for location {location}: {str(e)}")
                    
                    # Second attempt (retry once)
                    try:
                        logger.info(f"Retrying location {location}")
                        await asyncio.sleep(1)  # Brief pause before retry
                        zillow_response = await self.search_properties_by_location(location, preferences)
                    except Exception as retry_error:
                        logger.error(f"Second attempt also failed for location {location}", exc_info=True, extra={"location": location})
                        # Skip this location and continue with next
                        continue
                
                # Parse properties from this location
                location_properties = []
                results = zillow_response.get('results', [])
                
                for zillow_property in results:
                    try:
                        parsed_property = self.parse_zillow_property(zillow_property)
                        if parsed_property and parsed_property.get('zpid'):
                            zpid = parsed_property['zpid']

                            # Check for duplicates
                            if zpid not in seen_zpids:
                                seen_zpids.add(zpid)
                                location_properties.append(parsed_property)

                                # Check if we've reached the property limit
                                if len(all_properties) + len(location_properties) >= property_limit:
                                    logger.info(
                                        f"Reached property limit of {property_limit}. "
                                        f"Stopping search at location {i+1}/{len(locations)} ({location})"
                                    )
                                    break  # Stop processing this location's results
                            else:
                                logger.debug(f"Skipping duplicate property with zpid: {zpid}")

                    except Exception as parse_error:
                        logger.warning(f"Failed to parse property from location {location}: {str(parse_error)}")
                        continue
                
                all_properties.extend(location_properties)
                logger.info(f"Location {location}: {len(location_properties)} properties added ({len(all_properties)} total so far)")

                # If we've reached or exceeded the limit, stop searching more locations
                if len(all_properties) >= property_limit:
                    logger.info(f"Property limit of {property_limit} reached. Skipping remaining {len(locations) - i - 1} locations.")
                    break  # Stop processing remaining locations

                # Rate limiting: 1 second between requests (except for the last one)
                if i < len(locations) - 1:
                    await asyncio.sleep(1)
                    
            except Exception as location_error:
                logger.error(f"Unexpected error processing location {location}", exc_info=True, extra={"location": location})
                continue
        
        logger.info(
            f"Location search completed. Found {len(all_properties)} properties from {len(locations)} locations "
            f"(limit: {property_limit})"
        )
        logger.info(f"Deduplication: {len(seen_zpids)} unique properties after removing duplicates")

        # Safety: Ensure we don't return more than the limit
        if len(all_properties) > property_limit:
            logger.warning(f"Truncating results from {len(all_properties)} to {property_limit}")
            all_properties = all_properties[:property_limit]

        return all_properties
    
    async def get_matching_properties(
        self, 
        preferences: CollectionPreferencesSchema
    ) -> List[Dict[str, Any]]:
        """
        Get matching properties from Zillow based on collection preferences.
        
        Supports two search methods:
        1. Address-based search: Uses coordinates (lat/long) and diameter
        2. Location-based search: Uses cities and/or townships lists
        """
        try:
            # Determine which search method to use based on available data
            if preferences.lat and preferences.long:
                # Use coordinate-based search (existing method)
                logger.info("Using coordinate-based search (address + diameter)")
                zillow_response = await self.search_properties_by_coordinates(preferences)
                
                properties = []
                results = zillow_response.get('results', [])
                
                for i, zillow_property in enumerate(results):
                    try:
                        parsed_property = self.parse_zillow_property(zillow_property)
                        if parsed_property:  # Only add if parsing was successful
                            properties.append(parsed_property)
                    except Exception as parse_error:
                        logger.warning(f"Failed to parse property {i+1}/{len(results)}: {str(parse_error)}")
                        continue  # Skip this property but continue with others
                
                logger.info(f"Coordinate search: Successfully parsed {len(properties)}/{len(results)} properties")
                return properties
                
            elif (preferences.cities and len(preferences.cities) > 0) or (preferences.townships and len(preferences.townships) > 0):
                # Use location-based search (new method)
                logger.info("Using location-based search (cities + townships)")
                return await self.get_matching_properties_by_locations(preferences)
                
            else:
                # No search criteria provided
                logger.warning("No search criteria provided - neither coordinates nor cities/townships")
                return []
            
        except Exception as e:
            logger.error("Error fetching matching properties", exc_info=True)
            # Instead of raising, return empty list to allow collection creation to succeed
            logger.warning("Returning empty property list to allow collection creation to proceed")
            return []
    
    async def get_property_by_address(self, address: str, details: bool = False) -> PropertyDetailResponse:
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
                await self.rate_limiter.acquire_token()
                response = await client.get(url, headers=headers, params=params, timeout=30.0)
                
                if response.status_code == 200:
                    data = response.json()
                    if not details:
                        return PropertyDetailResponse(**data)
                    else:
                        return ZillowPropertyDetailResponse(**data)
                elif response.status_code == 401:
                    raise HTTPException(status_code=401, detail="Invalid RapidAPI key")
                elif response.status_code == 404:
                    raise HTTPException(status_code=404, detail="Property not found")
                elif response.status_code == 429:
                    raise HTTPException(status_code=429, detail="Rate limit exceeded")
                else:
                    # Enhanced error logging with full details
                    logger.error(f"Zillow API error for address {address}: {response.status_code}")
                    logger.error(f"Response text: {response.text}")
                    logger.error(f"Request URL: {url}")
                    logger.error(f"Request params: {params}")
                    logger.error(f"Response headers: {dict(response.headers)}")

                    # Try to parse JSON error response
                    try:
                        error_json = response.json()
                        logger.error(f"Response JSON: {error_json}")
                    except:
                        pass

                    raise HTTPException(status_code=response.status_code, detail=f"External API error: {response.text}")
                    
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Request to external API timed out")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Failed to connect to external API: {str(e)}")
        except ValueError as e:
            raise HTTPException(status_code=502, detail=f"Invalid response from external API: {str(e)}")
