import re
import math
import logging
from typing import List, Optional, Dict, Any, Union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func
from app.schemas.collection_preferences import CollectionPreferencesBase as CollectionPreferencesSchema
from app.models.database import Property
from app.models.property import PropertySummaryResponse, PropertyDetailResponse

# Configure logging
logger = logging.getLogger(__name__)

class PropertyService:
    """
    Local Search Engine that queries our mirrored 'properties' table.
    Replaces BrightMlsService for all frontend lookups.
    """

    @staticmethod
    async def get_property_by_listing_key(db: AsyncSession, listing_key: str) -> Optional[PropertyDetailResponse]:
        """Fetch full property details from the local mirror."""
        stmt = select(Property).where(Property.listing_key == str(listing_key))
        result = await db.execute(stmt)
        property_obj = result.scalar_one_or_none()
        
        if not property_obj:
            return None
            
        return PropertyDetailResponse.model_validate(property_obj)

    @staticmethod
    async def get_property_by_address(db: AsyncSession, raw_address: str) -> Optional[PropertyDetailResponse]:
        """
        High-performance address search optimized for 'Street, City, State Zip'.
        Uses anchors (House Number, Zip) to ensure index usage and abbreviation resilience.
        """
        if not raw_address:
            return None

        # 1. Clean and Split Input
        parts = [p.strip() for p in raw_address.split(',')]
        street_part = parts[0]
        
        # 2. Extract Zip Code (Looks for 5 digits)
        zip_match = re.search(r'\b\d{5}\b', raw_address)
        zip_code = zip_match.group(0) if zip_match else None

        # 3. Parse House Number and Street Root (e.g., "71 Drummers" -> "71", "Drummers")
        street_match = re.match(r'^(\d+)\s+([a-zA-Z0-9]+)', street_part)
        
        filters = []
        
        if street_match:
            house_number = street_match.group(1)
            street_root = street_match.group(2)
            
            # Anchor 1: Starts with House Number (Uses B-Tree index)
            filters.append(Property.street_address.like(f"{house_number} %"))
            # Anchor 2: Contains the primary street word (Handles Lane vs Ln)
            filters.append(Property.street_address.ilike(f"%{street_root}%"))
        else:
            # Fallback to fuzzy if no house number detected
            filters.append(Property.street_address.ilike(f"%{street_part}%"))

        # 4. Geographic Anchor (Zip OR City for resilience)
        geo_filters = []
        if zip_code:
            geo_filters.append(Property.zipcode == zip_code)
        
        if len(parts) > 1:
            city = parts[1].strip()
            geo_filters.append(Property.city.ilike(city))

        if geo_filters:
            filters.append(or_(*geo_filters))

        # 5. Execute
        stmt = select(Property).where(and_(*filters)).limit(1)
        
        logger.info(f"Address Lookup: {raw_address} | Street: {street_part} | Zip: {zip_code}")
        
        result = await db.execute(stmt)
        property_obj = result.scalar_one_or_none()

        if not property_obj:
            logger.warning(f"Address Lookup Failed: {raw_address}")
            return None
            
        return PropertyDetailResponse.model_validate(property_obj)

    @staticmethod
    async def get_properties_by_preferences(
        db: AsyncSession, 
        preferences: CollectionPreferencesSchema, 
        max_properties: int = 200
    ) -> List[PropertySummaryResponse]:
        """
        Performs a local SQLAlchemy search against the mirrored 'properties' table.
        Implements radius filtering (Bounding Box), price, beds/baths, and location filters.
        """
        query = select(Property)
        filters = []
        
        # Types that typically don't have bathrooms or bedrooms (Exempt from numeric filters)
        exempt_types = ['LAND', 'FARM', 'COMMERCIAL', 'RESIDENTIAL_LEASE', 'OTHER']

        # 1. Location Filtering (Cities OR Townships OR Radius)
        location_filters = []

        if preferences.cities:
            for loc in preferences.cities:
                parts = [s.strip() for s in loc.split(',')]
                city = parts[0]
                state = parts[1] if len(parts) >= 2 else "PA"
                location_filters.append(and_(Property.city.ilike(city), Property.state.ilike(state)))

        if preferences.townships:
            for township_pref in preferences.townships:
                # 1. Split to get name and state (e.g. "Radnor Township, PA" -> ["Radnor Township", "PA"])
                parts = [s.strip() for s in township_pref.split(',')]
                raw_name = parts[0]
                pref_state = parts[1].upper() if len(parts) >= 2 else None
                
                # 2. Clean the root name to match standardized DB format
                root_name = re.sub(r'\s+(Township|Twp|Boro|Borough|City|Town)$', '', raw_name, flags=re.I).strip()
                root_name = root_name.upper()
                
                # 3. Filter by both Township and State for geographical accuracy
                if pref_state:
                    location_filters.append(and_(
                        Property.township == root_name,
                        Property.state.ilike(pref_state)
                    ))
                else:
                    location_filters.append(Property.township == root_name)

        if location_filters:
            # Join combined City/Township filters with OR
            filters.append(or_(*location_filters))
                
        elif preferences.lat is not None and preferences.long is not None and preferences.diameter:
            # Bounding Box Math for Radius (only runs if no City/Township set)
            # Bounding Box Math for Radius (approximate)
            # diameter = total width, so radius = diameter / 2
            radius_miles = float(preferences.diameter) / 2.0
            lat_offset = radius_miles / 69.1
            cos_lat = math.cos(math.radians(float(preferences.lat)))
            long_offset = radius_miles / (69.1 * cos_lat) if abs(cos_lat) > 0.0001 else lat_offset
            
            filters.append(and_(
                Property.latitude >= float(preferences.lat) - lat_offset,
                Property.latitude <= float(preferences.lat) + lat_offset,
                Property.longitude >= float(preferences.long) - long_offset,
                Property.longitude <= float(preferences.long) + long_offset
            ))

        # 2. Status Filtering
        # Match statuses starting with ACTIVE or COMING SOON
        filters.append(or_(
            Property.home_status.ilike('ACTIVE%'),
            Property.home_status.ilike('COMING SOON%')
        ))

        # 3. Numeric Filters (with Smart Exemptions)
        numeric_filters = []
        if preferences.min_price is not None and preferences.min_price > 0:
            filters.append(Property.price >= preferences.min_price)
        if preferences.max_price is not None and preferences.max_price > 0:
            filters.append(Property.price <= preferences.max_price)
            
        if preferences.min_beds is not None and preferences.min_beds > 0:
            numeric_filters.append(Property.bedrooms >= preferences.min_beds)
        if preferences.max_beds is not None and preferences.max_beds > 0:
            numeric_filters.append(Property.bedrooms <= preferences.max_beds)
            
        if preferences.min_baths is not None and preferences.min_baths > 0:
            numeric_filters.append(Property.bathrooms >= float(preferences.min_baths))
        if preferences.max_baths is not None and preferences.max_baths > 0:
            numeric_filters.append(Property.bathrooms <= float(preferences.max_baths))
            
        if preferences.min_year_built is not None:
            numeric_filters.append(Property.year_built >= preferences.min_year_built)
        if preferences.max_year_built is not None:
            numeric_filters.append(Property.year_built <= preferences.max_year_built)

        # Apply numeric filters conditionally (either it's exempt or it matches)
        if numeric_filters:
            filters.append(or_(
                Property.home_type.in_(exempt_types),
                and_(*numeric_filters)
            ))

        # 4. Property Type Filtering
        selected_types = []
        if preferences.is_single_family:
            selected_types.append('SINGLE_FAMILY')
        if preferences.is_town_house:
            selected_types.append('TOWNHOUSE')
        if preferences.is_condo:
            selected_types.append('CONDO')
        if preferences.is_multi_family:
            selected_types.append('MULTI_FAMILY')
        if preferences.is_lot_land:
            selected_types.extend(['LAND', 'FARM'])
        if preferences.is_farm:
            selected_types.append('FARM')
        if preferences.is_commercial:
            selected_types.append('COMMERCIAL')
        if preferences.is_apartment:
            selected_types.append('RESIDENTIAL_LEASE')
            
        if selected_types:
            filters.append(Property.home_type.in_(selected_types))

        # Apply all filters
        if filters:
            query = query.where(and_(*filters))

        # Sort by most recent update/sync
        query = query.order_by(Property.modification_timestamp.desc().nulls_last())

        # Execute
        query = query.limit(max_properties)
        
        logger.info(f"Discovery Search: filters={len(filters)} | types={selected_types}")
        
        result = await db.execute(query)
        properties = result.scalars().all()

        return [PropertySummaryResponse.model_validate(p) for p in properties]

    @staticmethod
    async def get_properties_count_by_preferences(db: AsyncSession, preferences: CollectionPreferencesSchema) -> int:
        """Fetch only the count of matching properties without downloading records."""
        query = select(func.count(Property.id))
        filters = []
        exempt_types = ['LAND', 'FARM', 'COMMERCIAL', 'RESIDENTIAL_LEASE', 'OTHER']

        # 1. Location Filtering (Cities OR Townships OR Radius)
        location_filters = []

        if preferences.cities:
            for loc in preferences.cities:
                parts = [s.strip() for s in loc.split(',')]
                city = parts[0]
                state = parts[1] if len(parts) >= 2 else "PA"
                location_filters.append(and_(Property.city.ilike(city), Property.state.ilike(state)))

        if preferences.townships:
            for township_pref in preferences.townships:
                parts = [s.strip() for s in township_pref.split(',')]
                raw_name = parts[0]
                pref_state = parts[1].upper() if len(parts) >= 2 else None
                
                root_name = re.sub(r'\s+(Township|Twp|Boro|Borough|City|Town)$', '', raw_name, flags=re.I).strip()
                root_name = root_name.upper()
                
                if pref_state:
                    location_filters.append(and_(
                        Property.township == root_name,
                        Property.state.ilike(pref_state)
                    ))
                else:
                    location_filters.append(Property.township == root_name)

        if location_filters:
            filters.append(or_(*location_filters))
                
        elif preferences.lat is not None and preferences.long is not None and preferences.diameter:
            # radius = diameter / 2
            radius_miles = float(preferences.diameter) / 2.0
            lat_offset = radius_miles / 69.1
            cos_lat = math.cos(math.radians(float(preferences.lat)))
            long_offset = radius_miles / (69.1 * cos_lat) if abs(cos_lat) > 0.0001 else lat_offset
            
            filters.append(and_(
                Property.latitude >= float(preferences.lat) - lat_offset,
                Property.latitude <= float(preferences.lat) + lat_offset,
                Property.longitude >= float(preferences.long) - long_offset,
                Property.longitude <= float(preferences.long) + long_offset
            ))

        # 2. Status Filtering
        filters.append(or_(
            Property.home_status.ilike('ACTIVE%'),
            Property.home_status.ilike('COMING SOON%')
        ))

        # 3. Numeric Filters (with Smart Exemptions)
        numeric_filters = []
        if preferences.min_price is not None and preferences.min_price > 0:
            filters.append(Property.price >= preferences.min_price)
        if preferences.max_price is not None and preferences.max_price > 0:
            filters.append(Property.price <= preferences.max_price)
            
        if preferences.min_beds is not None and preferences.min_beds > 0:
            numeric_filters.append(Property.bedrooms >= preferences.min_beds)
        if preferences.max_beds is not None and preferences.max_beds > 0:
            numeric_filters.append(Property.bedrooms <= preferences.max_beds)
            
        if preferences.min_baths is not None and preferences.min_baths > 0:
            numeric_filters.append(Property.bathrooms >= float(preferences.min_baths))
        if preferences.max_baths is not None and preferences.max_baths > 0:
            numeric_filters.append(Property.bathrooms <= float(preferences.max_baths))
            
        if preferences.min_year_built is not None:
            numeric_filters.append(Property.year_built >= preferences.min_year_built)
        if preferences.max_year_built is not None:
            numeric_filters.append(Property.year_built <= preferences.max_year_built)

        if numeric_filters:
            filters.append(or_(
                Property.home_type.in_(exempt_types),
                and_(*numeric_filters)
            ))

        # 4. Property Type Filtering
        selected_types = []
        if preferences.is_single_family:
            selected_types.append('SINGLE_FAMILY')
        if preferences.is_town_house:
            selected_types.append('TOWNHOUSE')
        if preferences.is_condo:
            selected_types.append('CONDO')
        if preferences.is_multi_family:
            selected_types.append('MULTI_FAMILY')
        if preferences.is_lot_land:
            selected_types.extend(['LAND', 'FARM'])
        if preferences.is_farm:
            selected_types.append('FARM')
        if preferences.is_commercial:
            selected_types.append('COMMERCIAL')
        if preferences.is_apartment:
            selected_types.append('RESIDENTIAL_LEASE')
            
        if selected_types:
            filters.append(Property.home_type.in_(selected_types))

        if filters:
            query = query.where(and_(*filters))

        result = await db.execute(query)
        return result.scalar() or 0

    @staticmethod
    async def bright_mls_id_exists(db: AsyncSession, mls_id: str) -> bool:
        """
        Validates if a Bright MLS ID exists.
        Checks our local 'properties' mirror first (faster).
        If not found, falls back to the Bright MLS API.
        """
        # 1. Check local mirror (as an agent email or listing key)
        stmt = select(Property.id).where(or_(Property.list_agent_email.ilike(mls_id), Property.listing_key == mls_id)).limit(1)
        result = await db.execute(stmt)
        if result.scalar_one_or_none():
            return True
            
        # 2. Fallback to API
        from app.services.bright_mls_service import bright_mls_service
        return await bright_mls_service.bright_mls_id_exists(mls_id)

property_service = PropertyService()
