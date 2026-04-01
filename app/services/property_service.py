import re
import math
import logging
from typing import List, Optional, Dict, Any, Union
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func
from app.schemas.collection_preferences import CollectionPreferencesBase as CollectionPreferencesSchema
from app.models.database import Property
from app.models.property import PropertySummaryResponse, PropertyDetailResponse
from app.utils.geo import get_lat_long_offsets, filter_properties_by_radius, is_within_distance

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
        street_tokens = street_part.split()
        house_number = None
        street_root = None
        
        filters = []

        if street_tokens and re.match(r'^\d+', street_tokens[0]):
            # Extract just the numeric part of the house number (matches "42" or "42B")
            house_number = re.match(r'^\d+', street_tokens[0]).group()
            
            # Anchor 1: Starts with House Number (Uses B-Tree index)
            filters.append(Property.street_address.like(f"{house_number} %"))

            directionals = {'N', 'S', 'E', 'W', 'NW', 'NE', 'SW', 'SE', 'NORTH', 'SOUTH', 'EAST', 'WEST'}
            
            for i, token in enumerate(street_tokens[1:], 1):
                clean_token = re.sub(r'[^a-zA-Z0-9]', '', token).upper()
                
                # MIRROR ADDRESS FIX: If the first token after the number is a directional,
                # we add a strict prefix filter (e.g., "603 S %") to prevent matching "603 N %".
                if i == 1 and clean_token in directionals:
                    # Support both full word and abbreviation (e.g., "South" and "S")
                    dir_options = [clean_token]
                    if clean_token in {'NORTH', 'SOUTH', 'EAST', 'WEST'}:
                        dir_options.append(clean_token[0])
                    elif clean_token in {'N', 'S', 'E', 'W'}:
                        full_map = {'N': 'NORTH', 'S': 'SOUTH', 'E': 'EAST', 'W': 'WEST'}
                        dir_options.append(full_map[clean_token])
                    
                    filters.append(or_(*[Property.street_address.ilike(f"{house_number} {opt} %") for opt in dir_options]))
                
                # Identify Root (First non-directional word)
                if not street_root and clean_token not in directionals and clean_token:
                    street_root = token
            
            # If everything was a directional (unlikely), just use the first one after number
            if not street_root and len(street_tokens) > 1:
                street_root = street_tokens[1]
            
            # Anchor 2: Contains the primary street word (Handles Lane vs Ln)
            if street_root:
                filters.append(Property.street_address.ilike(f"%{street_root}%"))

            # 3.4 Suffix Mapping (ST/STREET, AVE/AVENUE, etc.)
            suffix_map = {
                'ST': 'STREET', 'STREET': 'ST',
                'AVE': 'AVENUE', 'AVENUE': 'AVE',
                'RD': 'ROAD', 'ROAD': 'RD',
                'DR': 'DRIVE', 'DRIVE': 'DR',
                'LN': 'LANE', 'LANE': 'LN',
                'PL': 'PLACE', 'PLACE': 'PL',
                'TER': 'TERRACE', 'TERRACE': 'TER',
                'CT': 'COURT', 'COURT': 'CT',
                'BLVD': 'BOULEVARD', 'BOULEVARD': 'BLVD',
                'HWY': 'HIGHWAY', 'HIGHWAY': 'HWY'
            }
            
            last_token = re.sub(r'[^a-zA-Z0-9]', '', street_tokens[-1]).upper()
            if last_token in suffix_map:
                suffix_options = [last_token, suffix_map[last_token]]
                # Match suffix at the end of the address
                filters.append(or_(*[Property.street_address.ilike(f"% {opt}") for opt in suffix_options]))
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
        max_properties: int = 500
    ) -> List[PropertySummaryResponse]:
        """
        Performs a local SQLAlchemy search against the mirrored 'properties' table.
        Implements radius filtering (Bounding Box), price, beds/baths, and location filters.
        """
        query = select(Property)
        filters = []
        
        # Types that typically don't have bathrooms or bedrooms (Exempt from numeric filters)
        exempt_types = ['LAND', 'FARM', 'COMMERCIAL', 'OTHER']

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

        if preferences.school_districts:
            for sd_pref in preferences.school_districts:
                # 1. Split to get name and state (e.g. "Radnor Township, PA" -> ["Radnor Township", "PA"])
                parts = [s.strip() for s in sd_pref.split(',')]
                raw_name = parts[0]
                pref_state = parts[1].upper() if len(parts) >= 2 else None
                
                # 2. Clean and match exactly (case-insensitive)
                if pref_state:
                    location_filters.append(and_(
                        Property.school_district_name.ilike(raw_name),
                        Property.state.ilike(pref_state)
                    ))
                else:
                    location_filters.append(Property.school_district_name.ilike(raw_name))

        if location_filters:
            # Join combined City/Township filters with OR
            filters.append(or_(*location_filters))
                
        elif preferences.lat is not None and preferences.long is not None and preferences.diameter:
            # Bounding Box Math for Radius (only runs if no City/Township set)
            lat_offset, long_offset = get_lat_long_offsets(preferences.lat, preferences.diameter)
            
            filters.append(and_(
                Property.latitude >= float(preferences.lat) - lat_offset,
                Property.latitude <= float(preferences.lat) + lat_offset,
                Property.longitude >= float(preferences.long) - long_offset,
                Property.longitude <= float(preferences.long) + long_offset
            ))

        # 2. Status Filtering
        # Match exact BRIGHT statuses
        filters.append(or_(
            Property.home_status == 'ACTIVE-BRIGHT',
            Property.home_status == 'COMING SOON-BRIGHT'
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

        # 5. Circular Post-Filtering (Trim the Corners)
        if not location_filters and preferences.lat is not None and preferences.long is not None and preferences.diameter:
            properties = filter_properties_by_radius(
                properties, 
                float(preferences.lat), 
                float(preferences.long), 
                float(preferences.diameter)
            )

        return [PropertySummaryResponse.model_validate(p) for p in properties]

    @staticmethod
    async def get_properties_count_by_preferences(db: AsyncSession, preferences: CollectionPreferencesSchema) -> int:
        """Fetch only the count of matching properties without downloading records."""
        # Use precise circular filtering if searching by radius (no explicit locations)
        # to ensure count matches actual discovery logic.
        use_circular_filter = (
            not preferences.cities and 
            not preferences.townships and 
            not preferences.school_districts and 
            preferences.lat is not None and 
            preferences.long is not None and 
            preferences.diameter
        )

        if use_circular_filter:
            query = select(Property.latitude, Property.longitude)
        else:
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

        if preferences.school_districts:
            for sd_pref in preferences.school_districts:
                # 1. Split to get name and state (e.g. "Radnor Township, PA" -> ["Radnor Township", "PA"])
                parts = [s.strip() for s in sd_pref.split(',')]
                raw_name = parts[0]
                pref_state = parts[1].upper() if len(parts) >= 2 else None
                
                # 2. Clean and match exactly (case-insensitive)
                if pref_state:
                    location_filters.append(and_(
                        Property.school_district_name.ilike(raw_name),
                        Property.state.ilike(pref_state)
                    ))
                else:
                    location_filters.append(Property.school_district_name.ilike(raw_name))

        if location_filters:
            filters.append(or_(*location_filters))
                
        elif preferences.lat is not None and preferences.long is not None and preferences.diameter:
            # radius = diameter / 2
            lat_offset, long_offset = get_lat_long_offsets(preferences.lat, preferences.diameter)
            
            filters.append(and_(
                Property.latitude >= float(preferences.lat) - lat_offset,
                Property.latitude <= float(preferences.lat) + lat_offset,
                Property.longitude >= float(preferences.long) - long_offset,
                Property.longitude <= float(preferences.long) + long_offset
            ))

        # 2. Status Filtering
        filters.append(or_(
            Property.home_status == 'ACTIVE-BRIGHT',
            Property.home_status == 'COMING SOON-BRIGHT'
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
        
        if use_circular_filter:
            # Perform precise circular filter on the bounding box results
            rows = result.all()
            center_lat = float(preferences.lat)
            center_long = float(preferences.long)
            radius_miles = float(preferences.diameter)
            
            count = 0
            for p_lat, p_lon in rows:
                if is_within_distance(center_lat, center_long, p_lat, p_lon, radius_miles):
                    count += 1
            return count
        else:
            return result.scalar() or 0

    @staticmethod
    async def bright_mls_id_exists(db: AsyncSession, mls_id: str) -> bool:
        """
        Validates if a Bright MLS ID exists.
        Checks our local 'properties' mirror first (faster).
        If not found, falls back to the Bright MLS API.
        """
        # 1. Check local mirror (as an agent email or listing key)
        # stmt = select(Property.id).where(or_(Property.list_agent_email.ilike(mls_id), Property.listing_key == mls_id)).limit(1)
        # result = await db.execute(stmt)
        # if result.scalar_one_or_none():
        #     return True
            
        from app.services.bright_mls_service import bright_mls_service
        return await bright_mls_service.bright_mls_id_exists(mls_id)

property_service = PropertyService()
