from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, and_, or_, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import selectinload
from typing import List, Dict, Any, Optional, Set
import asyncio
import json
import uuid
import math
from datetime import datetime, timezone, timedelta
import os

from app.models.database import (
    Collection, CollectionPreferences, Property, collection_properties, 
    User, PropertyInteraction, PropertyComment, PropertyTour, 
    ScheduledEmail, Notification, SystemSettings, SchoolDistrict
)
from app.services.bright_mls_service import bright_mls_service
from app.services.email_service import EmailService
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

            while True:
                try:
                    # 1. Fetch modified batch from MLS
                    raw_properties = await bright_mls_service.get_properties_modified_since(last_sync, top=page_size, skip=skip)
                    if not raw_properties:
                        break

                    # 2. Batch fetch media
                    listing_keys = [str(p["ListingKey"]) for p in raw_properties]
                    photo_map = await bright_mls_service.get_media_for_properties(listing_keys)

                    # 3. Process each property
                    for raw_prop in raw_properties:
                        l_key = str(raw_prop["ListingKey"])
                        mod_ts = raw_prop.get("ModificationTimestamp")
                        
                        # Track the latest modification timestamp found
                        if mod_ts and mod_ts > new_last_sync:
                            new_last_sync = mod_ts

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
        mapped = bright_mls_service.map_reso_to_internal(raw_data, photo_map)
        
        # --- NEW: Maintain School Districts Reference Table ---
        sd_name = mapped.get("school_district_name")
        sd_state = mapped.get("state")
        if sd_name and sd_state:
            try:
                # Upsert school district into reference table
                await db.execute(
                    insert(SchoolDistrict)
                    .values(name=sd_name, state=sd_state)
                    .on_conflict_do_nothing()
                )
            except Exception as e:
                logger.warning(f"Failed to auto-populate school district {sd_name}: {e}")
        # ------------------------------------------------------

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
        else:
            # New property to our system (Global Mirror)
            event_type = "NEW_GLOBAL"
            existing = Property(**mapped)
            db.add(existing)
        
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
                changes = {"new_properties": [], "price_drops": [event["data"]]}
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
        lat = prop.get("latitude")
        lng = prop.get("longitude")

        # Reverse Query Logic
        stmt = (
            select(Collection)
            .join(CollectionPreferences)
            .options(selectinload(Collection.owner))
            .where(Collection.status == 'ACTIVE')
        )

        # 1. Geography Match (City OR Township OR Radius)
        geo_conditions = []
        params = {}
        
        if city and state:
            city_state = f"{city}, {state}"
            params["city_state"] = city_state
            geo_conditions.append(
                text("EXISTS (SELECT 1 FROM jsonb_array_elements_text(collection_preferences.cities) AS pref_city WHERE pref_city ILIKE :city_state)")
            )
        
        if township:
            # On-the-fly normalization of user preferences in SQL:
            # 1. Clean Name: SPLIT_PART(..., ',', 1) + REGEXP_REPLACE (removes state and suffixes)
            # 2. Extract State: TRIM(SPLIT_PART(..., ',', 2))
            # 3. Match: Both name and state (if state exists in pref) must match the property
            params["township_name"] = township.upper()
            params["state_name"] = state.upper() if state else ""
            geo_conditions.append(
                text("""
                    EXISTS (
                        SELECT 1 
                        FROM jsonb_array_elements_text(collection_preferences.townships) AS pref_town 
                        WHERE 
                            UPPER(TRIM(REGEXP_REPLACE(SPLIT_PART(pref_town, ',', 1), '\\s+(Township|Twp|Boro|Borough|City|Town)$', '', 'i'))) = :township_name
                            AND (
                                TRIM(SPLIT_PART(pref_town, ',', 2)) = '' 
                                OR UPPER(TRIM(SPLIT_PART(pref_town, ',', 2))) = :state_name
                            )
                    )
                """)
            )

        if school_district:
            # Match school district name and state by splitting the user's preference string
            # Format in DB: "RADNOR TOWNSHIP, PA"
            # Property attributes: school_district="RADNOR TOWNSHIP", state="PA"
            params["sd_name"] = school_district.upper()
            params["sd_state"] = state.upper() if state else ""
            geo_conditions.append(
                text("""
                    EXISTS (
                        SELECT 1 
                        FROM jsonb_array_elements_text(collection_preferences.school_districts) AS sd 
                        WHERE 
                            UPPER(TRIM(SPLIT_PART(sd, ',', 1))) = :sd_name
                            AND (
                                TRIM(SPLIT_PART(sd, ',', 2)) = '' 
                                OR UPPER(TRIM(SPLIT_PART(sd, ',', 2))) = :sd_state
                            )
                    )
                """)
            )
        
        # Radius Match is checked only if no City/Township matches found OR if we want them to combine
        # Based on search logic, Radius is a fallback, but here we combine them into the same OR block for maximum discovery
        if lat and lng:
            lat_deg_per_mile = 1.0 / 69.1
            lng_deg_per_mile = 1.0 / (69.1 * math.cos(math.radians(lat)))
            
            geo_conditions.append(and_(
                CollectionPreferences.lat.isnot(None),
                CollectionPreferences.long.isnot(None),
                CollectionPreferences.diameter.isnot(None),
                func.abs(CollectionPreferences.lat - lat) <= (CollectionPreferences.diameter * 0.5 * lat_deg_per_mile),
                func.abs(CollectionPreferences.long - lng) <= (CollectionPreferences.diameter * 0.5 * lng_deg_per_mile)
            ))
        
        if geo_conditions:
            stmt = stmt.where(or_(*geo_conditions))

        # 2. Financial Match
        if price > 0:
            stmt = stmt.where(and_(
                or_(CollectionPreferences.min_price == None, CollectionPreferences.min_price <= price),
                or_(CollectionPreferences.max_price == None, CollectionPreferences.max_price >= price)
            ))

        # 3. Year Built Match
        year = prop.get("year_built")
        if year:
            stmt = stmt.where(and_(
                or_(CollectionPreferences.min_year_built == None, CollectionPreferences.min_year_built <= year),
                or_(CollectionPreferences.max_year_built == None, CollectionPreferences.max_year_built >= year)
            ))

        # 4. Size Match
        if beds > 0:
            stmt = stmt.where(or_(CollectionPreferences.min_beds == None, CollectionPreferences.min_beds <= beds))
        if baths > 0:
            stmt = stmt.where(or_(CollectionPreferences.min_baths == None, CollectionPreferences.min_baths <= baths))

        # 5. Property Type Match
        # In standard search, Lot/Land includes both LAND and FARM
        type_match_conditions = []
        if h_type:
            h_type_upper = h_type.upper()
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
        return result.scalars().all()

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

    async def _schedule_combined_notification(self, db: AsyncSession, collection: Collection, changes: Dict[str, Any], is_broadcast: bool = False):
        """Schedules emails and in-app alerts for a collection."""
        frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')
        new_count = len(changes.get("new_properties", []))
        drop_count = len(changes.get("price_drops", []))
        
        if new_count == 0 and drop_count == 0:
            return

        featured = (changes["new_properties"] + changes["price_drops"])[0]
        
        # Total property count helper
        total_count_query = select(func.count()).select_from(collection_properties).where(collection_properties.c.collection_id == collection.id)
        total_count_res = await db.execute(total_count_query)
        total_count = total_count_res.scalar() or 0

        common_vars = {
            "collection_name": collection.name,
            "visitor_name": collection.visitor_name or "Valued Visitor",
            "collection_link": f"{frontend_url}/showcase/{collection.share_token}",
            "new_count": new_count,
            "drop_count": drop_count,
            "total_count": total_count,
            "property_address": featured.get('street_address'),
            "property_image": featured.get('img_src'),
            "property_price": f"${featured.get('price', 0):,}",
            "property_beds": featured.get('bedrooms'),
            "property_baths": featured.get('bathrooms'),
            "property_sqft": featured.get('living_area'),
            "today_date": datetime.now().strftime("%m/%d/%Y")
        }

        # 1. Visitor Email
        if collection.visitor_email:
            template = "price_drop_alert" if (is_broadcast or (drop_count > 0 and new_count == 0)) else "new_properties_synced"
            visitor_vars = {
                **common_vars,
                "recipient_name": collection.visitor_name or "Valued Visitor",
                "agent_name": f"{collection.owner.first_name} {collection.owner.last_name}" if collection.owner else "Your Agent",
                "agent_email": collection.owner.email if collection.owner else "",
                "agent_phone": getattr(collection.owner, 'phone', "") if collection.owner else ""
            }
            db.add(ScheduledEmail(
                recipient_email=collection.visitor_email,
                subject=f"Updates for your showcase: {collection.name}",
                template_name=template,
                template_variables=visitor_vars,
                status="PENDING",
                scheduled_for=datetime.now(timezone.utc)
            ))

        # 2. Agent Email
        if collection.owner and collection.owner.email:
            agent_vars = {**common_vars, "recipient_name": collection.owner.first_name}
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
                summary = f"{drop_count} price drop{'s' if drop_count > 1 else ''}"
                prop_id_query = select(Property.id).where(Property.listing_key == str(featured.get('listing_key')))
                prop_id = (await db.execute(prop_id_query)).scalar()

                db.add(Notification(
                    agent_id=collection.owner_id,
                    type="PROPERTY_SYNC_UPDATE",
                    reference_type="VISITOR",
                    reference_id=collection.id,
                    title=f"Price Drop Alert: {collection.visitor_name or 'Visitor'}",
                    message=f"Found {summary} for {collection.name}.",
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
