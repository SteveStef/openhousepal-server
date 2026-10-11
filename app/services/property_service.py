import re
import logging
from typing import Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func, case
from app.schemas.collection_preferences import CollectionPreferencesBase as CollectionPreferencesSchema
from app.models.database import Property
from app.models.property import PropertySummaryResponse, PropertyDetailResponse
from app.services.google_geocoding_service import ensure_google_rooftop_geocode
from app.utils.same_home import dedupe_same_homes
from app.utils.geo import get_lat_long_offsets, filter_properties_by_radius, is_within_distance
# Reusing the Areas module's existing ray-casting point-in-polygon SQL (no PostGIS - see that
# module's own comments) rather than building a second polygon-filtering implementation for the
# Showcase "Draw on Map" search area.
from app.services.areas.geography import _polygon_filter



# Listing types that aren't homes for sale - only matched when a search asks for them.
NOT_FOR_SALE_HOME_TYPES = ('RESIDENTIAL_LEASE', 'COMMERCIAL')
MIN_FOR_SALE_HOME_PRICE = 10000


def pref_counties(preferences) -> list:
    """A Showcase's counties. Older form-built Showcases saved them only in the single `county`
    text field ("DELAWARE-PA, CHESTER-PA"), which nothing searched - read that too."""
    counties = list(getattr(preferences, "counties", None) or [])
    if counties:
        return counties
    legacy = (getattr(preferences, "county", None) or "").strip()
    return [c.strip() for c in legacy.split(",") if len(c.strip()) > 2] if legacy else []


def _polygon_bbox(geometry: dict[str, Any]) -> Optional[dict[str, float]]:
    """Min/max lat/lng across every ring of a GeoJSON Polygon/MultiPolygon - an indexable SQL
    prefilter ahead of the expensive per-edge ray-casting expression, same pattern areas/geography.py
    uses for its own polygon scope."""
    if geometry.get("type") == "Polygon":
        polygons = [geometry.get("coordinates") or []]
    elif geometry.get("type") == "MultiPolygon":
        polygons = geometry.get("coordinates") or []
    else:
        return None

    lats, lngs = [], []
    for polygon in polygons:
        for ring in polygon:
            for point in ring:
                try:
                    lngs.append(float(point[0]))
                    lats.append(float(point[1]))
                except (TypeError, ValueError, IndexError):
                    continue
    if not lats or not lngs:
        return None
    return {"min_lat": min(lats), "max_lat": max(lats), "min_lng": min(lngs), "max_lng": max(lngs)}

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

        property_obj = await ensure_google_rooftop_geocode(db, property_obj)
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
        # A relisted property gets a fresh Bright ListingKey each time, so one street address can
        # map to several Property rows (e.g. a 2017 CLOSED sale + a current ACTIVE listing). An
        # unordered LIMIT 1 returned an arbitrary one - usually the oldest by heap order - which is
        # how a CMA subject picked up a years-old closed record instead of the live listing. Prefer
        # active/coming-soon, then pending/under-contract, then closed, and within a tier the most
        # recently updated row.
        clean_status = func.upper(func.replace(func.coalesce(Property.home_status, ""), "-BRIGHT", ""))
        status_priority = case(
            (clean_status.in_(["ACTIVE", "COMING SOON"]), 0),
            (clean_status.ilike("PENDING%"), 1),
            (clean_status.ilike("%CONTRACT%"), 1),
            (clean_status.ilike("CLOSED%"), 2),
            (clean_status.ilike("SOLD%"), 2),
            else_=3,
        )
        stmt = (
            select(Property)
            .where(and_(*filters))
            .order_by(
                status_priority,
                func.coalesce(Property.modification_timestamp, Property.created_at).desc(),
            )
            .limit(1)
        )

        logger.info(f"Address Lookup: {raw_address} | Street: {street_part} | Zip: {zip_code}")
        
        result = await db.execute(stmt)
        property_obj = result.scalar_one_or_none()

        if not property_obj:
            logger.warning(f"Address Lookup Failed: {raw_address}")
            return None

        property_obj = await ensure_google_rooftop_geocode(db, property_obj)
        return PropertyDetailResponse.model_validate(property_obj)

    @staticmethod
    def get_township_search_values(name: str) -> List[str]:
        """
        Maps standardized categorical names back to all possible raw MLS abbreviations.
        This ensures that selecting 'ANNE ARUNDEL COUNTY' in the UI matches 'AA' in the DB.
        """
        mappings = {
            'ANNE ARUNDEL COUNTY': ['ANNE ARUNDEL COUNTY', 'AA', 'AA COUNTY'],
            'BALTIMORE CITY': ['BALTIMORE CITY', 'BA', 'BA COUNTY'],
            'BALTIMORE COUNTY': ['BALTIMORE COUNTY', 'BC', 'BC COUNTY']
        }
        name_upper = name.upper()
        return mappings.get(name_upper, [name_upper, name])

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

        # 1. Location Filtering
        # Town/township, school district, and county are a geographic hierarchy (a county contains
        # school districts contain towns) - so picking values in more than one of these narrows
        # down through that hierarchy (AND) rather than adding a second unrelated area (OR).
        # Picking two towns, or two districts, or two counties still unions within that one field
        # (OR) - "Villanova or Gladwyne" is still one broadening choice about which towns count.
        place_filters = []
        school_district_filters = []
        county_filters = []

        if preferences.cities:
            for loc in preferences.cities:
                parts = [s.strip() for s in loc.split(',')]
                city = parts[0]
                state = parts[1] if len(parts) >= 2 else None

                if state:
                    place_filters.append(and_(Property.city.ilike(city), Property.state.ilike(state)))
                else:
                    place_filters.append(Property.city.ilike(city))

        if preferences.townships:
            for loc in preferences.townships:
                parts = [s.strip() for s in loc.split(',')]
                township = parts[0]
                state = parts[1] if len(parts) >= 2 else None

                # Get all possible variations (aliases) for this township name
                search_names = PropertyService.get_township_search_values(township)

                if state:
                    place_filters.append(and_(Property.township.in_(search_names), Property.state.ilike(state)))
                else:
                    place_filters.append(Property.township.in_(search_names))

        if preferences.school_districts:
            for sd_pref in preferences.school_districts:
                # 1. Split to get name and state (e.g. "Radnor Township, PA" -> ["Radnor Township", "PA"])
                parts = [s.strip() for s in sd_pref.split(',')]
                raw_name = parts[0]
                pref_state = parts[1].upper() if len(parts) >= 2 else None

                # 2. Substring match (case-insensitive) - real MLS district names are full legal
                # names ("RADNOR TOWNSHIP"), while both the search-bar free-text parser and a
                # visitor's own typed shorthand ("radnor schools") only ever produce the short
                # colloquial name ("radnor") - an exact match would silently return zero results.
                if pref_state:
                    school_district_filters.append(and_(
                        Property.school_district_name.ilike(f"%{raw_name}%"),
                        Property.state.ilike(pref_state)
                    ))
                else:
                    school_district_filters.append(Property.school_district_name.ilike(f"%{raw_name}%"))

        if pref_counties(preferences):
            for county_pref in pref_counties(preferences):
                parts = [s.strip() for s in county_pref.split(',')]
                raw_name = parts[0]
                pref_state = parts[1].upper() if len(parts) >= 2 else None
                if pref_state:
                    county_filters.append(and_(
                        Property.county.ilike(raw_name),
                        Property.state.ilike(pref_state)
                    ))
                else:
                    county_filters.append(Property.county.ilike(raw_name))

        # AND the non-empty hierarchy dimensions together (each already OR'd within itself above).
        location_filters = []
        hierarchy_groups = [g for g in (place_filters, school_district_filters, county_filters) if g]
        if hierarchy_groups:
            location_filters.append(and_(*[or_(*group) for group in hierarchy_groups]))

        # Geography criteria stack: an agent can combine the town/district/county hierarchy above
        # with a radius and drawn shapes all at once - those are independent, additive areas (OR),
        # unlike the hierarchy fields (AND) above.
        geo_filters = list(location_filters)

        if preferences.lat is not None and preferences.long is not None and preferences.diameter:
            lat_offset, long_offset = get_lat_long_offsets(preferences.lat, preferences.diameter)
            geo_filters.append(and_(
                Property.latitude >= float(preferences.lat) - lat_offset,
                Property.latitude <= float(preferences.lat) + lat_offset,
                Property.longitude >= float(preferences.long) - long_offset,
                Property.longitude <= float(preferences.long) + long_offset
            ))

        if getattr(preferences, 'search_polygon', None):
            # Agent-drawn "Draw on Map" search area(s). Bbox prefilter first (indexable), then the
            # exact ray-casting expression (already ORs multiple shapes together on its own - see
            # areas/geography.py's _polygon_filter).
            bbox = _polygon_bbox(preferences.search_polygon)
            if bbox:
                geo_filters.append(and_(
                    Property.latitude.isnot(None),
                    Property.longitude.isnot(None),
                    Property.latitude >= bbox["min_lat"],
                    Property.latitude <= bbox["max_lat"],
                    Property.longitude >= bbox["min_lng"],
                    Property.longitude <= bbox["max_lng"],
                    _polygon_filter(Property, preferences.search_polygon),
                ))

        if geo_filters:
            filters.append(or_(*geo_filters))

        # 2. Status Filtering
        # Match exact BRIGHT statuses
        filters.append(or_(
            Property.home_status == 'ACTIVE-BRIGHT',
            Property.home_status == 'COMING SOON-BRIGHT'
        ))
        # Agent-entered "My Listings" rows are not real MLS inventory - never surface them in
        # showcase/collection matching (they belong only to that agent's own Hub/content tools).
        filters.append(Property.is_manual.is_(False))

        # 3. Numeric Filters (with Smart Exemptions)
        numeric_filters = []
        if preferences.min_price is not None and preferences.min_price > 0:
            filters.append(Property.price >= preferences.min_price)
        if preferences.max_price is not None and preferences.max_price > 0:
            filters.append(Property.price <= preferences.max_price)

        # Lot size applies to land/farm parcels too, so - unlike beds/baths/sqft below - it's a
        # direct filter rather than part of the exempt-types numeric_filters group.
        if getattr(preferences, 'min_acres', None) is not None and preferences.min_acres > 0:
            filters.append(Property.lot_size_acres >= preferences.min_acres)
        if getattr(preferences, 'max_acres', None) is not None and preferences.max_acres > 0:
            filters.append(Property.lot_size_acres <= preferences.max_acres)

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

        if getattr(preferences, 'min_sqft', None) is not None and preferences.min_sqft > 0:
            numeric_filters.append(Property.living_area >= preferences.min_sqft)
        if getattr(preferences, 'max_sqft', None) is not None and preferences.max_sqft > 0:
            numeric_filters.append(Property.living_area <= preferences.max_sqft)

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
        else:
            # No type picked = any home FOR SALE. Rentals and commercial spaces only come in when
            # the agent picks them (Rentals / Commercial) - "any" used to pull $2,100/mo rentals and
            # retail units into a buyer's Showcase (and past the beds/baths filter, as exempt types).
            filters.append(or_(Property.home_type.is_(None), Property.home_type.notin_(NOT_FOR_SALE_HOME_TYPES)))
        if not preferences.is_apartment and not preferences.is_commercial:
            # Rentals mis-tagged as homes for sale, timeshare weeks, parking spaces: nothing but land
            # lists for sale under $10k, so those never reach a buyer unless they asked for rentals.
            filters.append(or_(Property.price.is_(None), Property.price >= MIN_FOR_SALE_HOME_PRICE, Property.home_type.in_(('LAND', 'FARM'))))

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

        # 5. Circular Post-Filtering (Trim the Corners) - only safe when radius is the *only*
        # active geo criterion; trimming to an exact circle after combining with other criteria
        # would wrongly discard rows that only matched via a town/school-district/county/polygon.
        if not location_filters and not getattr(preferences, 'search_polygon', None) and preferences.lat is not None and preferences.long is not None and preferences.diameter:
            properties = filter_properties_by_radius(
                properties,
                float(preferences.lat),
                float(preferences.long),
                float(preferences.diameter)
            )

        # One house listed twice (two MLS numbers / two towns) shows once - the newest listing.
        properties = dedupe_same_homes(properties)

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
            not pref_counties(preferences) and
            not getattr(preferences, 'search_polygon', None) and
            preferences.lat is not None and
            preferences.long is not None and
            preferences.diameter
        )

        if use_circular_filter:
            query = select(Property.latitude, Property.longitude)
        else:
            query = select(func.count(Property.id))

        filters = []
        # Same list as get_properties_by_preferences - the count is shown next to those homes.
        exempt_types = ['LAND', 'FARM', 'COMMERCIAL', 'OTHER']

        # 1. Location Filtering - same town/school-district/county hierarchy (AND across
        # dimensions, OR within each one) as get_properties_by_preferences above; keep both in sync.
        place_filters = []
        school_district_filters = []
        county_filters = []

        if preferences.cities:
            for loc in preferences.cities:
                parts = [s.strip() for s in loc.split(',')]
                city = parts[0]
                state = parts[1] if len(parts) >= 2 else "PA"
                place_filters.append(and_(Property.city.ilike(city), Property.state.ilike(state)))

        if preferences.townships:
            for township_pref in preferences.townships:
                parts = [s.strip() for s in township_pref.split(',')]
                raw_name = parts[0]
                pref_state = parts[1].upper() if len(parts) >= 2 else None

                root_name = re.sub(r'\s+(Township|Twp|Boro|Borough|City|Town)$', '', raw_name, flags=re.I).strip()
                root_name = root_name.upper()

                if pref_state:
                    place_filters.append(and_(
                        Property.township == root_name,
                        Property.state.ilike(pref_state)
                    ))
                else:
                    place_filters.append(Property.township == root_name)

        if preferences.school_districts:
            for sd_pref in preferences.school_districts:
                # 1. Split to get name and state (e.g. "Radnor Township, PA" -> ["Radnor Township", "PA"])
                parts = [s.strip() for s in sd_pref.split(',')]
                raw_name = parts[0]
                pref_state = parts[1].upper() if len(parts) >= 2 else None

                # 2. Substring match (case-insensitive) - see matching comment in
                # get_properties_by_preferences above; keep both in sync.
                if pref_state:
                    school_district_filters.append(and_(
                        Property.school_district_name.ilike(f"%{raw_name}%"),
                        Property.state.ilike(pref_state)
                    ))
                else:
                    school_district_filters.append(Property.school_district_name.ilike(f"%{raw_name}%"))

        if pref_counties(preferences):
            for county_pref in pref_counties(preferences):
                parts = [s.strip() for s in county_pref.split(',')]
                raw_name = parts[0]
                pref_state = parts[1].upper() if len(parts) >= 2 else None
                if pref_state:
                    county_filters.append(and_(
                        Property.county.ilike(raw_name),
                        Property.state.ilike(pref_state)
                    ))
                else:
                    county_filters.append(Property.county.ilike(raw_name))

        location_filters = []
        hierarchy_groups = [g for g in (place_filters, school_district_filters, county_filters) if g]
        if hierarchy_groups:
            location_filters.append(and_(*[or_(*group) for group in hierarchy_groups]))

        # Same stacking as get_properties_by_preferences above - keep both in sync.
        geo_filters = list(location_filters)

        if getattr(preferences, 'search_polygon', None):
            bbox = _polygon_bbox(preferences.search_polygon)
            if bbox:
                geo_filters.append(and_(
                    Property.latitude.isnot(None),
                    Property.longitude.isnot(None),
                    Property.latitude >= bbox["min_lat"],
                    Property.latitude <= bbox["max_lat"],
                    Property.longitude >= bbox["min_lng"],
                    Property.longitude <= bbox["max_lng"],
                    _polygon_filter(Property, preferences.search_polygon),
                ))

        if preferences.lat is not None and preferences.long is not None and preferences.diameter:
            lat_offset, long_offset = get_lat_long_offsets(preferences.lat, preferences.diameter)
            geo_filters.append(and_(
                Property.latitude >= float(preferences.lat) - lat_offset,
                Property.latitude <= float(preferences.lat) + lat_offset,
                Property.longitude >= float(preferences.long) - long_offset,
                Property.longitude <= float(preferences.long) + long_offset
            ))

        if geo_filters:
            filters.append(or_(*geo_filters))

        # 2. Status Filtering
        filters.append(or_(
            Property.home_status == 'ACTIVE-BRIGHT',
            Property.home_status == 'COMING SOON-BRIGHT'
        ))
        # Agent-entered "My Listings" rows are not real MLS inventory (see the matching filter
        # in get_properties_by_preferences).
        filters.append(Property.is_manual.is_(False))

        # 3. Numeric Filters (with Smart Exemptions)
        numeric_filters = []
        if preferences.min_price is not None and preferences.min_price > 0:
            filters.append(Property.price >= preferences.min_price)
        if preferences.max_price is not None and preferences.max_price > 0:
            filters.append(Property.price <= preferences.max_price)

        if getattr(preferences, 'min_acres', None) is not None and preferences.min_acres > 0:
            filters.append(Property.lot_size_acres >= preferences.min_acres)
        if getattr(preferences, 'max_acres', None) is not None and preferences.max_acres > 0:
            filters.append(Property.lot_size_acres <= preferences.max_acres)

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

        if getattr(preferences, 'min_sqft', None) is not None and preferences.min_sqft > 0:
            numeric_filters.append(Property.living_area >= preferences.min_sqft)
        if getattr(preferences, 'max_sqft', None) is not None and preferences.max_sqft > 0:
            numeric_filters.append(Property.living_area <= preferences.max_sqft)

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
        else:
            # No type picked = any home FOR SALE. Rentals and commercial spaces only come in when
            # the agent picks them (Rentals / Commercial) - "any" used to pull $2,100/mo rentals and
            # retail units into a buyer's Showcase (and past the beds/baths filter, as exempt types).
            filters.append(or_(Property.home_type.is_(None), Property.home_type.notin_(NOT_FOR_SALE_HOME_TYPES)))
        if not preferences.is_apartment and not preferences.is_commercial:
            # Rentals mis-tagged as homes for sale, timeshare weeks, parking spaces: nothing but land
            # lists for sale under $10k, so those never reach a buyer unless they asked for rentals.
            filters.append(or_(Property.price.is_(None), Property.price >= MIN_FOR_SALE_HOME_PRICE, Property.home_type.in_(('LAND', 'FARM'))))

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
