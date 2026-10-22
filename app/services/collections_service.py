from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, insert, delete, text, func, update
from app.services.showcase_links import share_match
from sqlalchemy.orm import selectinload, joinedload, load_only
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
import uuid
import secrets
import string
import os

from app.models.database import (
    Collection, Property, User, PropertyInteraction,
    PropertyComment, PropertyTour, collection_properties,
    Notification, ScheduledEmail, CollectionPreferences, AgentForm, FormSubmission, LeadEvent
)
from app.schemas.collection import CollectionCreate
from app.services.areas import geography
from app.services.mls_open_houses import open_houses_payload
from app.schemas.collection_preferences import CollectionPreferencesCreate, CollectionPreferencesUpdate
from app.config.logging import get_logger

from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.blacklist_service import BlacklistService
from app.services.leads_service import upsert_lead
from app.services.website.content_adapters import get_agent_profile
from app.utils.listing_dom import dom_fetched_at, resolve_days_on_market

logger = get_logger(__name__)

_AVAILABLE_STATUSES = {'active', 'activebright', 'comingsoon', 'comingsoonbright'}


def _is_property_available(home_status: Optional[str]) -> bool:
    """Same Active/Coming Soon rule the frontend uses (isPropertyAvailableStatus) - anything
    else (Sold, Under Contract, Pending, Withdrawn, etc.) can't be bought anymore."""
    if not home_status:
        return True
    normalized = ''.join(ch for ch in home_status.lower() if ch.isalnum())
    return normalized in _AVAILABLE_STATUSES


# Sold/Removed folder (user, 10/21): a home that goes under contract / pending / sold / off the
# market no longer moves there by itself - it stays in the list with a notice until the buyer or
# the agent taps "Move to Sold/Removed" (collection_properties.sold_filed_at). The folder holds the
# filed homes that are still off the market; one back on the market shows normally again.
def sold_folder_split(props, filed_at_lookup: Dict[str, Any]) -> tuple:
    """(ids in the Sold/Removed folder, filed ids that are for sale again). A filed home back on
    the market shows normally - and loses its filing (_unfile_back_on_market), so if it later goes
    off the market again it gets a fresh notice instead of vanishing into the folder."""
    filed, back_on_market = set(), []
    for p in props:
        if filed_at_lookup.get(p.id) is None:
            continue
        if _is_property_available(p.home_status):
            back_on_market.append(p.id)
        else:
            filed.add(p.id)
    return filed, back_on_market


async def _unfile_back_on_market(db: AsyncSession, collection_id: str, property_ids: List[str]) -> None:
    """One small write, only when a filed home came back. Called after the payload is built."""
    if not property_ids:
        return
    try:
        await db.execute(update(collection_properties).where(and_(
            collection_properties.c.collection_id == collection_id,
            collection_properties.c.property_id.in_(property_ids),
        )).values(sold_filed_at=None))
        await db.commit()
    except Exception:  # noqa: BLE001 - never break loading the Showcase over it
        logger.warning("clearing Sold/Removed for homes back on the market failed", exc_info=True)
        await db.rollback()


def _sale_fields(prop: Property) -> Dict[str, Any]:
    """Sold price + date for a closed home's notice (Bright fills these only once it's closed)."""
    if 'closed' not in str(prop.home_status or '').lower():
        return {'ClosePrice': None, 'CloseDate': None}
    raw = prop.raw_mls_data or {}
    try:
        close_price = float(raw.get('ClosePrice')) if raw.get('ClosePrice') not in (None, '') else None
    except (TypeError, ValueError):
        close_price = None
    return {'ClosePrice': close_price, 'CloseDate': raw.get('CloseDate') or None}


async def _resolve_original_property_data(db: AsyncSession, collection: Collection) -> Optional[Dict[str, Any]]:
    """The "compare against" property used both for the "Curated from {address}" line and the
    match-score badge (frontend/lib/matchScore.ts) - the agent's explicit
    compare_property_listing_key override if one is set, otherwise whatever property the linked
    OpenHouseEvent was created for. A real Property lookup (works for sold listings too - see
    PropertyService.get_property_by_listing_key, which never filters by status) gives the richest
    data (lat/lng, school district, exact acreage); the OpenHouseEvent's own denormalized snapshot
    is the fallback when there's no resolvable listing_key at all.
    """
    override_key = collection.preferences.compare_property_listing_key if collection.preferences else None
    open_house = collection.original_open_house_event if collection.original_open_house_event_id else None
    effective_key = override_key or (open_house.listing_key if open_house else None)

    if effective_key:
        # Local import to avoid a circular import at module load time (property_service imports
        # from areas/geography, which doesn't touch collections - but keep this defensive).
        from app.services.property_service import PropertyService
        try:
            resolved = await PropertyService.get_property_by_listing_key(db, effective_key)
        except Exception:
            resolved = None
        if resolved:
            return {
                "id": resolved.id,
                "listingKey": resolved.listing_key,
                "address": resolved.street_address or "Unknown Address",
                "city": resolved.city,
                "state": resolved.state,
                "zipCode": resolved.zipcode,
                "price": resolved.price,
                "beds": resolved.bedrooms,
                "baths": resolved.bathrooms,
                "squareFeet": resolved.living_area,
                "acres": resolved.lot_size_acres,
                "schoolDistrictName": resolved.school_district_name,
                "latitude": resolved.latitude,
                "longitude": resolved.longitude,
                "image": resolved.img_src,
                "homeStatus": resolved.home_status,
                "propertyType": resolved.home_type or "Unknown",
                # The home they actually toured (None if the Showcase didn't come from an open
                # house) - differs from listingKey once the search is built around another home.
                "touredListingKey": open_house.listing_key if open_house else None,
            }

    if open_house:
        # Fallback: the OpenHouseEvent's own snapshot fields (no extra query - already loaded).
        # No school district or acreage available here, so those two match-score factors just
        # contribute 0 for these - the frontend's scoring already degrades gracefully for that.
        return {
            "id": open_house.id,
            "listingKey": open_house.listing_key,
            "address": open_house.address or "Unknown Address",
            "city": open_house.city,
            "state": open_house.state,
            "zipCode": open_house.zipcode,
            "price": open_house.price,
            "beds": open_house.bedrooms,
            "baths": open_house.bathrooms,
            "squareFeet": open_house.living_area,
            "latitude": open_house.latitude,
            "longitude": open_house.longitude,
            "image": open_house.cover_image_url,
            "propertyType": open_house.house_type or "Unknown",
            "touredListingKey": open_house.listing_key,
        }

    return None


# NEW tags - one set of rules everywhere:
#  - the agent's Showcases page card: a home matched in since the agent last opened that Showcase
#    (opening it sets last_agent_dismissed_at, same as the card's "N NEW x");
#  - inside the Showcase (agent): matched in within NEW_PROPERTY_DAYS and the agent hasn't opened it;
#  - the buyer's page: within NEW_PROPERTY_DAYS and the buyer hasn't opened it.
# Homes from the first fill (in within 10 minutes of making the Showcase) aren't "new matches" for
# the agent - they picked them; for the buyer everything is new in the Showcase's first week.
FIRST_FILL = timedelta(minutes=10)


def _utc(d: Optional[datetime]) -> Optional[datetime]:
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


def new_window() -> timedelta:
    return timedelta(days=int(os.getenv("NEW_PROPERTY_DAYS", "7")))


def matched_in_at(collection: Collection, added_at: Optional[datetime]) -> Optional[datetime]:
    """When this home came in as a NEW match - None for the Showcase's first fill."""
    a, c = _utc(added_at), _utc(collection.created_at)
    if a is None or (c is not None and a <= c + FIRST_FILL):
        return None
    return a


def card_new_since(collection: Collection) -> datetime:
    return _utc(collection.last_agent_dismissed_at or collection.created_at)


class CollectionsService:

    @staticmethod
    def count_new_properties(collection: Collection) -> int:
        """The card's NEW count (matched in since the agent last opened the Showcase) for Leads/AI.
        Needs collection._added_at ({property_id: added_at}) - lead_routes._fetch_owner_collections sets it."""
        added = getattr(collection, '_added_at', None) or {}
        since = card_new_since(collection)
        return sum(1 for p in (collection.properties or [])
                   if (m := matched_in_at(collection, added.get(p.id))) is not None and m > since)

    @staticmethod
    def _calculate_dom(
        mls_list_date: Optional[datetime],
        mls_days_on_market: Optional[int] = None,
        status: Optional[str] = None,
        fetched_at: Optional[datetime] = None,
    ) -> Optional[int]:
        return resolve_days_on_market(mls_days_on_market, status, mls_list_date, fetched_at=fetched_at)

    @staticmethod
    async def count_active_collections(db: AsyncSession, user_id: str) -> int:
        """Count the number of active collections for a user"""
        try:
            result = await db.execute(
                select(func.count(Collection.id)).where(
                    and_(
                        Collection.owner_id == user_id,
                        Collection.status == 'ACTIVE'
                    )
                )
            )
            return result.scalar() or 0
        except Exception as e:
            logger.error("Failed to count active collections", extra={"error": str(e)})
            return 0

    @staticmethod
    async def should_create_as_active(db: AsyncSession, user_id: str) -> bool:
        """Check if a new collection should be created as active (unlimited)"""
        return True

    @staticmethod
    async def can_activate_collection(db: AsyncSession, user_id: str, collection_id: str) -> bool:
        """Check if a collection can be activated (unlimited)"""
        return True

    @staticmethod
    async def get_user_collections(db: AsyncSession, user_id: str) -> List[Dict[str, Any]]:
        """Get all collections for a user with total and active property counts"""
        try:
            query = (
                select(Collection)
                .options(
                    selectinload(Collection.preferences),
                    selectinload(Collection.properties),
                    selectinload(Collection.original_open_house_event),
                    selectinload(Collection.property_interactions),
                )
                .where(Collection.owner_id == user_id)
                .order_by(Collection.created_at.desc())
            )

            result = await db.execute(query)
            collections = result.scalars().all()

            # Batch-fetch when each property was added to its collection (collection.properties
            # is a plain many-to-many relationship, so the association row's added_at isn't
            # reachable through the ORM objects) - one query for every collection up front,
            # instead of N+1 per collection, mirrors the lookup in get_properties().
            collection_ids = [c.id for c in collections]
            added_at_lookup: Dict[tuple, Any] = {}
            if collection_ids:
                added_at_result = await db.execute(
                    select(
                        collection_properties.c.collection_id,
                        collection_properties.c.property_id,
                        collection_properties.c.added_at,
                    ).where(collection_properties.c.collection_id.in_(collection_ids))
                )
                for row in added_at_result.all():
                    added_at_lookup[(row.collection_id, row.property_id)] = row.added_at

            # Unread visitor-activity notifications per collection, split into two signals:
            # tour requests get their own dedicated count-badge marker (see below), while
            # like/dislike/comment notifications feed static per-type count badges on the
            # dashboard card - same "icon + count, no animation" style as the tour marker,
            # shown together rather than cycling through one at a time.
            activity_counts_lookup: Dict[str, Dict[str, int]] = {}
            tour_requested_lookup: Dict[str, int] = {}
            comment_property_lookup: Dict[str, str] = {}
            if collection_ids:
                # The tour marker = tours still waiting on the agent (not confirmed, not marked
                # handled) - NOT the unread alert, so just opening the tours window can't clear it.
                pending_tours = await db.execute(
                    select(PropertyTour.collection_id, func.count(PropertyTour.id))
                    .where(
                        PropertyTour.collection_id.in_(collection_ids),
                        PropertyTour.confirmed_date.is_(None),
                        PropertyTour.is_completed.is_(False),
                    )
                    .group_by(PropertyTour.collection_id)
                )
                tour_requested_lookup = {cid: n for cid, n in pending_tours.all()}
                notif_result = await db.execute(
                    select(Notification)
                    .where(
                        Notification.agent_id == user_id,
                        Notification.collection_id.in_(collection_ids),
                        Notification.type.in_(['PROPERTY_INTERACTION', 'PROPERTY_COMMENT']),
                        Notification.is_read.is_(False),
                    )
                    .order_by(Notification.created_at.desc())
                )
                for n in notif_result.scalars().all():
                    if n.type == 'PROPERTY_COMMENT' and n.property_id and n.collection_id not in comment_property_lookup:
                        # Newest unread comment - the card's comment badge opens that home's thread.
                        comment_property_lookup[n.collection_id] = n.property_id
                    if n.type == 'PROPERTY_COMMENT':
                        activity_type = 'PROPERTY_COMMENT'
                    elif n.type == 'PROPERTY_INTERACTION' and n.message and n.message.startswith('Liked'):
                        activity_type = 'LIKE'
                    elif n.type == 'PROPERTY_INTERACTION' and n.message and n.message.startswith('Disliked'):
                        activity_type = 'DISLIKE'
                    else:
                        continue
                    counts = activity_counts_lookup.setdefault(n.collection_id, {})
                    counts[activity_type] = counts.get(activity_type, 0) + 1

            # Each client's email setting (daily / weekly / off + why) for the card's email badge.
            from app.models.database import ClientEmailPref
            email_prefs = {p.email: p for p in (await db.execute(
                select(ClientEmailPref).where(ClientEmailPref.owner_id == user_id))).scalars().all()}

            collections_data = []
            for collection in collections:
                # Calculate total vs active (non-disliked) properties
                total_count = len(collection.properties) if collection.properties else 0
                
                disliked_property_ids = {
                    interaction.property_id 
                    for interaction in collection.property_interactions 
                    if interaction.disliked
                }
                
                active_count = 0
                new_listings_count = 0

                # NEW on the card = matched in since the agent last opened this Showcase (see FIRST_FILL notes).
                dismiss_reference = card_new_since(collection)

                # Properties the agent has individually opened - clears that one property's NEW
                # tag (and the count) even if the collection-wide dismiss hasn't been clicked.
                agent_viewed_ids = {
                    interaction.property_id
                    for interaction in collection.property_interactions
                    if interaction.agent_viewed_at
                }

                if collection.properties:
                    for p in collection.properties:
                        # "Active" here means still a real, purchasable match: not disliked and
                        # not sold/removed - the single count shown on the dashboard card.
                        if p.id not in disliked_property_ids and _is_property_available(p.home_status):
                            active_count += 1

                        m_in = matched_in_at(collection, added_at_lookup.get((collection.id, p.id)))
                        if m_in is not None and m_in > dismiss_reference and p.id not in agent_viewed_ids:
                            new_listings_count += 1

                try:
                    original_property_data = await _resolve_original_property_data(db, collection)
                except Exception:
                    original_property_data = None

                preferences_data = {}
                if collection.preferences:
                    preferences_data = {
                        "min_beds": collection.preferences.min_beds,
                        "max_beds": collection.preferences.max_beds,
                        "min_baths": collection.preferences.min_baths,
                        "max_baths": collection.preferences.max_baths,
                        "min_price": collection.preferences.min_price,
                        "max_price": collection.preferences.max_price,
                        "min_year_built": collection.preferences.min_year_built,
                        "max_year_built": collection.preferences.max_year_built,
                        "lat": collection.preferences.lat,
                        "long": collection.preferences.long,
                        "address": collection.preferences.address,  # Add missing address field
                        "cities": collection.preferences.cities,
                        "townships": collection.preferences.townships,
                        "school_districts": collection.preferences.school_districts,
                        "diameter": collection.preferences.diameter,
                        "special_features": collection.preferences.special_features,
                        "visiting_reason": collection.preferences.visiting_reason,
                        "has_agent": collection.preferences.has_agent,
                        "compare_property_listing_key": collection.preferences.compare_property_listing_key,
                        "show_match_score": collection.preferences.show_match_score,
                    # The editor reopens with everything the agent set - leaving these out made
                    # the drawn area, counties, size and lot settings look empty, and the next Save
                    # quietly erased them.
                    "search_polygon": collection.preferences.search_polygon,
                    "counties": collection.preferences.counties,
                    "county": collection.preferences.county,
                    "state": collection.preferences.state,
                    "min_sqft": collection.preferences.min_sqft,
                    "max_sqft": collection.preferences.max_sqft,
                    "min_acres": collection.preferences.min_acres,
                    "max_acres": collection.preferences.max_acres,

                        "is_town_house": collection.preferences.is_town_house,
                        "is_condo": collection.preferences.is_condo,
                        "is_single_family": collection.preferences.is_single_family,
                        "is_lot_land": collection.preferences.is_lot_land,
                        "is_multi_family": collection.preferences.is_multi_family,
                        "is_apartment": collection.preferences.is_apartment,
                        "is_commercial": collection.preferences.is_commercial,
                        "is_farm": collection.preferences.is_farm
                    }

                # Transform properties for this collection (similar to get_shared_collection)
                properties_data = []
                for prop in collection.properties:
                    mls_date = prop.mls_list_date
                    if mls_date and mls_date.tzinfo is None:
                        mls_date = mls_date.replace(tzinfo=timezone.utc)
                    added_at = added_at_lookup.get((collection.id, prop.id))
                    if added_at and added_at.tzinfo is None:
                        added_at = added_at.replace(tzinfo=timezone.utc)
                    property_dict = {
                        'id': prop.id,
                        'ListingKey': prop.listing_key,
                        'open_houses': open_houses_payload(prop),
                        'FullStreetAddress': prop.street_address or 'Unknown Address',
                        'City': prop.city,
                        'StateOrProvince': prop.state,
                        'PostalCode': prop.zipcode,
                        'ListPrice': prop.price,
                        'BedroomsTotal': prop.bedrooms,
                        'BathroomsTotal': prop.bathrooms,
                        'LivingArea': prop.living_area,
                        'LotSizeSquareFeet': prop.lot_size,
                        'LotSizeAcres': prop.lot_size_acres,
                        'SchoolDistrictName': prop.school_district_name,
                        'Latitude': prop.latitude,
                        'Longitude': prop.longitude,
                        'PropertyType': prop.home_type,
                        'YearBuilt': prop.year_built,
                        'DaysOnMarket': CollectionsService._calculate_dom(
                            prop.mls_list_date, prop.days_on_market, prop.home_status, dom_fetched_at(prop),
                        ),
                        'AssociationYN': prop.has_association,
                        'ListPictureURL': prop.img_src,
                        'PublicRemarks': '',
                        'ModificationTimestamp': prop.modification_timestamp.isoformat() if prop.modification_timestamp else (prop.updated_at.isoformat() if prop.updated_at else None),
                        'PriceChangeTimestamp': prop.price_change_timestamp.isoformat() if prop.price_change_timestamp else None,
                        'MlsStatus': prop.home_status,
                        # Same rule as stats.newProperties - lets the card photos show exactly
                        # which homes make up that count.
                        'MLSListDate': mls_date.isoformat() if mls_date else None,
                        'is_new': bool((m_in := matched_in_at(collection, added_at)) and m_in > dismiss_reference and prop.id not in agent_viewed_ids),
                        # When this property was matched into the showcase (NULL for the
                        # properties the collection started with) - drives "most recently
                        # added" ordering on the dashboard card preview.
                        'AddedToCollectionAt': added_at.isoformat() if added_at else None,
                    }
                    properties_data.append(property_dict)

                # Convert to response format
                is_blacklisted = False
                if collection.visitor_email:
                    is_blacklisted = await BlacklistService.is_blacklisted(db, collection.visitor_email)
                    
                collection_data = {
                    "id": collection.id,
                    "name": collection.name,
                    "description": collection.description or "",
                    "status": collection.status or "ACTIVE",
                    "notify_visitor": collection.notify_visitor if hasattr(collection, 'notify_visitor') else True,
                    "notify_agent": collection.notify_agent if hasattr(collection, 'notify_agent') else True,
                    "is_blacklisted": is_blacklisted,
                    "visitor_name": collection.visitor_name,
                    "visitor_email": collection.visitor_email,
                    "visitor_phone": collection.visitor_phone,
                    "source": collection.source,
                    "originalProperty": original_property_data,
                    "preferences": preferences_data,
                    "matchedProperties": properties_data,  # Add actual properties data
                    "property_count": total_count,
                    "active_property_count": active_count,
                    "is_anonymous": collection.owner_id is None,
                    "is_public": bool(collection.is_public) if collection.is_public is not None else False,
                    "share_token": collection.share_token,
                    "short_code": collection.short_code,
                    "created_at": collection.created_at.isoformat(),
                    "updated_at": collection.updated_at.isoformat() if collection.updated_at else collection.created_at.isoformat(),
                    "refine_request_message": collection.refine_request_message,
                    "refine_request_at": collection.refine_request_at.isoformat() if collection.refine_request_at else None,
                    "favorited_at": collection.favorited_at.isoformat() if collection.favorited_at else None,
                    "inactive_reason": collection.inactive_reason,
                    "inactive_at": collection.inactive_at.isoformat() if collection.inactive_at else None,
                    "email_mode": (pref.mode or "daily") if (pref := email_prefs.get((collection.visitor_email or "").strip().lower())) else "daily",
                    "email_mode_reason": pref.reason if pref else None,
                    "stats": {
                        "totalProperties": total_count,
                        "activeProperties": active_count,
                        "newProperties": new_listings_count,
                        "lastActivity": collection.last_visitor_activity_at.isoformat() if collection.last_visitor_activity_at else None,
                        "lastAgentDismissedAt": collection.last_agent_dismissed_at.isoformat() if collection.last_agent_dismissed_at else None,
                        "activityCounts": activity_counts_lookup.get(collection.id, {}),
                        "tourRequestedCount": tour_requested_lookup.get(collection.id, 0),
                        "commentPropertyId": comment_property_lookup.get(collection.id),
                    }
                }
                collections_data.append(collection_data)

            # Sort: showcases with real visitor activity first (most recent activity first),
            # then showcases with no activity yet ordered by how recently they were created -
            # so a brand new lead ranks above a showcase that's been sitting untouched for weeks.
            def _sort_key(d: Dict[str, Any]):
                last_activity = d['stats']['lastActivity']
                if last_activity:
                    return (1, last_activity)
                return (0, d['created_at'])

            collections_data.sort(key=_sort_key, reverse=True)

            return collections_data

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            raise e

    @staticmethod
    async def dismiss_new_listings(
        db: AsyncSession,
        collection_id: str,
        user_id: str
    ) -> Dict[str, Any]:
        """Updates last_agent_dismissed_at to now for a collection."""
        try:
            query = select(Collection).where(
                and_(
                    Collection.id == collection_id,
                    Collection.owner_id == user_id
                )
            )
            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return {"success": False, "error": "Collection not found"}

            collection.last_agent_dismissed_at = datetime.now(timezone.utc)
            await db.commit()

            return {"success": True}

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            return {"success": False, "error": str(e)}

    @staticmethod
    async def mark_collection_seen(db: AsyncSession, collection_id: str, user_id: str) -> Dict[str, Any]:
        """The agent just opened this showcase (e.g. clicked into it from a Leads/Dashboard alert) -
        clears every pending like/dislike/comment/tour-request tag on it in one shot, the same
        "agent_viewed_at" signal each of those tags already individually respects, rather than
        requiring the agent to click through every single property card to dismiss each one."""
        try:
            owns = await db.execute(select(Collection.created_at, Collection.last_agent_dismissed_at).where(Collection.id == collection_id, Collection.owner_id == user_id))
            coll = owns.first()
            if not coll:
                return {"success": False, "error": "Collection not found"}

            now = datetime.now(timezone.utc)
            # agent_viewed_at is also what clears a home's NEW tag, and NEW clears only when the agent
            # opens THAT home - so homes that are still new are left alone here (a new home the buyer
            # already viewed or liked used to lose NEW the moment the agent opened the Showcase).
            # Still new inside the Showcase = matched in within the NEW window, not the first fill.
            created = _utc(coll.created_at)
            still_new = (
                select(collection_properties.c.property_id)
                .where(
                    collection_properties.c.collection_id == collection_id,
                    collection_properties.c.added_at >= now - new_window(),
                    collection_properties.c.added_at > created + FIRST_FILL,
                )
            )
            await db.execute(
                update(PropertyInteraction)
                .where(
                    PropertyInteraction.collection_id == collection_id,
                    PropertyInteraction.agent_viewed_at.is_(None),
                    PropertyInteraction.property_id.notin_(still_new),
                )
                .values(agent_viewed_at=now)
            )
            # The Showcases page card's NEW = "since you last opened it" - opening it is seeing them.
            await db.execute(update(Collection).where(Collection.id == collection_id).values(last_agent_dismissed_at=now))
            await db.execute(
                update(LeadEvent)
                .where(
                    LeadEvent.collection_id == collection_id,
                    LeadEvent.source_type.in_(['property_tour', 'property_comment']),
                    LeadEvent.agent_viewed_at.is_(None),
                )
                .values(agent_viewed_at=now)
            )
            await db.commit()

            return {"success": True}

        except Exception as e:
            logger.error("marking collection seen failed", extra={"error": str(e)})
            await db.rollback()
            return {"success": False, "error": str(e)}

    @staticmethod
    async def set_sold_filed(db: AsyncSession, collection_id: str, property_id: str, filed: bool) -> Dict[str, Any]:
        """Move one off-the-market home into the Showcase's Sold/Removed folder (or back out).
        Who may do it (the buyer through their link, the agent who owns it) is checked by the
        route. A home that's still for sale can't be filed - there'd be nothing to show it by."""
        row = (await db.execute(
            select(collection_properties.c.property_id, Property.home_status)
            .join(Property, Property.id == collection_properties.c.property_id)
            .where(and_(collection_properties.c.collection_id == collection_id,
                        collection_properties.c.property_id == property_id))
        )).first()
        if row is None:
            return {"success": False, "error": "Home not found"}
        if filed and _is_property_available(row.home_status):
            return {"success": False, "error": "This home is still for sale"}
        await db.execute(update(collection_properties).where(and_(
            collection_properties.c.collection_id == collection_id,
            collection_properties.c.property_id == property_id,
        )).values(sold_filed_at=datetime.now(timezone.utc) if filed else None))
        await db.commit()
        return {"success": True, "soldFiled": filed}

    @staticmethod
    async def submit_refine_request(db: AsyncSession, collection_id: str, message: str) -> Dict[str, Any]:
        """A visitor asked their agent (in plain English) to change the search instead of editing
        the criteria themselves - stores the message on the collection so it's visible whenever the
        agent opens the showcase, and fires a notification so they see it right away too."""
        try:
            result = await db.execute(select(Collection).where(Collection.id == collection_id))
            collection = result.scalar_one_or_none()
            if not collection:
                return {"success": False, "error": "Collection not found"}

            now = datetime.now(timezone.utc)
            collection.refine_request_message = message
            collection.refine_request_at = now

            notification = Notification(
                agent_id=collection.owner_id,
                type="REFINE_SEARCH_REQUEST",
                reference_type="COLLECTION",
                reference_id=collection_id,
                title=f"{collection.visitor_name or 'A visitor'} wants to refine their search",
                message=message,
                collection_id=collection_id,
                collection_name=collection.name,
                visitor_name=collection.visitor_name,
                link=f"/showcases?showcase={collection_id}",
                is_read=False,
                created_at=now,
            )
            db.add(notification)
            await db.commit()
            from app.models.database import User as _User
            from app.services.agent_email_service import absolute, email_agent
            from app.services.email_service import tidy_person_name
            email_agent(await db.get(_User, collection.owner_id), "REFINE_SEARCH_REQUEST", "refine_search_request",
                        f"{tidy_person_name(collection.visitor_name) or 'A client'} wants to refine their home search",
                        {"visitor_name": collection.visitor_name or "Your client", "showcase_name": collection.name,
                         "request_note": message, "link": absolute(f"/showcases?showcase={collection_id}")})
            from app.services.leads_service import log_showcase_activity
            event = await log_showcase_activity(db, collection_id, "showcase_refine", "Asked you to change their Showcase search",
                                                raw_snapshot={"message": message})
            if event is not None:
                # A concrete ask - it gets a follow-up (Follow-ups page + calendar), like a comment.
                from app.services.tasks_service import schedule_followup_for_lead_event
                schedule_followup_for_lead_event(collection.owner_id, event.lead_id, event.id, "showcase_refine")

            return {"success": True}

        except Exception as e:
            logger.error("submitting refine request failed", extra={"error": str(e)})
            await db.rollback()
            return {"success": False, "error": str(e)}

    @staticmethod
    async def count_pending_refine_requests(db: AsyncSession, user_id: str) -> int:
        """How many of the agent's showcases currently have an undismissed refine-search message -
        drives the Showcases nav badge. Deliberately keyed off Collection.refine_request_message
        (the same field the DetailView banner checks), not Notification.is_read - those two are
        decoupled (reading the bell notification doesn't dismiss the banner, and dismissing the
        banner doesn't mark the notification read), so this is the authoritative "still needs a
        look" signal, not just "unseen alert."""
        result = await db.execute(
            select(func.count(Collection.id)).where(
                Collection.owner_id == user_id,
                Collection.refine_request_message.isnot(None),
            )
        )
        # Plus every tour request still waiting on the agent (not confirmed, not marked handled).
        tours = await db.execute(
            select(func.count(PropertyTour.id))
            .join(Collection, Collection.id == PropertyTour.collection_id)
            .where(
                Collection.owner_id == user_id,
                PropertyTour.confirmed_date.is_(None),
                PropertyTour.is_completed.is_(False),
            )
        )
        return (result.scalar() or 0) + (tours.scalar() or 0)

    # What the Showcases page's Activity inbox lists - everything clients do in their Showcases.
    INBOX_ACTIVITY_TYPES = ('PROPERTY_INTERACTION', 'PROPERTY_COMMENT', 'TOUR_REQUEST',
                            'REFINE_SEARCH_REQUEST', 'SHOWCASE_SEARCH_CHANGED')

    @staticmethod
    async def get_inbox(db: AsyncSession, user_id: str) -> Dict[str, Any]:
        """The Showcases page header's two inboxes: tour requests still waiting on the agent
        (across every Showcase), and the last 30 days of client activity in their Showcases."""
        tour_rows = (await db.execute(
            select(PropertyTour, Collection.name, Collection.visitor_name)
            .join(Collection, Collection.id == PropertyTour.collection_id)
            .options(selectinload(PropertyTour.property))
            .where(
                Collection.owner_id == user_id,
                PropertyTour.confirmed_date.is_(None),
                PropertyTour.is_completed.is_(False),
            )
            .order_by(PropertyTour.created_at.desc())
        )).all()
        tours = []
        for tour, col_name, visitor in tour_rows:
            prop = tour.property
            tours.append({
                "id": tour.id, "collection_id": tour.collection_id, "property_id": tour.property_id,
                "showcase_name": visitor or col_name,
                "visitor_name": tour.visitor_name, "visitor_email": tour.visitor_email, "visitor_phone": tour.visitor_phone,
                "preferred_date": tour.preferred_date, "preferred_time": tour.preferred_time,
                "preferred_date_2": tour.preferred_date_2, "preferred_time_2": tour.preferred_time_2,
                "preferred_date_3": tour.preferred_date_3, "preferred_time_3": tour.preferred_time_3,
                "message": tour.message, "confirmed_date": None, "confirmed_time": None,
                "is_completed": False, "created_at": tour.created_at.isoformat() if tour.created_at else None,
                "property": {
                    "street_address": prop.street_address, "city": prop.city, "state": prop.state,
                    "imageUrl": prop.img_src,
                } if prop is not None else None,
            })
        since = datetime.now(timezone.utc) - timedelta(days=30)
        notes = (await db.execute(
            select(Notification)
            .where(
                Notification.agent_id == user_id,
                Notification.collection_id.isnot(None),
                Notification.type.in_(CollectionsService.INBOX_ACTIVITY_TYPES),
                Notification.created_at >= since,
            )
            .order_by(Notification.created_at.desc())
            .limit(60)
        )).scalars().all()
        pids = {n.property_id for n in notes if n.property_id}
        photos = {}
        if pids:
            photos = {pid: img for pid, img in (await db.execute(
                select(Property.id, Property.img_src).where(Property.id.in_(pids))
            )).all()}
        activity = [{
            "id": n.id, "type": n.type, "title": n.title, "message": n.message,
            "collection_id": n.collection_id, "showcase_name": n.visitor_name or n.collection_name,
            "property_id": n.property_id, "property_address": n.property_address,
            "photo": photos.get(n.property_id), "link": n.link, "is_read": n.is_read,
            "created_at": n.created_at.isoformat(),
        } for n in notes]
        return {"tours": tours, "activity": activity, "unread_activity": sum(1 for a in activity if not a["is_read"])}

    @staticmethod
    async def mark_inbox_read(db: AsyncSession, user_id: str) -> None:
        await db.execute(
            update(Notification)
            .where(
                Notification.agent_id == user_id,
                Notification.collection_id.isnot(None),
                Notification.type.in_(CollectionsService.INBOX_ACTIVITY_TYPES),
                Notification.is_read.is_(False),
            )
            .values(is_read=True, read_at=datetime.now(timezone.utc))
        )
        await db.commit()

    @staticmethod
    async def _showcase_only_followups(db: AsyncSession, collection: Collection) -> list:
        """Open auto follow-ups that only exist because of this Showcase - its comment alerts and
        "change my search" requests. Everything else about the person stays."""
        from app.models.database import AgentTask, LeadEvent
        event_ids = select(LeadEvent.id).where(LeadEvent.collection_id == collection.id)
        return list((await db.execute(
            select(AgentTask).where(
                AgentTask.owner_id == collection.owner_id,
                AgentTask.completed_at.is_(None),
                AgentTask.auto_source.in_(("showcase_comment", "showcase_search_request")),
                AgentTask.auto_source_id.in_(event_ids),
            )
        )).scalars().all())

    @staticmethod
    async def delete_preview(db: AsyncSession, collection_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        collection = (await db.execute(select(Collection).where(
            Collection.id == collection_id, Collection.owner_id == user_id))).scalar_one_or_none()
        if collection is None:
            return None
        tasks = await CollectionsService._showcase_only_followups(db, collection)
        return {"followups": [t.title for t in tasks]}

    @staticmethod
    async def dismiss_refine_request(db: AsyncSession, collection_id: str, user_id: str) -> Dict[str, Any]:
        """Agent acknowledged (and, presumably, acted on) the visitor's refine-search message -
        clears it so the banner stops showing in the showcase view."""
        try:
            result = await db.execute(select(Collection).where(Collection.id == collection_id, Collection.owner_id == user_id))
            collection = result.scalar_one_or_none()
            if not collection:
                return {"success": False, "error": "Collection not found"}

            collection.refine_request_message = None
            collection.refine_request_at = None
            await db.commit()
            # Dismissed = handled: its "Update Jane's Showcase search" follow-up is checked off too.
            from app.services.tasks_service import complete_request_followups
            await complete_request_followups(db, user_id, collection_id=collection_id, source_types=("showcase_refine",),
                                             rules={"showcase_search_request"})

            return {"success": True}

        except Exception as e:
            logger.error("dismissing refine request failed", extra={"error": str(e)})
            await db.rollback()
            return {"success": False, "error": str(e)}

    @staticmethod
    async def get_collection_by_id(
        db: AsyncSession, 
        collection_id: str, 
        user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get a specific collection by ID with total and active property counts"""
        try:
            query = select(Collection).options(
                selectinload(Collection.preferences),
                selectinload(Collection.properties),
                selectinload(Collection.property_interactions),
                selectinload(Collection.original_open_house_event)
            ).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return None

            # Calculate total vs active (non-disliked) properties
            total_count = len(collection.properties) if collection.properties else 0
            
            disliked_property_ids = {
                interaction.property_id 
                for interaction in collection.property_interactions 
                if interaction.disliked
            }

            # Transform properties to frontend format (similar to get_shared_collection)
            properties = []
            active_count = 0
            new_listings_count = 0

            # Same card rule as get_user_collections.
            dismiss_reference = card_new_since(collection)
            one_added = {r.property_id: r.added_at for r in (await db.execute(
                select(collection_properties.c.property_id, collection_properties.c.added_at)
                .where(collection_properties.c.collection_id == collection.id))).all()}

            for prop in collection.properties:
                # "Active" here means still a real, purchasable match: not disliked and not
                # sold/removed - the single count shown on the dashboard card.
                if prop.id not in disliked_property_ids and _is_property_available(prop.home_status):
                    active_count += 1

                m_in = matched_in_at(collection, one_added.get(prop.id))
                if m_in is not None and m_in > dismiss_reference:
                    new_listings_count += 1

                property_dict = {
                    'id': prop.id,
                    'ListingKey': prop.listing_key,
                    'open_houses': open_houses_payload(prop),
                    'FullStreetAddress': prop.street_address or 'Unknown Address',
                    'City': prop.city,
                    'StateOrProvince': prop.state,
                    'PostalCode': prop.zipcode,
                    'ListPrice': prop.price,
                    'BedroomsTotal': prop.bedrooms,
                    'BathroomsTotal': prop.bathrooms,
                    'LivingArea': prop.living_area,
                    'LotSizeSquareFeet': prop.lot_size,
                    'LotSizeAcres': prop.lot_size_acres,
                    'SchoolDistrictName': prop.school_district_name,
                    'Latitude': prop.latitude,
                    'Longitude': prop.longitude,
                    'PropertyType': prop.home_type,
                    'YearBuilt': prop.year_built,
                    'DaysOnMarket': CollectionsService._calculate_dom(
                        prop.mls_list_date, prop.days_on_market, prop.home_status, dom_fetched_at(prop),
                    ),
                    'AssociationYN': prop.has_association,
                    'ListPictureURL': prop.img_src,
                    'PublicRemarks': '',
                    'ModificationTimestamp': prop.modification_timestamp.isoformat() if prop.modification_timestamp else (prop.updated_at.isoformat() if prop.updated_at else None),
                    'PriceChangeTimestamp': prop.price_change_timestamp.isoformat() if prop.price_change_timestamp else None,
                    'MlsStatus': prop.home_status,                }
                properties.append(property_dict)

            # Get preferences data if available
            preferences_data = {}
            if collection.preferences:
                preferences_data = {
                    "min_beds": collection.preferences.min_beds,
                    "max_beds": collection.preferences.max_beds,
                    "min_baths": collection.preferences.min_baths,
                    "max_baths": collection.preferences.max_baths,
                    "min_price": collection.preferences.min_price,
                    "max_price": collection.preferences.max_price,
                    "min_year_built": collection.preferences.min_year_built,
                    "max_year_built": collection.preferences.max_year_built,
                    "lat": collection.preferences.lat,
                    "long": collection.preferences.long,
                    "address": collection.preferences.address,  # Add missing address field
                    "cities": collection.preferences.cities,
                    "townships": collection.preferences.townships,
                    "school_districts": collection.preferences.school_districts,
                    "diameter": collection.preferences.diameter,
                    "special_features": collection.preferences.special_features,
                    "visiting_reason": collection.preferences.visiting_reason,
                    "has_agent": collection.preferences.has_agent,
                    "compare_property_listing_key": collection.preferences.compare_property_listing_key,
                    "show_match_score": collection.preferences.show_match_score,
                    # The editor reopens with everything the agent set - leaving these out made
                    # the drawn area, counties, size and lot settings look empty, and the next Save
                    # quietly erased them.
                    "search_polygon": collection.preferences.search_polygon,
                    "counties": collection.preferences.counties,
                    "county": collection.preferences.county,
                    "state": collection.preferences.state,
                    "min_sqft": collection.preferences.min_sqft,
                    "max_sqft": collection.preferences.max_sqft,
                    "min_acres": collection.preferences.min_acres,
                    "max_acres": collection.preferences.max_acres,

                    "is_town_house": collection.preferences.is_town_house,
                    "is_condo": collection.preferences.is_condo,
                    "is_single_family": collection.preferences.is_single_family,
                    "is_lot_land": collection.preferences.is_lot_land,
                    "is_multi_family": collection.preferences.is_multi_family,
                    "is_apartment": collection.preferences.is_apartment,
                    "is_commercial": collection.preferences.is_commercial,
                    "is_farm": collection.preferences.is_farm
                }

            is_blacklisted = False
            if collection.visitor_email:
                is_blacklisted = await BlacklistService.is_blacklisted(db, collection.visitor_email)

            try:
                original_property_data = await _resolve_original_property_data(db, collection)
            except Exception:
                original_property_data = None

            return {
                "id": collection.id,
                "name": collection.name,
                "description": collection.description or "",
                "visitor_name": collection.visitor_name,
                "visitor_email": collection.visitor_email,
                "visitor_phone": collection.visitor_phone,
                "preferences": preferences_data,
                "originalProperty": original_property_data,
                "properties": properties,
                "property_count": total_count,
                "active_property_count": active_count,
                "is_anonymous": collection.owner_id is None,
                "is_public": collection.is_public or False,
                "share_token": collection.share_token,
                "short_code": collection.short_code,
                "status": collection.status,
                "notify_visitor": collection.notify_visitor if hasattr(collection, 'notify_visitor') else True,
                "notify_agent": collection.notify_agent if hasattr(collection, 'notify_agent') else True,
                "is_blacklisted": is_blacklisted,
                "created_at": collection.created_at.isoformat(),
                "updated_at": collection.updated_at.isoformat() if collection.updated_at else collection.created_at.isoformat(),
                "stats": {
                    "totalProperties": total_count,
                    "activeProperties": active_count,
                    "newProperties": new_listings_count,
                    "lastActivity": collection.last_visitor_activity_at.isoformat() if collection.last_visitor_activity_at else None,
                    "lastAgentDismissedAt": collection.last_agent_dismissed_at.isoformat() if collection.last_agent_dismissed_at else None
                }
            }

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            raise e

    @staticmethod
    async def create_collection(
        db: AsyncSession,
        collection_data: CollectionCreate,
        user_id: str
    ) -> Dict[str, Any]:
        """Create a new collection with intelligent status assignment"""
        try:
            # Determine if this collection should be active or inactive based on current count
            should_be_active = await CollectionsService.should_create_as_active(db, user_id)
            status = 'ACTIVE' if should_be_active else 'INACTIVE'

            active_count = await CollectionsService.count_active_collections(db, user_id)

            # Generate share token for public access
            share_token = CollectionsService.generate_share_token()

            collection = Collection(
                id=str(uuid.uuid4()),
                name=collection_data.name,
                description=collection_data.description,
                owner_id=user_id,
                is_public=collection_data.is_public if collection_data.is_public is not None else True,
                share_token=share_token,
                status=status,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )

            db.add(collection)
            await db.commit()
            await db.refresh(collection)

            try:
                preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection.id)

                if preferences:
                    # Initial population for newly created collections
                    await CollectionsService.repopulate_collection_from_preferences(db, collection.id, commit=True)

            except Exception as e:
                # Collection creation should still succeed even if property population fails
                pass

            return {
                "id": collection.id,
                "name": collection.name,
                "description": collection.description or "",
                "visitor_name": collection.visitor_name,
                "visitor_email": collection.visitor_email,
                "visitor_phone": collection.visitor_phone,
                "preferences": {},  # No preferences relationship created yet
                "property_count": 0,
                "is_anonymous": False,
                "is_public": collection.is_public,
                "share_token": collection.share_token,
                "short_code": collection.short_code,
                "status": collection.status,
                "notify_visitor": collection.notify_visitor if hasattr(collection, 'notify_visitor') else True,
                "notify_agent": collection.notify_agent if hasattr(collection, 'notify_agent') else True,
                "created_at": collection.created_at.isoformat(),
                "updated_at": collection.updated_at.isoformat(),
                "stats": {
                    "totalProperties": 0,
                    "activeProperties": 0,
                    "lastActivity": None
                }
            }

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e

    @staticmethod
    def _agent_has_premium_access(user: Optional[User]) -> bool:
        """Mirrors app.utils.auth.require_premium_plan's eligibility rules without raising -
        Showcases are Premium-only, and this runs from the public/anonymous form-submission
        path where there's no request-scoped current_user to gate through that dependency."""
        from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS
        return owner_has_access(user, SHOWCASE_ACCESS_TIERS)

    @staticmethod
    def _preferences_from_showcase_form_answers(answers_by_id: Dict[str, Any]) -> Dict[str, Any]:
        """Maps the showcase_request form's fixed question set (see DEFAULT_FORMS in
        forms_routes.py) onto CollectionPreferences fields. Kept in one place since both the
        create and update paths need the same mapping."""

        def to_number(value: Any) -> Optional[float]:
            if value is None or value == "":
                return None
            if isinstance(value, (int, float)):
                return value
            try:
                cleaned = str(value).replace("$", "").replace(",", "").strip()
                return float(cleaned) if cleaned else None
            except (TypeError, ValueError):
                return None

        def to_int(value: Any) -> Optional[int]:
            number = to_number(value)
            return int(number) if number is not None else None

        def to_string_list(value: Any) -> list:
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item or "").strip()]
            return []

        cities = to_string_list(answers_by_id.get("cities"))
        school_districts = to_string_list(answers_by_id.get("school_districts"))
        counties = to_string_list(answers_by_id.get("county"))
        key_features = to_string_list(answers_by_id.get("key_features"))

        home_types = to_string_list(answers_by_id.get("home_type"))
        type_flags = {
            "is_single_family": "Single-family home" in home_types,
            "is_condo": "Condo/Townhouse" in home_types,
            "is_town_house": "Condo/Townhouse" in home_types,
            "is_multi_family": "Multi-family" in home_types,
            "is_lot_land": "Land" in home_types,
        }

        # The IDX "Save this search" window sends the website search's exact home types (the form
        # itself lumps Condo/Townhouse together), so the Showcase matches the search one-to-one.
        exact_types = [t for t in to_string_list(answers_by_id.get("exact_home_types")) if t in type_flags or t == "is_town_house"]
        if exact_types:
            type_flags = {flag: flag in exact_types for flag in type_flags}

        # I'm Buying / How Can We Help keep their own versions of a few of these (kept as-is) -
        # used when the Showcase question itself was left blank.
        if not home_types and answers_by_id.get("buy_property_type"):
            home_types = [str(answers_by_id["buy_property_type"])]
            type_flags = {
                "is_single_family": "Single-family home" in home_types,
                "is_condo": "Condo/Townhouse" in home_types,
                "is_town_house": "Condo/Townhouse" in home_types,
                "is_multi_family": "Multi-family" in home_types,
                "is_lot_land": "Land" in home_types,
            }
        notes_text = str(answers_by_id.get("notes") or answers_by_id.get("buy_notes") or "").strip()
        features_text = ", ".join(key_features)
        special_features = "\n".join(part for part in [notes_text, f"Key features: {features_text}" if features_text else ""] if part)

        # A typo in a number box (a budget typed into "bedrooms", a year with an extra digit) must
        # not empty the buyer's Showcase - numbers outside any real home's range are ignored.
        def sane(v, lo, hi):
            return v if v is not None and lo <= v <= hi else None

        # "Around a home I love" from the IDX Showcase builder (user, 10/18): that home's spot + how
        # far to look, stored as the Showcase's own distance search (lat/long/diameter = radius in
        # miles), so the agent's and the client's radius tools work on it like any other Showcase.
        center: Dict[str, Any] = {}
        c_lat, c_lng, c_rad = to_number(answers_by_id.get("center_lat")), to_number(answers_by_id.get("center_lng")), to_number(answers_by_id.get("radius_miles"))
        if c_lat is not None and c_lng is not None and -90 <= c_lat <= 90 and -180 <= c_lng <= 180 and sane(c_rad, 0.1, 50):
            center = {"address": str(answers_by_id.get("center_address") or "").strip()[:300] or None,
                      "lat": float(c_lat), "long": float(c_lng), "diameter": float(c_rad), "search_polygon": None}
        elif str(answers_by_id.get("search_mode") or "") in ("towns", "district"):
            # The builder switched this Showcase to towns / a school district: its old circle or
            # drawn area goes, or both would apply at once (a district inside the old circle).
            center = {"address": None, "lat": None, "long": None, "diameter": None, "search_polygon": None}

        this_year = datetime.now().year
        min_price, max_price = sane(to_int(answers_by_id.get("budget_min")), 1, 500_000_000), sane(to_int(answers_by_id.get("budget_max")), 1, 500_000_000)
        if min_price and max_price and min_price > max_price:
            min_price, max_price = max_price, min_price
        return {
            "cities": cities,
            "school_districts": school_districts,
            "county": ", ".join(counties) or None,
            "counties": counties,
            "state": str(answers_by_id.get("state") or "").strip() or None,
            "min_price": min_price,
            "max_price": max_price,
            "min_beds": sane(to_int(answers_by_id.get("min_bedrooms")) or to_int(answers_by_id.get("buy_bedrooms")), 1, 20),
            # Only sent by the IDX "Save this search" window (the form itself has no baths question).
            "min_baths": sane(to_number(answers_by_id.get("min_baths")), 0.5, 20),
            "min_acres": sane(to_number(answers_by_id.get("min_acres")), 0.01, 2000),
            "max_acres": sane(to_number(answers_by_id.get("max_acres")), 0.01, 2000),
            "min_sqft": sane(to_number(answers_by_id.get("min_sqft")), 100, 100_000),
            "max_sqft": sane(to_number(answers_by_id.get("max_sqft")), 100, 100_000),
            "min_year_built": sane(to_int(answers_by_id.get("min_year_built")), 1700, this_year + 3),
            "max_year_built": sane(to_int(answers_by_id.get("max_year_built")), 1700, this_year + 3),
            "special_features": special_features,
            **type_flags,
            # An area the visitor drew on the IDX map ("Save this search") - their alerts follow
            # that exact shape. Only set when present, so a refresh never wipes an agent-drawn one.
            **({"search_polygon": polygon} if (polygon := geography.polygon_from_param(answers_by_id.get("search_area"))) else {}),
            **center,
        }

    @staticmethod
    async def create_or_update_from_form_submission(
        db: AsyncSession,
        form: AgentForm,
        submission: FormSubmission,
        answers_by_id: Dict[str, Any],
        require_criteria: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Auto-builds (or updates) a Showcase from a 'Setup My Own Showcase' (showcase_request)
        form submission - the QR-code-on-the-Brag-Sheet flow - and, with require_criteria, from
        the same home-search questions on I'm Buying / How Can We Help (skipped when the buyer
        left every where/price/bedroom question blank). Never raises: a failure here must never
        break the visitor's form submission, so callers should treat this as best-effort.
        """
        try:
            agent = (await db.execute(select(User).where(User.id == form.owner_id))).scalar_one_or_none()
            if not CollectionsService._agent_has_premium_access(agent):
                return None

            pref_fields = CollectionsService._preferences_from_showcase_form_answers(answers_by_id)
            if require_criteria and not any(pref_fields.get(k) for k in (
                    "cities", "school_districts", "county", "counties", "state", "min_price", "max_price", "min_beds", "search_polygon", "lat")):
                return None

            # Match on the same contact, so a returning visitor refines their existing Showcase
            # instead of piling up duplicates - email first, phone as a fallback when no email.
            existing_collection = None
            if submission.contact_email:
                result = await db.execute(
                    select(Collection).where(
                        Collection.owner_id == form.owner_id,
                        func.lower(Collection.visitor_email) == submission.contact_email.strip().lower(),
                    )
                )
                existing_collection = result.scalars().first()
            if not existing_collection and submission.contact_phone:
                result = await db.execute(
                    select(Collection).where(
                        Collection.owner_id == form.owner_id,
                        Collection.visitor_phone == submission.contact_phone,
                    )
                )
                existing_collection = result.scalars().first()

            if existing_collection:
                existing_collection.visitor_name = submission.contact_name or existing_collection.visitor_name
                existing_collection.visitor_email = submission.contact_email or existing_collection.visitor_email
                existing_collection.visitor_phone = submission.contact_phone or existing_collection.visitor_phone
                existing_collection.source = "form"
                existing_collection.source_form_submission_id = submission.id
                db.add(existing_collection)
                await db.commit()

                await CollectionPreferencesService.update_preferences(
                    db, existing_collection.id, CollectionPreferencesUpdate(**pref_fields)
                )
                await CollectionsService.repopulate_collection_from_preferences(db, existing_collection.id, commit=True)
                collection = existing_collection
                # Asked for their Showcase again (Setup My Own Showcase) - their refreshed link goes to their
                # inbox too, same as a first one (the follow-up text says it was emailed).
                if form.form_type == "showcase_request":
                    from app.services.showcase_digest_service import queue_showcase_ready
                    await queue_showcase_ready(db, collection.id)
            else:
                should_be_active = await CollectionsService.should_create_as_active(db, form.owner_id)
                collection = Collection(
                    id=str(uuid.uuid4()),
                    name=f"Showcase for {submission.contact_name or 'Guest'}",
                    description=f"Auto-built from the {form.title} form",
                    owner_id=form.owner_id,
                    visitor_name=submission.contact_name,
                    visitor_email=submission.contact_email,
                    visitor_phone=submission.contact_phone,
                    share_token=CollectionsService.generate_share_token(),
                    is_public=True,
                    status='ACTIVE' if should_be_active else 'INACTIVE',
                    source="form",
                    source_form_submission_id=submission.id,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                db.add(collection)
                await db.commit()
                await db.refresh(collection)

                await CollectionPreferencesService.create_preferences(
                    db, CollectionPreferencesCreate(collection_id=collection.id, **pref_fields)
                )
                await CollectionsService.repopulate_collection_from_preferences(db, collection.id, commit=True)
                # Their link, in their inbox right away (not just on the thank-you page).
                from app.services.showcase_digest_service import queue_showcase_ready
                await queue_showcase_ready(db, collection.id)

            matched_lead = await upsert_lead(
                db,
                owner_id=form.owner_id,
                name=submission.contact_name,
                email=submission.contact_email,
                phone=submission.contact_phone,
                source_type="collection_visitor",
                source_id=collection.id,
                summary=f"Showcase {'updated' if existing_collection else 'created'} from {form.title} form - {collection.name}",
                occurred_at=datetime.now(timezone.utc),
                collection_id=collection.id,
                form_id=form.id,
            )
            if matched_lead and matched_lead.showcase_deleted_at:
                matched_lead.showcase_deleted_at = None
                db.add(matched_lead)
                await db.commit()

            return {"collection_id": collection.id, "created": existing_collection is None, "share_token": collection.share_token}
        except Exception as e:
            logger.error("Auto-creating/updating showcase from form submission failed", extra={"error": str(e)})
            await db.rollback()
            return None

    @staticmethod
    async def update_collection_status(
        db: AsyncSession,
        collection_id: str,
        user_id: str,
        status: str
    ) -> bool:
        """Update collection status"""
        try:
            query = select(Collection).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return False

            old_status = collection.status

            # Update the status column directly
            if status != old_status:
                from app.services.showcase_sleep import agent_changed
                await agent_changed(db, collection, status)
            collection.status = status
            collection.updated_at = datetime.now(timezone.utc)
            if status == 'ACTIVE' and old_status != 'ACTIVE' and collection.visitor_email:
                # Turned off when their lead was deleted (lead_cleanup_service) - turning it back on
                # means the agent wants them getting its updates again.
                from app.services.client_email_service import lift_lead_deleted
                await lift_lead_deleted(db, user_id, collection.visitor_email)

            await db.commit()

            # If reactivating, catch up on missed properties that were listed while inactive
            if status == 'ACTIVE' and old_status != 'ACTIVE':
                try:
                    await CollectionsService.repopulate_collection_from_preferences(db, collection_id, commit=True)
                except Exception as e:
                    logger.error(f"Failed to repopulate collection {collection_id} after reactivation: {e}")
                    # We don't fail the status update if repopulation fails

            return True

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e

    @staticmethod
    async def update_collection_notifications(
        db: AsyncSession,
        collection_id: str,
        user_id: str,
        notify_visitor: bool,
        notify_agent: bool
    ) -> bool:
        """Update collection notification settings"""
        try:
            query = select(Collection).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return False

            # Update the notification columns directly
            collection.notify_visitor = notify_visitor
            collection.notify_agent = notify_agent
            collection.updated_at = datetime.now(timezone.utc)

            await db.commit()
            return True

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e

    @staticmethod
    async def update_shared_collection_notifications(
        db: AsyncSession,
        share_token: str,
        notify_visitor: bool
    ) -> bool:
        """Update collection notification settings via share token (for visitors)"""
        try:
            query = select(Collection).where(share_match(share_token))

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return False

            # Update the visitor notification column
            was_on = bool(collection.notify_visitor)
            collection.notify_visitor = notify_visitor
            collection.updated_at = datetime.now(timezone.utc)

            await db.commit()
            if was_on != bool(notify_visitor):
                from app.services.leads_service import log_showcase_activity
                await log_showcase_activity(
                    db, collection.id, "showcase_emails_on" if notify_visitor else "showcase_emails_off",
                    "Turned their Showcase emails back on" if notify_visitor else "Turned off their Showcase emails")
            return True

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e

    @staticmethod
    def generate_share_token() -> str:
        """Generate a unique, secure share token"""
        # Generate a 12-character random string with letters and numbers
        alphabet = string.ascii_letters + string.digits
        random_part = ''.join(secrets.choice(alphabet) for _ in range(12))
        return f"coll-{random_part}"

    @staticmethod
    async def toggle_share_status(
        db: AsyncSession,
        collection_id: str,
        user_id: str,
        make_public: bool,
        force_regenerate: bool = False
    ) -> Dict[str, Any]:
        """Toggle collection share status and generate/revoke share token"""
        try:
            query = select(Collection).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return {"success": False, "message": "Collection not found"}

            if make_public:
                # Generate new share token if making public or forced regeneration
                if not collection.share_token or force_regenerate:
                    # Ensure the token is unique
                    while True:
                        new_token = CollectionsService.generate_share_token()
                        # Check if token already exists
                        token_check = await db.execute(
                            select(Collection).where(Collection.share_token == new_token)
                        )
                        if not token_check.scalar_one_or_none():
                            collection.share_token = new_token
                            break

                collection.is_public = True
                from app.services.showcase_links import showcase_path
                share_url = await showcase_path(collection)
                message = "Collection is now public and shareable"
            else:
                # Make private but keep the share token for potential future use
                collection.is_public = False
                share_url = None
                message = "Collection is now private"

            collection.updated_at = datetime.now(timezone.utc)
            await db.commit()

            return {
                "success": True,
                "message": message,
                "is_public": collection.is_public,
                "share_token": collection.share_token if collection.is_public else None,
                "share_url": share_url
            }

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e

    @staticmethod
    async def get_shared_collection(
        db: AsyncSession,
        share_token: str
    ) -> Optional[Dict[str, Any]]:
        """Get a shared collection by share token (for anonymous access)"""
        try:
            # Query collection with properties and preferences, ensure it's public
            query = (
                select(Collection)
                .options(
                    selectinload(Collection.properties),
                    joinedload(Collection.preferences),
                    selectinload(Collection.original_open_house_event)
                )
                .where(
                    and_(
                        share_match(share_token),
                        Collection.is_public == True,
                        Collection.status == "ACTIVE",
                    )
                )
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                # Let's also check if collection exists but is not public
                debug_query = select(Collection).where(share_match(share_token))
                debug_result = await db.execute(debug_query)
                debug_collection = debug_result.scalar_one_or_none()
                if debug_collection:
                    pass
                else:
                    pass
                return None

            if collection.preferences:
                pass
            else:
                # Manual query for preferences to debug the issue
                from app.models.database import CollectionPreferences
                prefs_query = select(CollectionPreferences).where(CollectionPreferences.collection_id == collection.id)
                prefs_result = await db.execute(prefs_query)
                manual_prefs = prefs_result.scalar_one_or_none()
                if manual_prefs:
                    # Use the manually loaded preferences
                    collection.preferences = manual_prefs

            agent_result = await db.execute(select(User).where(User.id == collection.owner_id))
            agent = agent_result.scalar_one_or_none()

            # Get all property IDs for batch querying interactions
            property_ids = [prop.id for prop in collection.properties]

            # Initialize lookup dictionaries
            interactions_lookup = {}
            comments_lookup = {}
            unread_comment_lookup: Dict[str, int] = {}
            tours_lookup = {}
            added_at_lookup = {}
            filed_at_lookup = {}

            # Only query if we have properties
            if property_ids:
                # Fetch added_at timestamps for each property from collection_properties
                added_at_query = select(
                    collection_properties.c.property_id,
                    collection_properties.c.added_at,
                    collection_properties.c.sold_filed_at,
                ).where(
                    and_(
                        collection_properties.c.collection_id == collection.id,
                        collection_properties.c.property_id.in_(property_ids)
                    )
                )
                added_at_result = await db.execute(added_at_query)
                added_at_data = added_at_result.all()

                # Create lookup dictionary for added_at timestamps
                for row in added_at_data:
                    added_at_lookup[row.property_id] = row.added_at
                    filed_at_lookup[row.property_id] = row.sold_filed_at
            sold_folder, back_on_market = sold_folder_split(collection.properties, filed_at_lookup)

            # Calculate "new" threshold (properties added in last 7 days)
            now = datetime.now(timezone.utc)
            new_threshold_days = int(os.getenv("NEW_PROPERTY_DAYS", "7"))
            new_threshold = now - timedelta(days=new_threshold_days)

            # Only query if we have properties
            if property_ids:
                # Fetch all interactions for this collection in a single query
                interactions_query = select(PropertyInteraction).where(
                    and_(
                        PropertyInteraction.collection_id == collection.id,
                        PropertyInteraction.property_id.in_(property_ids)
                    )
                )

                interactions_result = await db.execute(interactions_query)
                interactions = interactions_result.scalars().all()

                # Create lookup dictionary for interactions by property_id
                for interaction in interactions:
                    interactions_lookup[interaction.property_id] = interaction

                # Fetch all comments for this collection in a single query
                comments_query = select(PropertyComment).where(
                    and_(
                        PropertyComment.collection_id == collection.id,
                        PropertyComment.property_id.in_(property_ids)
                    )
                )

                comments_result = await db.execute(comments_query)
                comments = comments_result.scalars().all()

                # Create lookup dictionary for comments by property_id
                for comment in comments:
                    if comment.property_id not in comments_lookup:
                        comments_lookup[comment.property_id] = []
                    comment_dict = {
                        'id': comment.id,
                        'content': comment.content,
                        'author': comment.visitor_name or 'Anonymous',
                        'isAgent': bool(comment.is_agent),
                        'createdAt': comment.created_at.isoformat()
                    }
                    comments_lookup[comment.property_id].append(comment_dict)

                # Unread-by-visitor count per property: agent-authored comments newer than the
                # last time the visitor actually opened this property's thread (mirrors the
                # agent-side unreadCommentCount in get_properties).
                for comment in comments:
                    if not comment.is_agent:
                        continue
                    interaction = interactions_lookup.get(comment.property_id)
                    visitor_read_at = interaction.visitor_comments_read_at if interaction else None
                    comment_created_at = comment.created_at
                    if comment_created_at.tzinfo is None:
                        comment_created_at = comment_created_at.replace(tzinfo=timezone.utc)
                    if visitor_read_at is not None:
                        if visitor_read_at.tzinfo is None:
                            visitor_read_at = visitor_read_at.replace(tzinfo=timezone.utc)
                        if comment_created_at <= visitor_read_at:
                            continue
                    unread_comment_lookup[comment.property_id] = unread_comment_lookup.get(comment.property_id, 0) + 1

                # Fetch all tours for this collection in a single query
                tours_query = select(PropertyTour).where(
                    and_(
                        PropertyTour.collection_id == collection.id,
                        PropertyTour.property_id.in_(property_ids)
                    )
                )

                tours_result = await db.execute(tours_query)
                tours = tours_result.scalars().all()

                # Create lookup dictionary for tours by property_id (just track existence)
                for tour in tours:
                    tours_lookup[tour.property_id] = True

            # Transform to response format similar to get_user_collections
            properties_data = []
            for prop in collection.properties:
                property_dict = {
                    'id': prop.id,
                    'ListingKey': prop.listing_key,
                    'open_houses': open_houses_payload(prop),
                    'FullStreetAddress': prop.street_address or 'Unknown Address',
                    'City': prop.city,
                    'StateOrProvince': prop.state,
                    'PostalCode': prop.zipcode,
                    'ListPrice': prop.price,
                    'BedroomsTotal': prop.bedrooms,
                    'BathroomsTotal': prop.bathrooms,
                    'LivingArea': prop.living_area,
                    'LotSizeSquareFeet': prop.lot_size,
                    'LotSizeAcres': prop.lot_size_acres,
                    'SchoolDistrictName': prop.school_district_name,
                    'PropertyType': prop.home_type,
                    'YearBuilt': prop.year_built,
                    'DaysOnMarket': CollectionsService._calculate_dom(
                        prop.mls_list_date, prop.days_on_market, prop.home_status, dom_fetched_at(prop),
                    ),
                    'AssociationYN': prop.has_association,
                    'ListPictureURL': prop.img_src,
                    'PublicRemarks': '',
                    'ModificationTimestamp': prop.modification_timestamp.isoformat() if prop.modification_timestamp else (prop.updated_at.isoformat() if prop.updated_at else None),
                    'PriceChangeTimestamp': prop.price_change_timestamp.isoformat() if prop.price_change_timestamp else None,
                    'MlsStatus': prop.home_status,
                    # Off the market: in the Sold/Removed folder, or still in the list with a notice.
                    'soldFiled': prop.id in sold_folder,
                    **_sale_fields(prop),
                    # Real interaction data from database
                    'liked':interactions_lookup[prop.id].liked if prop.id in interactions_lookup else False,
                    'disliked': interactions_lookup[prop.id].disliked if prop.id in interactions_lookup else False,
                    'agentRecommended': bool(prop.id in interactions_lookup and interactions_lookup[prop.id].agent_recommended_at),
                    'viewed': prop.id in interactions_lookup,  # True if any interaction exists
                    'viewCount': interactions_lookup[prop.id].view_count if prop.id in interactions_lookup else 0,
                    # NEW for the buyer: in their Showcase less than NEW_PROPERTY_DAYS (the first fill
                    # counts from when it was made); the page also hides it once they've opened it.
                    'is_new': bool(_utc(added_at_lookup.get(prop.id) or collection.created_at) >= new_threshold),
                    # when it joined their Showcase - the share preview shows the newest three
                    'AddedAt': (_utc(added_at_lookup.get(prop.id)) or _utc(collection.created_at)).isoformat() if (added_at_lookup.get(prop.id) or collection.created_at) else None,
                    'lastViewedAt': interactions_lookup[prop.id].last_viewed_at.isoformat() if prop.id in interactions_lookup and interactions_lookup[prop.id].last_viewed_at else None,
                    'comments': comments_lookup.get(prop.id, []),
                    'unreadCommentCount': unread_comment_lookup.get(prop.id, 0),
                    # Tour status
                    'hasTourScheduled': tours_lookup.get(prop.id, False),
                    # Latitude/Longitude feed the frontend's local (no-API-call) straight-line
                    # distance-from-destination display and its Google Maps directions deep link -
                    # see lib/utils.ts haversineMiles/googleMapsDirectionsUrl. Deliberately not
                    # calling the Routes API here: that would put a per-visitor routing cost on
                    # every showcase load/refresh.
                    'Latitude': prop.latitude,
                    'Longitude': prop.longitude,
                }
                properties_data.append(property_dict)

            # Calculate stats
            total_properties = len(properties_data)
            viewed_properties = sum(1 for prop in properties_data if prop.get('viewCount', 0) > 0)
            liked_properties = sum(1 for prop in properties_data if prop.get('liked', False))

            is_blacklisted = False
            if collection.visitor_email:
                is_blacklisted = await BlacklistService.is_blacklisted(db, collection.visitor_email)

            try:
                original_property_data = await _resolve_original_property_data(db, collection)
            except Exception:
                logger.warning("resolving the showcase's compare property failed", exc_info=True)
                original_property_data = None

            # Full contact info for the visitor's "Ask My Agent" tab (EditShowcaseCriteriaModal) -
            # so a visitor who'd rather just call sees who they're asking, not just a message box.
            # Same read-only Agent Hub profile merge the public agent website uses (degrades to
            # plain User fields when the agent has no Agent Hub set up).
            agent_profile = await get_agent_profile(db, agent) if agent else None

            collection_data = {
                'id': collection.id,
                'name': collection.name,
                # lets the page notice its own agent opening it (their clicks aren't the client's)
                'ownerId': collection.owner_id,
                'agentName': agent.first_name if agent else None,
                'agentFullName': agent_profile.get('name') if agent_profile else None,
                'agentEmail': agent_profile.get('email') if agent_profile else None,
                'agentPhone': agent_profile.get('phone') or None if agent_profile else None,
                'agentPhotoUrl': agent_profile.get('photo_url') if agent_profile else None,
                'originalProperty': original_property_data,
                'customer': {
                    'firstName': collection.visitor_name.split(' ')[0] if collection.visitor_name else 'Anonymous',
                    'lastName': collection.visitor_name.split(' ')[-1] if collection.visitor_name and ' ' in collection.visitor_name else 'Visitor',
                    'email': collection.visitor_email or 'anonymous@visitor.com',
                    'phone': collection.visitor_phone or 'N/A',
                    'preferredContact': 'EMAIL',
                    'is_blacklisted': is_blacklisted
                },
                'matchedProperties': properties_data,
                'createdAt': collection.created_at.isoformat(),
                'updatedAt': collection.updated_at.isoformat() if collection.updated_at else collection.created_at.isoformat(),
                'status': collection.status or 'ACTIVE',
                'notifyVisitor': collection.notify_visitor if hasattr(collection, 'notify_visitor') else True,
                'preferences': {
                    'min_beds': collection.preferences.min_beds,
                    'max_beds': collection.preferences.max_beds,
                    'min_baths': collection.preferences.min_baths,
                    'max_baths': collection.preferences.max_baths,
                    'min_price': collection.preferences.min_price,
                    'max_price': collection.preferences.max_price,
                    'min_year_built': collection.preferences.min_year_built,
                    'max_year_built': collection.preferences.max_year_built,
                    'lat': collection.preferences.lat,
                    'long': collection.preferences.long,
                    'address': collection.preferences.address,  # Add missing address field
                    'cities': collection.preferences.cities,
                    'townships': collection.preferences.townships,
                    'school_districts': collection.preferences.school_districts,
                    'diameter': collection.preferences.diameter,
                    'special_features': collection.preferences.special_features,
                    'visiting_reason': collection.preferences.visiting_reason,
                    'has_agent': collection.preferences.has_agent,
                    'compare_property_listing_key': collection.preferences.compare_property_listing_key,
                    'show_match_score': collection.preferences.show_match_score,
                    # The visitor's window shows "the area your agent drew" (they can't draw it).
                    'has_drawn_area': bool(collection.preferences.search_polygon),

                    'is_town_house': collection.preferences.is_town_house,
                    'is_condo': collection.preferences.is_condo,
                    'is_single_family': collection.preferences.is_single_family,
                    'is_lot_land': collection.preferences.is_lot_land,
                    'is_multi_family': collection.preferences.is_multi_family,
                    'is_apartment': collection.preferences.is_apartment,
                    'is_commercial': collection.preferences.is_commercial,
                    'is_farm': collection.preferences.is_farm
                } if collection.preferences else {},
                'stats': {
                    'totalProperties': total_properties,
                    'viewedProperties': viewed_properties,
                    'likedProperties': liked_properties,
                    'lastActivity': collection.last_visitor_activity_at.isoformat() if collection.last_visitor_activity_at else None
                },
                'shareToken': collection.share_token,
                'shortCode': collection.short_code,
                'isPublic': collection.is_public
            }

            await _unfile_back_on_market(db, collection.id, back_on_market)
            return collection_data

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            raise e

    @staticmethod
    async def repopulate_collection_from_preferences(
        db: AsyncSession,
        collection_id: str,
        commit: bool = True,
        preferences: Optional[CollectionPreferences] = None
    ) -> Dict[str, Any]:
        """
        Standardized utility to sync a collection's properties with its preferences.
        """
        # Local import to avoid circular dependency
        from app.services.property_service import property_service

        try:
            # 1. Get current preferences if not provided
            if not preferences:
                preferences = await CollectionPreferencesService.get_preferences_by_collection_id(db, collection_id)
            
            if not preferences:
                return {'success': False, 'error': 'Preferences not found'}

            # An address with no coordinates would match homes everywhere - place it first.
            from app.services.collection_preferences_service import fill_missing_coordinates
            await fill_missing_coordinates(db, preferences)

            # 2. Fetch new matching properties from the local database
            matching_properties = await property_service.get_properties_by_preferences(db, preferences)
            new_match_ids = {p.id for p in matching_properties}

            # 3. Identify IDs of properties to PROTECT (those with user interactions)
            # We don't want to remove properties the user has actually engaged with (Liked, Commented, Toured)
            # A simple 'view' is NOT enough to protect a property from being cleared.
            keep_query = select(collection_properties.c.property_id).where(
                collection_properties.c.collection_id == collection_id
            ).where(
                or_(
                    collection_properties.c.property_id.in_(
                        select(PropertyInteraction.property_id).where(
                            and_(
                                PropertyInteraction.collection_id == collection_id,
                                PropertyInteraction.liked == True
                            )
                        )
                    ),
                    collection_properties.c.property_id.in_(select(PropertyComment.property_id).where(PropertyComment.collection_id == collection_id)),
                    collection_properties.c.property_id.in_(select(PropertyTour.property_id).where(PropertyTour.collection_id == collection_id)),
                    # The agent's own picks (recommended / added by hand from a home's page) stay too.
                    collection_properties.c.property_id.in_(select(PropertyInteraction.property_id).where(
                        and_(PropertyInteraction.collection_id == collection_id, PropertyInteraction.agent_recommended_at.is_not(None))))
                )
            )
            keep_ids_res = await db.execute(keep_query)
            protected_ids = {r[0] for r in keep_ids_res.fetchall()}

            # The same house under two MLS numbers: matches are already one per house (newest
            # listing) - but if they acted on the other copy, keep that one instead.
            if protected_ids:
                from app.utils.same_home import is_same_home
                protected_props = (await db.execute(select(Property).where(Property.id.in_(list(protected_ids))))).scalars().all()
                new_match_ids = {p.id for p in matching_properties
                                 if p.id in protected_ids or not any(is_same_home(p, q) for q in protected_props)}

            # 4. Clear stale links (not a new match AND not protected)
            # Combine everything we want to KEEP (Matches + Liked/Commented/Toured)
            keep_ids = new_match_ids | protected_ids
            
            delete_stmt = delete(collection_properties).where(
                collection_properties.c.collection_id == collection_id
            )
            
            # If we have items to keep, exclude them from deletion. 
            # If keep_ids is empty, the stmt above will correctly delete ALL properties.
            if keep_ids:
                delete_stmt = delete_stmt.where(
                    collection_properties.c.property_id.notin_(list(keep_ids))
                )
            
            await db.execute(delete_stmt)
            
            # 5. Identify which new matches need to be inserted 
            # (skip those already linked/protected)
            existing_links_query = select(collection_properties.c.property_id).where(
                collection_properties.c.collection_id == collection_id
            )
            existing_res = await db.execute(existing_links_query)
            already_linked_ids = {r[0] for r in existing_res.fetchall()}
            
            ids_to_insert = new_match_ids - already_linked_ids
            
            # 6. Bulk Insert
            if ids_to_insert:
                insert_data = [
                    {"collection_id": collection_id, "property_id": p_id, "added_at": datetime.now(timezone.utc)}
                    for p_id in ids_to_insert
                ]
                await db.execute(insert(collection_properties), insert_data)

            if commit:
                await db.commit()

            return {
                'success': True,
                'properties_found': len(new_match_ids),
                'new_links_created': len(ids_to_insert),
                'protected_count': len(protected_ids)
            }

        except Exception as e:
            logger.error(f"Failed to repopulate collection {collection_id}: {e}", exc_info=True)
            if commit:
                await db.rollback()
            return {'success': False, 'error': str(e)}

    @staticmethod
    async def delete_collection(
        db: AsyncSession,
        collection_id: str,
        user_id: str
    ) -> bool:
        """Delete a collection and clean up orphaned properties"""
        try:
            # First, get the collection with its properties
            query = select(Collection).options(
                selectinload(Collection.properties)
            ).where(
                Collection.id == collection_id,
                Collection.owner_id == user_id
            )

            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            if not collection:
                return False

            # Mark the matching lead's showcase as deleted (if one exists) so Leads/Dash can
            # show a "Showcase Deleted" indicator instead of silently going stale.
            try:
                from app.services.leads_service import find_lead_by_contact
                matched_lead = await find_lead_by_contact(db, user_id, collection.visitor_email, collection.visitor_phone)
                if matched_lead:
                    matched_lead.showcase_deleted_at = datetime.now(timezone.utc)
                    db.add(matched_lead)
            except Exception as e:
                matched_lead = None
                logger.error(f"Failed to flag lead showcase_deleted_at: {e}")

            # Follow-ups that only existed because of this Showcase (its comment alerts and
            # "change my search" requests) go with it - and off the agent's calendar.
            removed_task_ids: list[str] = []
            try:
                for task in await CollectionsService._showcase_only_followups(db, collection):
                    removed_task_ids.append(task.id)
                    await db.delete(task)
            except Exception as e:
                logger.error(f"Failed to remove the deleted Showcase's follow-ups: {e}")

            # Delete the collection. Its collection_properties links, likes/views and comments go
            # with it (ON DELETE CASCADE on collection_id). The homes themselves are never deleted:
            # they are the shared MLS mirror (history, CMA comps, IDX, other Showcases later) - a
            # home that was in no other Showcase used to be hard-deleted here (Bright audit, 10/21).
            # An agent's own manual listing is removed only from the Open House Manager
            # (open_house_manager_service.delete_manual_listing).
            await db.delete(collection)

            await db.commit()

            try:
                from app.services import calendar_sync_service
                from app.services.tasks_service import resync_lead_followups
                for task_id in removed_task_ids:
                    calendar_sync_service.unsync_task_bg(user_id, task_id)
                if matched_lead is not None:
                    # Their other follow-ups' calendar events drop the dead Showcase link.
                    resync_lead_followups(user_id, matched_lead.id)
            except Exception as e:
                logger.error(f"Calendar cleanup after Showcase delete failed: {e}")

            return True

        except Exception as e:
            logger.error("Operation failed", extra={"error": str(e)})
            await db.rollback()
            raise e


    @staticmethod
    async def get_properties(
        collectionId: str,
        db: AsyncSession,
        viewer_user_id: Optional[str] = None
    ):
        try:
            # Get all the properties that have a relation to the collectionId
            query = (
                select(Collection)
                .options(selectinload(Collection.properties))
                .where(Collection.id == collectionId)
            )
            result = await db.execute(query)
            collection = result.scalar_one_or_none()

            # The agent opening their own showcase clears its like/dislike activity badge -
            # same "seeing it is enough" pattern as the per-property NEW tag. PROPERTY_COMMENT
            # and TOUR_REQUEST are excluded here on purpose: those signals require a more
            # deliberate action to clear - actually reading the comment (see
            # get_property_comments) or opening the tours list (see get_collection_tours) -
            # not just from opening the showcase.
            if collection and viewer_user_id and viewer_user_id == collection.owner_id:
                await db.execute(
                    update(Notification)
                    .where(
                        Notification.agent_id == viewer_user_id,
                        Notification.collection_id == collectionId,
                        Notification.type == 'PROPERTY_INTERACTION',
                        Notification.is_read.is_(False),
                    )
                    .values(is_read=True, read_at=datetime.now(timezone.utc))
                )
                await db.commit()

            if not collection:
                return []

            property_ids = [prop.id for prop in collection.properties]

            # Fetch added_at timestamps for each property from collection_properties
            added_at_query = select(
                collection_properties.c.property_id,
                collection_properties.c.added_at,
                collection_properties.c.sold_filed_at,
            ).where(
                and_(
                    collection_properties.c.collection_id == collectionId,
                    collection_properties.c.property_id.in_(property_ids)
                )
            )
            added_at_result = await db.execute(added_at_query)
            added_at_data = added_at_result.all()

            # Create lookup dictionary for added_at timestamps
            added_at_lookup = {}
            filed_at_lookup = {}
            for row in added_at_data:
                added_at_lookup[row.property_id] = row.added_at
                filed_at_lookup[row.property_id] = row.sold_filed_at
            sold_folder, back_on_market = sold_folder_split(collection.properties, filed_at_lookup)

            # Calculate "new" threshold (properties added in last 7 days)
            now = datetime.now(timezone.utc)
            new_threshold_days = int(os.getenv("NEW_PROPERTY_DAYS", "7"))
            new_threshold = now - timedelta(days=new_threshold_days)

            interactions_query = select(PropertyInteraction).where(
                and_(
                    PropertyInteraction.collection_id == collectionId,
                    PropertyInteraction.property_id.in_(property_ids)
                )
            )

            interactions_result = await db.execute(interactions_query)
            interactions = interactions_result.scalars().all()

            # Create lookup dictionary for interactions by property_id
            interactions_lookup = {}
            for interaction in interactions:
                interactions_lookup[interaction.property_id] = interaction

            # Fetch all comments for this collection in a single query
            comments_query = select(PropertyComment).where(
                and_(
                    PropertyComment.collection_id == collectionId,
                    PropertyComment.property_id.in_(property_ids)
                )
            ).order_by(PropertyComment.created_at.asc())

            comments_result = await db.execute(comments_query)
            comments = comments_result.scalars().all()

            # Create lookup dictionary for comments by property_id
            comments_lookup = {}
            for comment in comments:
                if comment.property_id not in comments_lookup:
                    comments_lookup[comment.property_id] = []
                comments_lookup[comment.property_id].append({
                    'id': comment.id,
                    'author': comment.visitor_name or 'Anonymous',
                    'isAgent': bool(comment.is_agent),
                    'content': comment.content,
                    'createdAt': comment.created_at.isoformat()
                })

            # Unread-by-agent count per property: visitor-authored comments newer than the
            # last time the agent actually opened this property's thread (see
            # get_property_comments / mark_comments_read) - drives the message button's badge
            # instead of just "has any comments at all".
            unread_comment_lookup: Dict[str, int] = {}
            for comment in comments:
                if comment.is_agent:
                    continue
                interaction = interactions_lookup.get(comment.property_id)
                agent_read_at = interaction.agent_comments_read_at if interaction else None
                comment_created_at = comment.created_at
                if comment_created_at.tzinfo is None:
                    comment_created_at = comment_created_at.replace(tzinfo=timezone.utc)
                if agent_read_at is not None:
                    if agent_read_at.tzinfo is None:
                        agent_read_at = agent_read_at.replace(tzinfo=timezone.utc)
                    if comment_created_at <= agent_read_at:
                        continue
                unread_comment_lookup[comment.property_id] = unread_comment_lookup.get(comment.property_id, 0) + 1

            # Fetch tour counts for each property in this collection - only tours still
            # awaiting the agent's action (not yet confirmed or marked completed), so this
            # badge clears once the agent has actually handled the request instead of
            # staying stuck at 1 forever (a PropertyTour row is never deleted).
            tours_query = select(
                PropertyTour.property_id,
                func.count(PropertyTour.id).label('tour_count')
            ).where(
                and_(
                    PropertyTour.collection_id == collectionId,
                    PropertyTour.property_id.in_(property_ids),
                    PropertyTour.confirmed_date.is_(None),
                    PropertyTour.is_completed.is_(False),
                )
            ).group_by(PropertyTour.property_id)

            tours_result = await db.execute(tours_query)
            tours_data = tours_result.all()

            # Create lookup dictionary for tour counts by property_id
            tours_lookup = {}
            for tour_row in tours_data:
                tours_lookup[tour_row.property_id] = tour_row.tour_count

            properties_data = []
            for prop in collection.properties:
                added_at = _utc(added_at_lookup.get(prop.id))
                # Matched in within the NEW window (not the first fill - see FIRST_FILL notes).
                m_in = matched_in_at(collection, added_at)
                is_new = bool(m_in and m_in >= new_threshold)

                # Once the agent has actually opened this property from their own showcase
                # management view, its NEW tag clears there - independent of the collection-wide
                # "dismiss new matches" action, which is a separate, coarser signal.
                interaction = interactions_lookup.get(prop.id)
                if is_new and interaction and interaction.agent_viewed_at:
                    is_new = False

                property_dict = {
                    'id': prop.id,
                    'ListingKey': prop.listing_key,
                    'open_houses': open_houses_payload(prop),
                    'FullStreetAddress': prop.street_address or 'Unknown Address',
                    'City': prop.city,
                    'StateOrProvince': prop.state,
                    'PostalCode': prop.zipcode,
                    'ListPrice': prop.price,
                    'BedroomsTotal': prop.bedrooms,
                    'BathroomsTotal': prop.bathrooms,
                    'LivingArea': prop.living_area,
                    'LotSizeSquareFeet': prop.lot_size,
                    'LotSizeAcres': prop.lot_size_acres,
                    'SchoolDistrictName': prop.school_district_name,
                    'Latitude': prop.latitude,
                    'Longitude': prop.longitude,
                    'PropertyType': prop.home_type,
                    'YearBuilt': prop.year_built,
                    'DaysOnMarket': CollectionsService._calculate_dom(
                        prop.mls_list_date, prop.days_on_market, prop.home_status, dom_fetched_at(prop),
                    ),
                    'AssociationYN': prop.has_association,
                    'ListPictureURL': prop.img_src,
                    'PublicRemarks': '',
                    'ModificationTimestamp': prop.modification_timestamp.isoformat() if prop.modification_timestamp else (prop.updated_at.isoformat() if prop.updated_at else None),
                    'PriceChangeTimestamp': prop.price_change_timestamp.isoformat() if prop.price_change_timestamp else None,
                    'MlsStatus': prop.home_status,
                    'soldFiled': prop.id in sold_folder,  # see get_shared_collection
                    **_sale_fields(prop),
                    'liked': interactions_lookup[prop.id].liked if prop.id in interactions_lookup else False,
                    'disliked': interactions_lookup[prop.id].disliked if prop.id in interactions_lookup else False,
                    'agentRecommended': bool(prop.id in interactions_lookup and interactions_lookup[prop.id].agent_recommended_at),
                    'viewed': prop.id in interactions_lookup,  # True if any interaction exists
                    'viewCount': interactions_lookup[prop.id].view_count if prop.id in interactions_lookup else 0,
                    'lastViewedAt': interactions_lookup[prop.id].last_viewed_at.isoformat() if prop.id in interactions_lookup and interactions_lookup[prop.id].last_viewed_at else None,
                    'comments': comments_lookup.get(prop.id, []),
                    'unreadCommentCount': unread_comment_lookup.get(prop.id, 0),
                    'tourCount': tours_lookup.get(prop.id, 0),
                    'added_at': added_at.isoformat() if added_at else None,
                    'is_new': is_new
                }
                properties_data.append(property_dict)

            await _unfile_back_on_market(db, collectionId, back_on_market)
            return properties_data
        except Exception as e:
            return []


