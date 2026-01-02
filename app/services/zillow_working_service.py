import httpx
import os
from typing import List, Optional, Dict, Any
import asyncio
from fastapi import HTTPException

from app.schemas.collection_preferences import CollectionPreferences as CollectionPreferencesSchema
from app.models.property import PropertyDetailResponse, ZillowPropertyDetailResponse
from app.utils.rate_limiter import RateLimiter
from app.config.logging import get_logger

# Get logger from centralized config
logger = get_logger(__name__)

class ZillowWorkingService:
    """
    Zillow service using zllw-working-api.p.rapidapi.com API.
    Returns data in the same format as ZillowService for database compatibility.
    """

    def __init__(self):
        self.api_key = os.getenv("RAPID_API_KEY")
        self.base_url = "https://zllw-working-api.p.rapidapi.com"
        self.rate_limiter = RateLimiter()

        if not self.api_key:
            logger.warning("RAPID_API_KEY not found in environment variables")

    def _extract_image_url(self, media: Dict[str, Any]) -> Optional[str]:
        """
        Extract image URL from media object.
        Prioritizes high resolution for better quality.
        Returns None if no image available (instead of empty string).
        """
        try:
            photo_links = media.get('propertyPhotoLinks', {})

            # Prefer high resolution for better quality
            high_res_link = photo_links.get('highResolutionLink')
            if high_res_link:
                return high_res_link

            # Fallback to medium resolution
            medium_link = photo_links.get('mediumSizeLink')
            if medium_link:
                return medium_link

            return None  # Return None instead of empty string
        except Exception as e:
            logger.warning(f"Error extracting image URL: {str(e)}")
            return None

    def _transform_photos(self, original_photos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Transform originalPhotos array from new API to match old API format.
        Extracts highest resolution images from mixedSources.
        """
        transformed_photos = []

        try:
            for photo in original_photos:
                if not isinstance(photo, dict):
                    continue

                caption = photo.get('caption', '')
                mixed_sources = photo.get('mixedSources', {})

                # Get JPEG sources (prefer over WebP for compatibility)
                jpeg_sources = mixed_sources.get('jpeg', [])

                # Build mixedSources object with proper structure
                photo_obj = {
                    'caption': caption,
                    'mixedSources': {
                        'jpeg': jpeg_sources if jpeg_sources else [],
                        'webp': mixed_sources.get('webp', [])
                    }
                }

                transformed_photos.append(photo_obj)

        except Exception as e:
            logger.warning(f"Error transforming photos: {str(e)}")

        return transformed_photos

    def _normalize_property_type(self, api_type: str) -> str:
        """
        Normalize property type from new API format to old API format.
        """
        if not api_type:
            return ''

        # Mapping from new API values to old API format
        type_mapping = {
            'condo': 'CONDO',
            'house': 'SINGLE_FAMILY',
            'apartment': 'APARTMENT',
            'townhouse': 'TOWNHOUSE',
            'multi-family': 'MULTI_FAMILY',
            'lot': 'LOT',
            'manufactured': 'MANUFACTURED',
        }

        # Normalize to lowercase for lookup
        normalized = api_type.lower()
        return type_mapping.get(normalized, api_type.upper())

    def _normalize_listing_status(self, api_status: str) -> str:
        """
        Normalize listing status from new API format to old API format.
        """
        if not api_status:
            return ''

        # Mapping from new API values to old API format
        status_mapping = {
            'forSale': 'FOR_SALE',
            'forsale': 'FOR_SALE',
            'forRent': 'FOR_RENT',
            'forrent': 'FOR_RENT',
            'sold': 'SOLD',
        }

        return status_mapping.get(api_status, api_status.upper())

    def _build_home_types(self, preferences: CollectionPreferencesSchema) -> str:
        """
        Build comma-separated home types string from preferences.
        """
        home_types = []

        if preferences.is_single_family:
            home_types.append('Houses')
        if preferences.is_town_house:
            home_types.append('Townhomes')
        if preferences.is_multi_family:
            home_types.append('Multi-family')
        if preferences.is_condo:
            home_types.append('Condos/Co-ops')
        if preferences.is_lot_land:
            home_types.append('Lots-Land')
        if preferences.is_apartment:
            home_types.append('Apartments')

        # If no specific types selected, default to all
        if not home_types:
            return 'Houses, Townhomes, Multi-family, Condos/Co-ops, Lots-Land, Apartments, Manufactured'

        return ', '.join(home_types)

    def _format_bathrooms(self, min_baths: Optional[float]) -> str:
        """
        Convert minimum bathrooms to API enum format.
        """
        if not min_baths or min_baths <= 0:
            return 'Any'

        if min_baths >= 4:
            return '4+'
        elif min_baths >= 3:
            return '3+'
        elif min_baths >= 2:
            return '2+'
        elif min_baths >= 1.5:
            return '1.5+'
        elif min_baths >= 1:
            return '1+'

        return 'Any'

    def parse_zillow_property(self, zillow_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse property data from new Zillow API response to standard format.
        Returns dict compatible with existing database schema.
        """
        try:
            # New API wraps data in 'property' object
            property_data = zillow_data.get('property', zillow_data)

            # Extract nested address
            address_obj = property_data.get('address', {})

            # Extract nested location
            location_obj = property_data.get('location', {})

            # Extract nested price
            price_obj = property_data.get('price', {})
            price_value = price_obj.get('value') if isinstance(price_obj, dict) else price_obj

            # Extract nested lot size
            lot_size_obj = property_data.get('lotSizeWithUnit', {})
            lot_size_value = lot_size_obj.get('lotSize') if isinstance(lot_size_obj, dict) else None

            # Extract nested estimates
            estimates_obj = property_data.get('estimates', {})
            zestimate_value = estimates_obj.get('zestimate')

            # Extract nested listing
            listing_obj = property_data.get('listing', {})
            listing_status = listing_obj.get('listingStatus', '')

            # Build standardized property dict
            parsed_property = {
                'zpid': str(property_data.get('zpid', '')),
                'address': address_obj.get('streetAddress', ''),
                'city': address_obj.get('city', ''),
                'state': address_obj.get('state', ''),
                'zipcode': address_obj.get('zipcode', ''),
                'price': price_value,
                'bedrooms': property_data.get('bedrooms'),
                'bathrooms': property_data.get('bathrooms'),
                'living_area': property_data.get('livingArea'),
                'lot_size': lot_size_value,
                'home_type': self._normalize_property_type(property_data.get('propertyType', '')),
                'home_status': self._normalize_listing_status(listing_status),
                'latitude': location_obj.get('latitude'),
                'longitude': location_obj.get('longitude'),
                'image_url': self._extract_image_url(property_data.get('media', {})),
                'zestimate': zestimate_value,
            }

            return parsed_property

        except Exception as e:
            logger.error(f"Error parsing Zillow property data: {str(e)}", exc_info=True)
            return {}

    def _get_dummy_search_results(self) -> Dict[str, Any]:
        """
        Returns dummy search results for testing.
        """
        return {
            'searchResults': [
                {
                    'zpid': '1234567890',
                    'property': {
                        'zpid': '1234567890',
                        'address': {
                            'streetAddress': '123 Dummy St',
                            'city': 'Test City',
                            'state': 'TS',
                            'zipcode': '12345'
                        },
                        'location': {
                            'latitude': 34.0522,
                            'longitude': -118.2437
                        },
                        'price': {'value': 500000},
                        'lotSizeWithUnit': {'lotSize': 5000},
                        'estimates': {'zestimate': 510000},
                        'listing': {'listingStatus': 'FOR_SALE'},
                        'bedrooms': 3,
                        'bathrooms': 2,
                        'livingArea': 2000,
                        'propertyType': 'SINGLE_FAMILY',
                        'media': {'propertyPhotoLinks': {'highResolutionLink': 'https://img.freepik.com/free-vector/charming-house-with-tree-illustration_1308-176337.jpg?semt=ais_hybrid&w=740&q=80'}}
                    }
                },
                {
                    'zpid': '0987654321',
                    'property': {
                        'zpid': '0987654321',
                        'address': {
                            'streetAddress': '456 Mock Ave',
                            'city': 'Mocktown',
                            'state': 'TS',
                            'zipcode': '67890'
                        },
                        'location': {
                            'latitude': 34.0622,
                            'longitude': -118.2537
                        },
                        'price': {'value': 750000},
                        'lotSizeWithUnit': {'lotSize': 7000},
                        'estimates': {'zestimate': 760000},
                        'listing': {'listingStatus': 'FOR_SALE'},
                        'bedrooms': 4,
                        'bathrooms': 3,
                        'livingArea': 3000,
                        'propertyType': 'SINGLE_FAMILY',
                        'media': {'propertyPhotoLinks': {'highResolutionLink': 'https://img.freepik.com/free-vector/charming-house-with-tree-illustration_1308-176337.jpg?semt=ais_hybrid&w=740&q=80'}}
                    }
                }
            ]
        }

    async def search_properties_by_location(
        self,
        location: str,
        preferences: CollectionPreferencesSchema
    ) -> Dict[str, Any]:
        """
        Search properties using new Zillow API with location-based search.
        RETURNS DUMMY DATA.
        """
        logger.info(f"Returning dummy data for location search: {location}")
        return self._get_dummy_search_results()

    async def search_properties_by_coordinates(
        self,
        preferences: CollectionPreferencesSchema
    ) -> Dict[str, Any]:
        """
        Search properties using new Zillow API with coordinate-based search.
        RETURNS DUMMY DATA.
        """
        logger.info("Returning dummy data for coordinate search")
        return self._get_dummy_search_results()

    async def get_matching_properties(
        self,
        preferences: CollectionPreferencesSchema
    ) -> List[Dict[str, Any]]:
        """
        Get matching properties based on preferences.
        Automatically chooses between coordinate or location-based search.
        """

        if preferences.lat and preferences.long:
            logger.info("Using coordinate-based search")
            zillow_response = await self.search_properties_by_coordinates(preferences)
        # Use location search if cities or townships available
        elif preferences.cities or preferences.townships:
            logger.info("Using location-based batch search")
            # For dummy data, we can just call the batch search which uses the dummy location search
            return await self.get_matching_properties_by_locations(preferences)
        else:
            logger.warning("No coordinates or locations specified in preferences")
            return []

        # Parse results from searchResults array
        search_results = zillow_response.get('searchResults', [])
        properties = []

        for result in search_results:
            parsed_property = self.parse_zillow_property(result)
            if parsed_property:  # Only add if parsing succeeded
                properties.append(parsed_property)

        logger.info(f"Found {len(properties)} properties")
        return properties

    async def get_matching_properties_by_locations(
        self,
        preferences: CollectionPreferencesSchema,
        max_properties: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get matching properties from Zillow based on cities and townships in preferences.
        Uses the new API's multi-location feature (up to 5 locations separated by semicolons).
        """
        # Since we are returning dummy data, we can just make one call to get the dummy list
        # We don't need to actually loop through batches if we just want to return the same test data
        
        logger.info("Returning dummy data for location batch search")
        zillow_response = await self.search_properties_by_location("dummy", preferences)
        
        search_results = zillow_response.get('searchResults', [])
        all_properties = []
        
        for result in search_results:
            parsed_property = self.parse_zillow_property(result)
            if parsed_property:
                all_properties.append(parsed_property)
                
        return all_properties

    async def get_property_by_address(self, address: str, details: bool = False):
        """
        Get property details from new Zillow API by address.
        Returns PropertyDetailResponse or ZillowPropertyDetailResponse to match old API format.
        RETURNS DUMMY DATA.
        """
        logger.info(f"Returning dummy data for property address: {address}")
        
        dummy_data = {
            'propertyDetails': {
                'zpid': '1234567890',
                'address': {
                    'streetAddress': '123 Dummy St',
                    'city': 'Test City',
                    'state': 'TS',
                    'zipcode': '12345',
                    'neighborhood': 'Dummy Hood',
                    'community': 'Dummy Comm',
                    'subdivision': 'Dummy Sub'
                },
                'bedrooms': 3,
                'bathrooms': 2,
                'homeStatus': 'FOR_SALE',
                'homeType': 'SINGLE_FAMILY',
                'livingArea': 2000,
                'lotSize': 5000,
                'price': 500000,
                'zestimate': 510000,
                'yearBuilt': 2000,
                'latitude': 34.0522,
                'longitude': -118.2437,
                'daysOnZillow': 5,
                'description': 'This is a lovely dummy home with great features.',
                'newConstructionType': None,
                'resoFacts': {
                    'heating': ['Forced air'],
                    'cooling': ['Central']
                },
                'taxHistory': [],
                'priceHistory': [],
                'originalPhotos': [
                    {
                        'caption': 'Front View',
                        'mixedSources': {
                            'jpeg': [{'url': 'https://img.freepik.com/free-vector/charming-house-with-tree-illustration_1308-176337.jpg?semt=ais_hybrid&w=740&q=80', 'width': 1024}]
                        }
                    }
                ]
            }
        }

        # Transform new API response to match old API structure
        transformed_data = self._transform_property_details(dummy_data, details)

        if not details:
            return PropertyDetailResponse(**transformed_data)
        else:
            return ZillowPropertyDetailResponse(**transformed_data)

    def _transform_property_details(self, api_response: Dict[str, Any], include_details: bool) -> Dict[str, Any]:
        """
        Transform new API response structure to match old API Pydantic models.
        """
        try:
            prop = api_response.get('propertyDetails', {})

            # Build address object
            addr = prop.get('address', {})
            address_obj = {
                'streetAddress': addr.get('streetAddress'),
                'city': addr.get('city'),
                'state': addr.get('state'),
                'zipcode': addr.get('zipcode'),
                'neighborhood': addr.get('neighborhood'),
                'community': addr.get('community'),
                'subdivision': addr.get('subdivision'),
            }

            # Base fields for PropertyDetailResponse
            transformed = {
                'zpid': prop.get('zpid'),
                'abbreviatedAddress': addr.get('streetAddress'),
                'address': address_obj,
                'bedrooms': prop.get('bedrooms'),
                'bathrooms': prop.get('bathrooms'),
                'city': addr.get('city'),
                'homeStatus': prop.get('homeStatus'),
                'homeType': prop.get('homeType'),
                'livingArea': prop.get('livingAreaValue') or prop.get('livingArea'),
                'lotSize': prop.get('lotSize'),
                'price': prop.get('price'),
                'zestimate': prop.get('zestimate'),
                'yearBuilt': prop.get('yearBuilt'),
                'latitude': prop.get('latitude'),
                'longitude': prop.get('longitude'),
                'propertyTaxRate': None,  # Not in new API response
                'daysOnZillow': prop.get('daysOnZillow'),
            }

            # Add tax history
            tax_history = prop.get('taxHistory', [])
            if tax_history:
                transformed['taxHistory'] = tax_history

            # Add price history
            price_history = prop.get('priceHistory', [])
            if price_history:
                transformed['priceHistory'] = price_history

            # Add photos - transform to proper format
            original_photos = prop.get('originalPhotos', [])
            if original_photos:
                transformed['originalPhotos'] = self._transform_photos(original_photos)
            else:
                transformed['originalPhotos'] = []

            # Add open house schedule (if exists)
            transformed['openHouseSchedule'] = []

            # If details requested, add comprehensive fields
            if include_details:
                transformed['description'] = prop.get('description')
                transformed['newConstructionType'] = prop.get('newConstructionType')

                # Add resoFacts if available
                reso_facts = prop.get('resoFacts', {})
                if reso_facts:
                    transformed['resoFacts'] = reso_facts

            return transformed

        except Exception as e:
            logger.error(f"Error transforming property details: {str(e)}", exc_info=True)
            raise ValueError(f"Failed to transform property data: {str(e)}")
