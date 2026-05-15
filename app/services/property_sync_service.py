from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload, joinedload
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone, timedelta
import os
import re

from app.models.database import (
    Collection, CollectionPreferences, Property, collection_properties, 
    PropertyInteraction, ScheduledEmail, Notification, SystemSettings, SchoolDistrict, 
    City, Township, Brokerage
)
from app.services.bright_mls_service import bright_mls_service
from app.utils.mls_mapper import map_reso_to_internal
from app.utils.geo import get_lat_long_offsets, is_within_distance
from app.utils.normalization import normalize_brokerage
from app.services.email_service import EmailService
from app.services.blacklist_service import BlacklistService
from app.config.logging import get_logger
from app.database import AsyncSessionLocal

logger = get_logger(__name__)

class PropertySyncService:
    """
    Global Synchronization Service.
    Syncs ALL modified properties from Bright MLS to the local mirror 
    and notifies affected collections.
    """
    def __init__(self):
        self.email_service = EmailService()
        self.SYNC_KEY = "last_property_sync_time"

    async def get_last_sync_time(self, db: AsyncSession) -> str:
        """Retrieves the last successful sync timestamp from SystemSettings."""
        stmt = select(SystemSettings).where(SystemSettings.key == self.SYNC_KEY)
        result = await db.execute(stmt)
        setting = result.scalar_one_or_none()
        
        if setting and setting.value and "timestamp" in setting.value:
            return setting.value["timestamp"]
        
        # Default fallback: 24 hours ago
        fallback = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return fallback

    async def update_last_sync_time(self, db: AsyncSession, timestamp: str):
        """Persists the last successful sync timestamp."""
        stmt = select(SystemSettings).where(SystemSettings.key == self.SYNC_KEY)
        result = await db.execute(stmt)
        setting = result.scalar_one_or_none()
        
        val = {"timestamp": timestamp}
        if setting:
            setting.value = val
        else:
            db.add(SystemSettings(key=self.SYNC_KEY, value=val))
        
        await db.commit()

    async def run_global_sync(self) -> Dict[str, Any]:
        """
        The Main Sync Loop.
        1. Fetches all changes from MLS since last run.
        2. Updates local mirror.
        3. Identifies and notifies affected collections.
        """
        logger.info("Starting Global Incremental Sync...")
        stats = {"updated": 0, "notifications_sent": 0, "errors": 0, "property_details": []}
        
        async with AsyncSessionLocal() as db:
            last_sync = await self.get_last_sync_time(db)
            logger.info(f"Syncing changes since: {last_sync}")

            skip = 0
            page_size = 200
            new_last_sync = last_sync
            had_failure = False

            while True:
                try:
                    # 1. Fetch modified batch from MLS
                    raw_properties = await bright_mls_service.get_properties_modified_since(last_sync, top=page_size, skip=skip)
                    if not raw_properties:
                        logger.info("No more modified properties found.")
                        break
                    
                    logger.info(f"Found {len(raw_properties)} modified properties in this batch (skip={skip})")

                    # 2. Batch fetch media
                    listing_keys = [str(p["ListingKey"]) for p in raw_properties]
                    photo_map = await bright_mls_service.get_media_for_properties(listing_keys)

                    # 3. Process each property
                    for raw_prop in raw_properties:
                        l_key = str(raw_prop.get("ListingKey"))
                        mod_ts = raw_prop.get("ModificationTimestamp")
                        
                        try:
                            # Perform the local update and detect events
                            sync_event = await self._sync_single_property(db, raw_prop, photo_map)
                            if sync_event:
                                stats["updated"] += 1
                                stats["property_details"].append({
                                    "listing_key": sync_event["listing_key"],
                                    "home_type": sync_event["data"]["home_type"],
                                    "event": sync_event["type"]
                                })
                                # 4. Notify affected collections
                                notified = await self._propagate_property_change(db, sync_event)
                                stats["notifications_sent"] += notified
                            
                            # Only advance the checkpoint if we haven't hit any errors yet in this run.
                            # This ensures that if a property fails, the next sync run will start
                            # from before that property and retry it.
                            if not had_failure and mod_ts and (not new_last_sync or mod_ts > new_last_sync):
                                new_last_sync = mod_ts
                                
                        except Exception as prop_error:
                            logger.error(f"Error syncing property {l_key}: {prop_error}", exc_info=True)
                            stats["errors"] += 1
                            had_failure = True
                            # Continue to next property in the batch

                    # 5. Move to next page
                    if len(raw_properties) < page_size:
                        break
                    skip += page_size

                except Exception as e:
                    logger.error(f"Error in sync batch at skip {skip}: {e}", exc_info=True)
                    stats["errors"] += 1
                    break

            # 6. Finalize sync state
            if new_last_sync != last_sync:
                await self.update_last_sync_time(db, new_last_sync)
                logger.info(f"Global Sync Complete. New Checkpoint: {new_last_sync}")

        return stats

    async def _sync_single_property(self, db: AsyncSession, raw_data: Dict[str, Any], photo_map: Dict[str, List[str]]) -> Optional[Dict[str, Any]]:
        """
        Updates local mirror and detects price drops.
        Returns a 'sync_event' dict if successful.
        """
        l_key = str(raw_data["ListingKey"])
        mapped = map_reso_to_internal(raw_data, photo_map)
        
        # --- SAFE AUTO-POPULATION OF REFERENCE TABLES ---
        state = (mapped.get("state") or "").upper().strip()

        # 1. School Districts
        sd_name = (mapped.get("school_district_name") or "").upper().strip()
        if sd_name and state:
            try:
                await db.execute(
                    insert(SchoolDistrict)
                    .values(name=sd_name, state=state)
                    .on_conflict_do_nothing()
                )
            except Exception as e:
                logger.warning(f"Failed to auto-populate school district {sd_name}: {e}")
        
        # 2. Cities
        city_name = (mapped.get("city") or "").upper().strip()
        if city_name and state:
            try:
                await db.execute(
                    insert(City)
                    .values(name=city_name, state=state)
                    .on_conflict_do_nothing()
                )
            except Exception as e:
                logger.warning(f"Failed to auto-populate city {city_name}: {e}")

        # 3. Townships (Safe + Junk Filter)
        township_name = (mapped.get("township") or "").upper().strip()
        is_junk = (
            not township_name or 
            len(township_name) <= 1 or 
            re.match(r'^[0-9.\-]+$', township_name) or 
            township_name in ['NA', 'N/A', 'NO', 'NT']
        )
        if not is_junk and state:
            try:
                await db.execute(
                    insert(Township)
                    .values(name=township_name, state=state)
                    .on_conflict_do_nothing()
                )
            except Exception as e:
                logger.warning(f"Failed to auto-populate township {township_name}: {e}")

        office_name = (mapped.get("list_office_name") or "")
        off_name, parent_name = normalize_brokerage(office_name)

        if off_name and parent_name:
            try:
                await db.execute(
                    insert(Brokerage)
                    .values(name=off_name, state=state, parent_name=parent_name)
                    .on_conflict_do_nothing()
                )
            except Exception as e:
                logger.warning(f"Failed to auto-populate brokerage {office_name} parent of {parent_name}: {e}")
        # ------------------------------------------------

        # Get existing record to compare state
        stmt = select(Property).where(Property.listing_key == l_key)
        res = await db.execute(stmt)
        existing = res.scalar_one_or_none()
        
        event_type = "UPDATE"
        old_price = None

        if existing:
            # Check for Price Drop
            if existing.price and mapped["price"] and mapped["price"] < existing.price:
                event_type = "PRICE_DROP"
                old_price = existing.price
            
            # Update all columns
            for key, value in mapped.items():
                setattr(existing, key, value)
            existing.updated_at = datetime.now(timezone.utc)
            logger.debug(f"Updated existing property: {l_key} | {mapped['street_address']} | Status: {mapped['home_status']}")
        else:
            # For NEW properties, only add them if they are ACTIVE or COMING SOON
            # This prevents our DB from filling up with old CLOSED listings we never tracked
            status = raw_data.get("MlsStatus", "")
            is_active = status.startswith("ACTIVE-BRIGHT") or status.startswith("COMING SOON")
            
            if not is_active:
                logger.debug(f"Skipping discovery for NEW inactive property: {l_key} | Status: {status}")
                return None

            # New property to our system (Global Mirror)
            event_type = "NEW_GLOBAL"
            existing = Property(**mapped)
            db.add(existing)
            logger.debug(f"Added NEW property: {l_key} | {mapped['street_address']} | Status: {mapped['home_status']}")
        
        try:
            await db.commit()
            await db.refresh(existing)
            return {
                "property_id": existing.id,
                "listing_key": l_key,
                "type": event_type,
                "old_price": old_price,
                "new_price": mapped["price"],
                "data": mapped
            }
        except Exception as e:
            logger.error(f"Failed to upsert property {l_key}: {e}")
            await db.rollback()
            return None

    async def _propagate_property_change(self, db: AsyncSession, event: Dict[str, Any]) -> int:
        """
        Identifies all active collections containing this property and schedules notifications.
        Also handles 'Discovery' for brand new properties.
        """
        prop_id = event["property_id"]
        l_key = event["listing_key"]
        count = 0

        # 1. DISCOVERY: If this is a NEW house to our system, find ALL matching collections
        if event["type"] == "NEW_GLOBAL":
            discovered_collections = await self._discover_matching_collections(db, event["data"])
            for col in discovered_collections:
                # Link property to collection (Mark as NEW by setting added_at)
                await self._link_property_to_collection(db, col.id, prop_id)
                
                # Schedule 'New Property' notification
                changes = {"new_properties": [event["data"]], "price_drops": []}
                await self._schedule_combined_notification(db, col, changes)
                count += 1

        # 2. UPDATES: Find collections that already HAVE this property (Price Drops)
        query = (
            select(Collection)
            .join(collection_properties)
            .outerjoin(PropertyInteraction, and_(
                PropertyInteraction.collection_id == Collection.id,
                PropertyInteraction.property_id == prop_id
            ))
            .options(selectinload(Collection.owner))
            .where(
                and_(
                    collection_properties.c.property_id == prop_id,
                    Collection.status == 'ACTIVE',
                    or_(
                        PropertyInteraction.id == None,
                        PropertyInteraction.disliked == False
                    )
                )
            )
        )
        result = await db.execute(query)
        existing_collections = result.scalars().all()
        
        for col in existing_collections:
            if event["type"] == "PRICE_DROP":
                # Include price change info in the data
                drop_data = {
                    **event["data"],
                    "old_price_raw": event.get("old_price"),
                    "new_price_raw": event.get("new_price")
                }
                changes = {"new_properties": [], "price_drops": [drop_data]}
                await self._schedule_combined_notification(db, col, changes, is_broadcast=True)
                count += 1
                
        await db.commit()
        return count

    async def _discover_matching_collections(self, db: AsyncSession, prop: Dict[str, Any]) -> List[Collection]:
        """
        The Matchmaker.
        High-performance query to find all active collections whose preferences 
        match this specific property.
        """
        # Extract property attributes for the reverse query
        price = prop.get("price") or 0
        beds = prop.get("bedrooms") or 0
        baths = prop.get("bathrooms") or 0
        city = prop.get("city")
        state = prop.get("state")
        township = prop.get("township")
        school_district = prop.get("school_district_name")
        h_type = prop.get("home_type")
        h_type_upper = h_type.upper() if h_type else "OTHER"
        lat = prop.get("latitude")
        lng = prop.get("longitude")

        # Reverse Query Logic
        stmt = (
            select(Collection)
            .join(CollectionPreferences)
            .options(
                selectinload(Collection.owner),
                joinedload(Collection.preferences)
            )
            .where(Collection.status == 'ACTIVE')
        )

        # 1. Geography Match (Exclusive Logic: Cities/Townships/Districts OR Radius)
        geo_conditions = []
        location_filters = []
        params = {}
        
        if city:
            params["city_name"] = city.upper()
            params["state_name"] = state.upper() if state else ""
            location_filters.append(text("""
                EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(
                        CASE 
                            WHEN jsonb_typeof(collection_preferences.cities) = 'array' 
                            THEN collection_preferences.cities 
                            ELSE '[]'::jsonb 
                        END
                    ) AS pref_city 
                    WHERE 
                        UPPER(TRIM(SPLIT_PART(pref_city, ',', 1))) = :city_name
                        AND (
                            TRIM(SPLIT_PART(pref_city, ',', 2)) = '' 
                            OR UPPER(TRIM(SPLIT_PART(pref_city, ',', 2))) = :state_name
                        )
                )
            """))
        
        if township:
            params["township_name"] = township.upper()
            params["state_name"] = state.upper() if state else ""
            location_filters.append(text("""
                EXISTS (
                    SELECT 1 
                    FROM jsonb_array_elements_text(
                        CASE 
                            WHEN jsonb_typeof(collection_preferences.townships) = 'array' 
                            THEN collection_preferences.townships 
                            ELSE '[]'::jsonb 
                        END
                    ) AS pref_town 
                    WHERE 
                        UPPER(TRIM(REGEXP_REPLACE(SPLIT_PART(pref_town, ',', 1), '\\s+(Township|Twp|Boro|Borough|City|Town)$', '', 'i'))) = :township_name
                        AND (
                            TRIM(SPLIT_PART(pref_town, ',', 2)) = '' 
                            OR UPPER(TRIM(SPLIT_PART(pref_town, ',', 2))) = :state_name
                        )
                )
            """))

        if school_district:
            params["sd_name"] = school_district.upper()
            params["sd_state"] = state.upper() if state else ""
            location_filters.append(text("""
                EXISTS (
                    SELECT 1 
                    FROM jsonb_array_elements_text(
                        CASE 
                            WHEN jsonb_typeof(collection_preferences.school_districts) = 'array' 
                            THEN collection_preferences.school_districts 
                            ELSE '[]'::jsonb 
                        END
                    ) AS sd 
                    WHERE 
                        UPPER(TRIM(SPLIT_PART(sd, ',', 1))) = :sd_name
                        AND (
                            TRIM(SPLIT_PART(sd, ',', 2)) = '' 
                            OR UPPER(TRIM(SPLIT_PART(sd, ',', 2))) = :sd_state
                        )
                )
            """))
        
        # Priority 1: If user has explicit location lists, only match those
        if location_filters:
            geo_conditions.append(or_(*location_filters))
        
        # Priority 2: Fallback to Radius only if no City/Township/District is provided
        if lat and lng:
            lat_offset, long_offset = get_lat_long_offsets(lat, 1.0) # Get offsets for 1 mile
            
            radius_condition = and_(
                # Ensure user hasn't specified other locations (The "Exclusive" part)
                or_(
                    CollectionPreferences.cities == None, 
                    and_(
                        func.jsonb_typeof(CollectionPreferences.cities) == 'array',
                        func.jsonb_array_length(CollectionPreferences.cities) == 0
                    ),
                    func.jsonb_typeof(CollectionPreferences.cities) != 'array'
                ),
                or_(
                    CollectionPreferences.townships == None, 
                    and_(
                        func.jsonb_typeof(CollectionPreferences.townships) == 'array',
                        func.jsonb_array_length(CollectionPreferences.townships) == 0
                    ),
                    func.jsonb_typeof(CollectionPreferences.townships) != 'array'
                ),
                or_(
                    CollectionPreferences.school_districts == None, 
                    and_(
                        func.jsonb_typeof(CollectionPreferences.school_districts) == 'array',
                        func.jsonb_array_length(CollectionPreferences.school_districts) == 0
                    ),
                    func.jsonb_typeof(CollectionPreferences.school_districts) != 'array'
                ),
                # Standard radius box
                CollectionPreferences.lat.isnot(None),
                CollectionPreferences.long.isnot(None),
                CollectionPreferences.diameter.isnot(None),
                func.abs(CollectionPreferences.lat - lat) <= (CollectionPreferences.diameter * lat_offset),
                func.abs(CollectionPreferences.long - lng) <= (CollectionPreferences.diameter * long_offset)
            )
            geo_conditions.append(radius_condition)
        
        if geo_conditions:
            stmt = stmt.where(or_(*geo_conditions))

        # 2. Financial & Size Match (with Smart Exemptions)
        # Types that don't always have traditional bed/bath data (Exempt from numeric filters)
        exempt_types = ['LAND', 'FARM', 'COMMERCIAL', 'OTHER']
        numeric_conditions = []

        if price > 0:
            numeric_conditions.append(and_(
                or_(CollectionPreferences.min_price == None, CollectionPreferences.min_price <= price),
                or_(CollectionPreferences.max_price == None, CollectionPreferences.max_price >= price)
            ))

        # Size Filters (apply even if property has 0 beds/baths)
        numeric_conditions.append(or_(CollectionPreferences.min_beds == None, CollectionPreferences.min_beds <= (beds or 0)))
        numeric_conditions.append(or_(CollectionPreferences.min_baths == None, CollectionPreferences.min_baths <= (baths or 0.0)))

        # Year Built Match
        year = prop.get("year_built")
        if year:
            numeric_conditions.append(and_(
                or_(CollectionPreferences.min_year_built == None, CollectionPreferences.min_year_built <= year),
                or_(CollectionPreferences.max_year_built == None, CollectionPreferences.max_year_built >= year)
            ))

        # Apply: (Is Exempt Type) OR (Matches all numeric filters)
        if h_type_upper in exempt_types:
            # If exempt, we only care about the price (if provided)
            if price > 0:
                stmt = stmt.where(and_(
                    or_(CollectionPreferences.min_price == None, CollectionPreferences.min_price <= price),
                    or_(CollectionPreferences.max_price == None, CollectionPreferences.max_price >= price)
                ))
        else:
            # If not exempt (Residential, Condo, etc.), must match all numeric filters
            stmt = stmt.where(and_(*numeric_conditions))

        # 3. Property Type Match
        # In standard search, Lot/Land includes both LAND and FARM
        type_match_conditions = []
        if h_type:
            if h_type_upper == "SINGLE_FAMILY": type_match_conditions.append(CollectionPreferences.is_single_family == True)
            elif h_type_upper == "TOWNHOUSE": type_match_conditions.append(CollectionPreferences.is_town_house == True)
            elif h_type_upper == "CONDO": type_match_conditions.append(CollectionPreferences.is_condo == True)
            elif h_type_upper == "MULTI_FAMILY": type_match_conditions.append(CollectionPreferences.is_multi_family == True)
            elif h_type_upper == "LAND": type_match_conditions.append(CollectionPreferences.is_lot_land == True)
            elif h_type_upper == "FARM": type_match_conditions.append(or_(CollectionPreferences.is_lot_land == True, CollectionPreferences.is_farm == True))
            elif h_type_upper == "COMMERCIAL": type_match_conditions.append(CollectionPreferences.is_commercial == True)
            elif h_type_upper == "RESIDENTIAL_LEASE": type_match_conditions.append(CollectionPreferences.is_apartment == True)

        if type_match_conditions:
            stmt = stmt.where(or_(*type_match_conditions))

        result = await db.execute(stmt, params)
        collections = result.scalars().all()

        # 4. Circular Post-Filtering (Trim the Corners)
        # If the property has lat/long, ensure it's precisely within the radius of matching collections
        if lat and lng:
            refined_collections = []
            for col in collections:
                # If collection has a radius search, verify exact distance
                if col.preferences and col.preferences.lat and col.preferences.long and col.preferences.diameter:
                    # Skip circular check if collection matches via City/Township (inclusive logic)
                    matches_via_city = False
                    if city and col.preferences.cities:
                        city_names = [c.split(',')[0].strip().upper() for c in col.preferences.cities]
                        if city.upper() in city_names:
                            matches_via_city = True
                    
                    if not matches_via_city and township and col.preferences.townships:
                        # Simple check for townships
                        town_names = [t.split(',')[0].strip().upper() for t in col.preferences.townships]
                        if township.upper() in town_names:
                            matches_via_city = True

                    if matches_via_city:
                        refined_collections.append(col)
                    else:
                        # Must match via Radius - perform precise check
                        if is_within_distance(
                            col.preferences.lat, col.preferences.long, 
                            lat, lng, 
                            col.preferences.diameter
                        ):
                            refined_collections.append(col)
                else:
                    # Match via other criteria, keep it
                    refined_collections.append(col)
            
            return refined_collections

        return collections

    async def _link_property_to_collection(self, db: AsyncSession, collection_id: str, property_id: str):
        """Silently links a property to a collection if not already linked."""
        stmt = select(collection_properties).where(
            and_(
                collection_properties.c.collection_id == collection_id,
                collection_properties.c.property_id == property_id
            )
        )
        existing = await db.execute(stmt)
        if not existing.fetchone():
            await db.execute(insert(collection_properties).values(
                collection_id=collection_id,
                property_id=property_id,
                added_at=datetime.now(timezone.utc) # Mark as NEW
            ))

    def _format_price_compact(self, price: float) -> str:
        """Formats price into a compact string like $750K or $2.1M"""
        if not price:
            return ""
        if price >= 1000000:
            val = price / 1000000
            # If it's a whole number, don't show decimal
            if val == int(val):
                return f"${int(val)}M"
            return f"${val:.1f}M"
        if price >= 1000:
            return f"${int(price / 1000)}K"
        return f"${int(price)}"

    def _generate_subject(self, featured: Dict[str, Any], template: str) -> str:
        """Generates a dynamic subject line to avoid spam filters"""
        import random
        
        price_raw = featured.get('price_raw') or featured.get('price') or 0
        price_compact = self._format_price_compact(float(price_raw))
        city = featured.get('city', '')
        street = featured.get('street_address', '')
        
        if template == "price_drop_alert":
            # For price drops, we want to be clear
            subjects = [
                f"Price drop: {street} in {city} is now {price_compact}",
                f"Great news: Price drop on {street}!",
                f"Price updated for {street}: {price_compact}",
                f"Price reduced for the home in {city}: {street}"
            ]
            return random.choice(subjects)
        
        # New Listing styles
        subjects = [
            f"{price_compact} listing just came up in {city}",
            f"New {price_compact} home in {city} you might like",
            f"New listing: {street} in {city} for {price_compact}",
            f"{street} just hit the market in {city} for {price_compact}"
        ]
        return random.choice(subjects)

    async def _schedule_combined_notification(self, db: AsyncSession, collection: Collection, changes: Dict[str, Any], is_broadcast: bool = False):
        """Schedules emails and in-app alerts for a collection."""
        frontend_url = os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'http://localhost:3000'))
        new_count = len(changes.get("new_properties", []))
        drop_count = len(changes.get("price_drops", []))
        
        if new_count == 0 and drop_count == 0:
            return

        featured = (changes["new_properties"] + changes["price_drops"])[0]
        
        # Total property count helper
        total_count_query = select(func.count()).select_from(collection_properties).where(collection_properties.c.collection_id == collection.id)
        total_count_res = await db.execute(total_count_query)
        total_count = total_count_res.scalar() or 0

        # Create full address string
        full_address = f"{featured.get('street_address')}, {featured.get('city')}, {featured.get('state')} {featured.get('zipcode', '')}".strip()

        common_vars = {
            "collection_name": collection.name,
            "visitor_name": collection.visitor_name or "Valued Visitor",
            "new_count": new_count,
            "drop_count": drop_count,
            "total_count": total_count,
            "property_address": full_address,
            "property_image": featured.get('img_src'),
            "property_price": f"${featured.get('price', 0):,}",
            "property_beds": featured.get('bedrooms'),
            "property_baths": featured.get('bathrooms'),
            "property_sqft": featured.get('living_area'),
            "today_date": datetime.now(timezone.utc).strftime("%m/%d/%Y")
        }

        # 1. Visitor Email
        if collection.visitor_email and getattr(collection, 'notify_visitor', True):
            # Check if visitor is blacklisted
            if await BlacklistService.is_blacklisted(db, collection.visitor_email):
                logger.info(f"Skipping scheduled email for blacklisted visitor: {collection.visitor_email}")
            else:
                template = "price_drop_alert" if (is_broadcast or (drop_count > 0 and new_count == 0)) else "new_properties_synced"
                
                # Calculate price drop variables if applicable
                old_p = featured.get("old_price_raw")
                new_p = featured.get("new_price_raw")
                savings = 0
                if old_p and new_p:
                    savings = old_p - new_p

                visitor_vars = {
                    **common_vars,
                    "collection_link": f"{frontend_url}/showcase/{collection.share_token}",
                    "recipient_name": collection.visitor_name or "Valued Visitor",
                    "agent_name": f"{collection.owner.first_name} {collection.owner.last_name}" if collection.owner else "Your Agent",
                    "agent_email": collection.owner.email if collection.owner else "",
                    "agent_phone": getattr(collection.owner, 'phone', "") if collection.owner else "",
                    "old_price": f"${old_p:,.0f}" if old_p else None,
                    "new_price": f"${new_p:,.0f}" if new_p else None,
                    "savings": f"${savings:,.0f}" if savings > 0 else None,
                    "Unsub": f"{frontend_url}/unsubscribe?email={collection.visitor_email}"
                }
                
                subject = self._generate_subject(featured, template)
                
                db.add(ScheduledEmail(
                    recipient_email=collection.visitor_email,
                    subject=subject,
                    template_name=template,
                    template_variables=visitor_vars,
                    status="PENDING",
                    scheduled_for=datetime.now(timezone.utc)
                ))

        # 2. Agent Email
        if collection.owner and collection.owner.email and getattr(collection, 'notify_agent', True):
            agent_vars = {
                **common_vars, 
                "collection_link": f"{frontend_url}/showcases?showcase={collection.id}",
                "recipient_name": collection.owner.first_name
            }
            db.add(ScheduledEmail(
                recipient_email=collection.owner.email,
                subject=f"Showcase Updated: {collection.visitor_name or 'Visitor'} - {collection.name}",
                template_name="new_properties_synced_agent",
                template_variables=agent_vars,
                status="PENDING",
                scheduled_for=datetime.now(timezone.utc)
            ))

            # 3. In-App Notification
            try:
                if new_count > 0 and drop_count > 0:
                    title = f"Showcase Update: {collection.visitor_name or 'Visitor'}"
                    message = f"Found {new_count} new and {drop_count} price drop{'s' if drop_count > 1 else ''} for {collection.name}."
                elif new_count > 0:
                    title = f"New Property Match: {collection.visitor_name or 'Visitor'}"
                    message = f"Found {new_count} new property match{'es' if new_count > 1 else ''} for {collection.name}."
                else:
                    title = f"Price Drop Alert: {collection.visitor_name or 'Visitor'}"
                    message = f"Found {drop_count} price drop{'s' if drop_count > 1 else ''} for {collection.name}."

                prop_id_query = select(Property.id).where(Property.listing_key == str(featured.get('listing_key')))
                prop_id = (await db.execute(prop_id_query)).scalar()

                db.add(Notification(
                    agent_id=collection.owner_id,
                    type="PROPERTY_SYNC_UPDATE",
                    reference_type="VISITOR",
                    reference_id=collection.id,
                    title=title,
                    message=message,
                    collection_id=collection.id,
                    collection_name=collection.name,
                    property_id=prop_id,
                    property_address=featured.get('street_address'),
                    visitor_name=collection.visitor_name,
                    link=f"/showcases?showcase={collection.id}" + (f"&property={prop_id}" if prop_id else ""),
                    is_read=False,
                    created_at=datetime.now(timezone.utc)
                ))
            except Exception as e:
                logger.error(f"In-app notification failed: {e}")

    async def cleanup_off_market_properties(self):
        """
        Optional task: Run once a week to mark properties as OFF_MARKET 
        if they haven't been modified in Bright MLS for a long time.
        """
        pass
