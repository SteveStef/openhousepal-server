from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
import os
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from app.services.email_avatar_service import agent_email_vars
from app.models.database import Property, OpenHouseVisitor, Collection, collection_properties, OpenHouseEvent, User, CollectionPreferences
from app.schemas.open_house import OpenHouseFormSubmission
from app.services.collection_preferences_service import CollectionPreferencesService
from app.services.collections_service import CollectionsService
from app.services.property_service import property_service
from app.services.leads_service import upsert_lead
from app.schemas.collection_preferences import CollectionPreferences as CollectionPreferencesSchema
from app.config.logging import get_logger
from app.services.agent_email_service import agent_public_phone

logger = get_logger(__name__)


def schedule_showcase_backfill(agent_id: str) -> None:
    """Fire-and-forget: call right after an agent's plan changes. If the new plan includes
    Showcases, every guest they missed while on Basic gets their showcase + invite now. Runs on
    its own DB session so it never holds up (or rolls back) the request that changed the plan,
    and is a cheap no-op for any agent without a Showcase plan or without a backlog."""
    import asyncio

    async def _run():
        from sqlalchemy import text
        from app.database import AsyncSessionLocal, engine
        try:
            # One backfill per agent at a time, across every worker/process: several triggers can
            # fire for the same upgrade (plan-change route, PayPal webhook + its retries, the
            # Showcases-page safety net). A Postgres advisory lock held on its own connection for
            # the whole run means a second trigger simply skips; the "never had a showcase or
            # invite" filter in missed_showcases() then makes any later run a no-op too.
            async with engine.connect() as lock_conn:
                got_lock = (await lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": f"showcase-backfill:{agent_id}"}
                )).scalar()
                if not got_lock:
                    return
                try:
                    async with AsyncSessionLocal() as session:
                        await OpenHouseService.backfill_missed_showcases(session, agent_id)
                finally:
                    await lock_conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": f"showcase-backfill:{agent_id}"})
        except Exception:
            logger.exception("missed-showcase backfill failed", extra={"agent_id": agent_id})

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        # No running loop (e.g. called from a sync script) - nothing to schedule onto.
        logger.warning("no event loop for showcase backfill", extra={"agent_id": agent_id})


class OpenHouseService:

    @staticmethod
    async def create_visitor(db: AsyncSession, form_data: OpenHouseFormSubmission) -> OpenHouseVisitor:
        """Create a visitor record from open house form submission"""

        visitor = OpenHouseVisitor(
            full_name=form_data.full_name,
            email=form_data.email,
            phone=form_data.phone,
            has_agent=form_data.has_agent.value,
            open_house_event_id=form_data.open_house_event_id,
            qr_code="",  # Will be updated by the calling code
            interested_in_similar=form_data.interested_in_similar,
            created_at=datetime.now(timezone.utc)
        )

        db.add(visitor)
        await db.commit()
        await db.refresh(visitor)
        return visitor

    @staticmethod
    async def create_collection_for_visitor(
        db: AsyncSession,
        visitor: OpenHouseVisitor,
        form_data: OpenHouseFormSubmission
    ) -> Dict[str, Any]:
        """Create a collection for a visitor and immediately populate it with matching properties.
        Callers already check has_agent != YES before calling this - showcase creation isn't
        gated on interested_in_similar anymore (see open_houses_routes.py), that field is kept
        only as a historical/reporting value on the visitor record."""

        if not form_data.open_house_event_id:
            return {"success": False, "properties_added": 0}

        try:
            # Get the original open house event to create smart filters
            visited_open_house = await OpenHouseService.get_open_house_event_by_id(db, form_data.open_house_event_id)

            if not visited_open_house:
                return {"success": False, "properties_added": 0}

            # Check if agent has PREMIUM plan (collections are a Premium-only feature)
            agent_id = visited_open_house.get('agent_id')
            agent_query = select(User).where(User.id == agent_id)
            agent_result = await db.execute(agent_query)
            agent = agent_result.scalar_one_or_none()

            if not agent:
                return {"success": False, "properties_added": 0, "reason": "agent_not_found"}

            # Showcases (collections) are Premium+ with a live subscription. A Basic agent's guest
            # is NOT dropped on the floor: the sign-in (has_agent != YES, no collection) is exactly
            # what missed_showcases() reports as the backlog, and backfill_missed_showcases()
            # builds + sends every one of them the moment the agent upgrades.
            from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS
            if not owner_has_access(agent, SHOWCASE_ACCESS_TIERS):
                return {"success": False, "properties_added": 0, "reason": "basic_plan"}

            # Create collection (always ACTIVE, no limit)
            collection = Collection(
                owner_id=visited_open_house.get('agent_id'),  # Use agent_id from the open house event
                name=visited_open_house.get('address', 'Unknown Property'),
                description=f"Properties similar to {visited_open_house.get('address', 'the visited property')} based on {visitor.full_name}'s preferences",
                visitor_email=visitor.email,
                visitor_name=visitor.full_name,
                visitor_phone=visitor.phone,
                original_open_house_event_id=form_data.open_house_event_id,
                share_token=CollectionsService.generate_share_token(),
                status='ACTIVE',
                created_at=datetime.now(timezone.utc)
            )

            db.add(collection)
            await db.commit()
            await db.refresh(collection)

            matched_lead = await upsert_lead(
                db,
                owner_id=collection.owner_id,
                name=visitor.full_name,
                email=visitor.email,
                phone=visitor.phone,
                source_type="collection_visitor",
                source_id=collection.id,
                summary=f"Personalized showcase created - {collection.name}",
                occurred_at=collection.created_at,
                collection_id=collection.id,
            )
            if matched_lead and matched_lead.showcase_deleted_at:
                matched_lead.showcase_deleted_at = None
                db.add(matched_lead)
                await db.commit()

            # Auto-generate preferences based on the original property and form data
            try:
                preferences_model = await CollectionPreferencesService.auto_generate_preferences(db, collection.id, form_data)

                if preferences_model:
                    # Convert model to schema for the population service
                    preferences_schema = CollectionPreferencesSchema.model_validate(preferences_model)

                    # Immediately fetch and populate properties
                    properties_added = await OpenHouseService._populate_collection_with_properties(
                        db, collection, preferences_schema
                    )
                    return {"success": True, "properties_added": properties_added, "collection_id": collection.id, "share_token": collection.share_token}
                else:
                    logger.warning(f"Failed to generate preferences for collection {collection.id}")
                    return {"success": True, "properties_added": 0, "collection_id": collection.id, "share_token": collection.share_token}

            except Exception as e:
                logger.error(f"Preference generation or population failed for collection {collection.id}: {str(e)}", exc_info=True)
                # Collection creation should still succeed even if preferences fail
                return {"success": True, "properties_added": 0, "collection_id": collection.id, "share_token": collection.share_token}

        except Exception as e:
            logger.error("creating collection for visitor failed", extra={"error": str(e)})
            await db.rollback()
            return {"success": False, "properties_added": 0}

    @staticmethod
    async def create_showcase_and_invite(
        db: AsyncSession,
        visitor: OpenHouseVisitor,
        form_data: OpenHouseFormSubmission,
        *,
        send_after: timedelta = timedelta(hours=1),
    ) -> Dict[str, Any]:
        """Builds the guest's showcase (create_collection_for_visitor - which also enforces the
        agent's plan) and, when one was actually created, schedules the guest's "your homes are
        ready" invite email. Shared by the live sign-in path (1 hour out) and the upgrade backfill
        (a few minutes out), so a backfilled guest gets the exact same showcase + email as one who
        signed in on a Premium agent's open house."""
        from app.models.database import ScheduledEmail
        from app.services.website.site_service import resolve_owner_public_base_url

        collection_result = await OpenHouseService.create_collection_for_visitor(db, visitor, form_data)
        if not (collection_result.get("success") and collection_result.get("share_token")):
            return collection_result

        try:
            property_data = await OpenHouseService.get_property_by_qr_code(db, form_data.open_house_event_id)
            if not property_data:
                return collection_result

            hosting_agent = None
            oh_event = (await db.execute(
                select(OpenHouseEvent).where(OpenHouseEvent.id == form_data.open_house_event_id)
            )).scalar_one_or_none()
            if oh_event:
                hosting_agent = (await db.execute(select(User).where(User.id == oh_event.agent_id))).scalar_one_or_none()
            agent_name = f"{hosting_agent.first_name or ''} {hosting_agent.last_name or ''}".strip() if hosting_agent else ""
            agent_email = (hosting_agent.email or "") if hosting_agent else ""
            # The agent's Studio -> Email settings: invite off = the Showcase is still built, no email;
            # "sooner" = 0 / 15 / 30 min instead of the 1-hour default (only for the live sign-in -
            # a backfill's few minutes stay as they are).
            from app.services.email_settings_service import DEFAULT_INVITE_DELAY, client_rule, invite_delay_minutes
            rule = client_rule(hosting_agent, "visitor_confirmation")
            if rule is not None and not rule["on"]:
                return collection_result
            if hosting_agent is not None and send_after == timedelta(minutes=DEFAULT_INVITE_DELAY):
                send_after = timedelta(minutes=invite_delay_minutes(hosting_agent))

            # IDX hosts get the showcase link on their own domain, same as the sign-in QR code.
            base_url = await resolve_owner_public_base_url(db, hosting_agent) if hosting_agent else os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'http://localhost:3000'))
            showcase_link = f"{base_url}/showcase/{collection_result['share_token']}"

            count = collection_result.get('properties_added', 0)
            prop_addr = property_data.get('address', 'the open house')
            # Reads like a person's follow-up, not a campaign (personal_email_service).
            street = " ".join(str(prop_addr or "").split(",")[0].split())
            subject = f"Following up from the open house at {street}" if street else "Following up from the open house"

            db.add(ScheduledEmail(
                recipient_email=visitor.email,
                subject=subject,
                template_name="visitor_confirmation",
                template_variables={
                    "visitor_name": visitor.full_name,
                    "first_name": (visitor.full_name or "").strip().split(" ")[0].title(),   # "Hi Olive,"
                    "property_address": prop_addr,
                    # The live Mailgun template still reads its old placeholder name - without this the
                    # guest read "Thanks for visiting the  open house today." (blank address).
                    "test_property_address": prop_addr,
                    "showcase_link": showcase_link,
                    "properties_count": count,
                    "agent_name": agent_name,
                    "agent_email": agent_email,
                    "agent_phone": await agent_public_phone(db, hosting_agent),
                    # one-click way out, shown in the footer (the List-Unsubscribe header has it too)
                    "Unsub": f"{os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'https://openhousepal.com')).rstrip('/')}/unsubscribe?email={quote(visitor.email or '', safe='')}",
                    **(await agent_email_vars(db, hosting_agent)),   # face, title, brokerage
                },
                scheduled_for=datetime.now(timezone.utc) + send_after,
                status="PENDING",
                owner_id=oh_event.agent_id,  # sent from the agent's own mailbox if connected
            ))
            await db.commit()
        except Exception as e:
            # The showcase itself already exists - a failed invite must never undo that.
            logger.error("scheduling showcase invite email failed", extra={"error": str(e)})
            await db.rollback()
        return collection_result

    @staticmethod
    async def listing_spotlights(db: AsyncSession, agent: User, open_houses: list) -> Dict[str, dict]:
        """Per open house the agent is hosting at their OWN listing (IDX only - Spotlights are an
        IDX feature): {open_house_id: {"url": published Spotlight URL or None, "draft_id": an
        unpublished one's id or None}}. Open houses that aren't at their own listing are left out,
        so a Spotlight made from someone else's listing never counts.

        "Own" = the MLS lists them as listing / co-listing agent, or the open house came to them as
        the listing owner. A manually-entered home (no MLS record) counts only when they've made a
        Spotlight for that same address - there's no listing to pre-pick for a new one."""
        from app.models.database import Property, PropertyWebsite
        from app.services import property_website_service as pws
        from app.utils.auth import owner_has_access

        if not agent or not open_houses or not owner_has_access(agent, {"IDX"}):
            return {}
        sites = (await db.execute(
            select(PropertyWebsite).where(PropertyWebsite.owner_id == agent.id).order_by(PropertyWebsite.updated_at.desc())
        )).scalars().all()
        live_key: Dict[str, Any] = {}
        draft_key: Dict[str, Any] = {}
        live_addr: Dict[str, Any] = {}
        for s in sites:  # newest first - the first one seen wins
            if s.listing_key:
                (live_key if s.status == "PUBLISHED" else draft_key).setdefault(s.listing_key, s)
            elif s.status == "PUBLISHED":
                addr = pws._normalize_address((s.property_snapshot or {}).get("address") or "")
                if addr:
                    live_addr.setdefault(addr, s)

        owned_keys = await OpenHouseService.owned_listing_keys(db, agent, {oh.listing_key for oh in open_houses if oh.listing_key})

        out: Dict[str, dict] = {}
        urls: Dict[str, str] = {}
        for oh in open_houses:
            if oh.agent_id != agent.id:
                continue
            if oh.listing_key:
                if not (oh.listing_key in owned_keys or oh.listing_owner_agent_id == agent.id):
                    continue
                site = live_key.get(oh.listing_key)
                draft = draft_key.get(oh.listing_key)
            else:
                site = live_addr.get(pws._normalize_address(oh.address or ""))
                draft = None
                if not site:
                    continue
            if site and site.id not in urls:
                urls[site.id] = await pws.public_url(db, site)
            out[oh.id] = {"url": urls[site.id] if site else None, "draft_id": draft.id if (draft and not site) else None}
        return out

    @staticmethod
    async def owned_listing_keys(db: AsyncSession, agent: User, keys: set) -> set:
        """The listing keys (of `keys`) the MLS lists this agent on as listing or co-listing agent."""
        from app.models.database import Property
        from app.utils.plan_ids import listing_owner_key
        keys = {k for k in keys if k}
        if not agent or not keys:
            return set()
        owner_key = listing_owner_key(agent)
        rows = (await db.execute(
            select(Property.listing_key, Property.list_agent_mls_id, Property.co_list_agent_mls_id)
            .where(Property.listing_key.in_(keys))
        )).all()
        return {r.listing_key for r in rows if owner_key in {r.list_agent_mls_id, r.co_list_agent_mls_id}}

    @staticmethod
    async def is_own_listing(db: AsyncSession, agent: User, listing_key: Optional[str]) -> bool:
        """The agent is this home's listing (or co-listing) agent."""
        return bool(listing_key) and listing_key in await OpenHouseService.owned_listing_keys(db, agent, {listing_key})

    @staticmethod
    async def guest_defaults(db: AsyncSession, agent: User, listing_key: Optional[str]) -> Dict[str, Any]:
        """What a new kit starts with. Sitting your OWN listing you want guests focused on it, not
        shopping other homes - so no Showcases and no Similar Homes (sheet or Guest Pass). Hosting
        someone else's listing (or a home with no MLS record) both start on."""
        own = await OpenHouseService.is_own_listing(db, agent, listing_key)
        return {"own_listing": own, "auto_showcase": not own, "similar_homes": not own}

    @staticmethod
    async def own_listing_spotlights(db: AsyncSession, agent: User, open_houses: list) -> Dict[str, str]:
        """{open_house_id: Spotlight URL} - just the ones with a published Spotlight."""
        info = await OpenHouseService.listing_spotlights(db, agent, open_houses)
        return {k: v["url"] for k, v in info.items() if v["url"]}

    @staticmethod
    async def agent_hub_link(db: AsyncSession, agent: User) -> Dict[str, Any]:
        """{"available": the plan includes an Agent Hub, "ready": it's set up enough to be public,
        "url": its public link when ready}. Same checks the public Hub page itself makes."""
        from app.models.database import AgentHub
        from app.utils.auth import owner_has_access, AGENT_HUB_ACCESS_TIERS
        from app.services.website.site_service import owner_links
        if not agent or not owner_has_access(agent, AGENT_HUB_ACCESS_TIERS):
            return {"available": False, "ready": False, "url": None}
        hub = (await db.execute(select(AgentHub).where(AgentHub.owner_id == agent.id))).scalar_one_or_none()
        from app.api.agent_hub_routes import _hub_has_minimum_content
        if not hub or not _hub_has_minimum_content(hub.config or {}):
            return {"available": True, "ready": False, "url": None}
        return {"available": True, "ready": True, "url": (await owner_links(db, agent, hub.slug)).hub()}

    @staticmethod
    def guest_pass_on(oh: OpenHouseEvent, host: Optional[User]) -> bool:
        """This open house gives guests a Guest Pass: switched on, and the host is on a Bright MLS
        plan (Basic / Premium / IDX) with a live subscription - it's built from MLS data."""
        from app.utils.auth import owner_has_access, MLS_ACCESS_TIERS
        return bool(oh is not None and oh.guest_pass is not False and host is not None
                    and owner_has_access(host, MLS_ACCESS_TIERS))

    @staticmethod
    async def after_signin_target(db: AsyncSession, oh: OpenHouseEvent, host: User) -> Optional[Dict[str, str]]:
        """No-MLS Pro only: {"kind": "hub", "url"} when the agent chose to send guests to their Agent
        Hub after signing in (and it's live), else None. Bright MLS plans get the Guest Pass instead
        (its link comes back with the sign-in itself - see guest_pass_on)."""
        from app.services.visit_source import with_src
        from app.utils.auth import owner_has_access, MLS_ACCESS_TIERS
        if not host or owner_has_access(host, MLS_ACCESS_TIERS) or oh.after_signin != "hub":
            return None
        url = (await OpenHouseService.agent_hub_link(db, host))["url"]
        return {"kind": "hub", "url": f"{with_src(url, 'signin')}&oh={oh.id}"} if url else None

    # How far back a missed showcase still counts. A guest from months ago getting a "homes you
    # might like" email out of nowhere would read as spam, not service.
    MISSED_SHOWCASE_WINDOW_DAYS = 90

    @staticmethod
    async def missed_showcases(db: AsyncSession, agent_id: str, *, limit: int = 200) -> list[dict]:
        """Open-house guests who said they DON'T have an agent, signed in at this agent's open
        houses within the window, and never got a showcase (the agent wasn't on a Showcase plan).
        This is the "you'd be getting these right now" backlog shown to Basic agents, and exactly
        the list backfill_missed_showcases() works through on upgrade. Derived live from the
        sign-in records - no separate bookkeeping to drift out of sync."""
        from sqlalchemy import func, and_, exists
        from app.models.database import Lead, LeadEvent, ScheduledEmail

        cutoff = datetime.now(timezone.utc) - timedelta(days=OpenHouseService.MISSED_SHOWCASE_WINDOW_DAYS)
        agent_email = (await db.execute(select(User.email).where(User.id == agent_id))).scalar_one_or_none() or ""
        guest_email = func.lower(OpenHouseVisitor.email)
        # A guest is only "missed" if they have NEVER had a Showcase or invite from this agent -
        # not merely "has no showcase right now". Three independent guards, so a guest can never
        # get a second invite even if the agent later deleted their showcase:
        #  1. a showcase exists for them now
        has_showcase = exists().where(and_(
            Collection.owner_id == agent_id,
            func.lower(Collection.visitor_email) == guest_email,
        ))
        #  2. a showcase was EVER created for them (the lead history keeps a collection_visitor
        #     event even after the showcase itself is deleted)
        ever_had_showcase = exists().where(and_(
            LeadEvent.lead_id == Lead.id,
            Lead.owner_id == agent_id,
            func.lower(Lead.email) == guest_email,
            LeadEvent.source_type == "collection_visitor",
        ))
        #  3. an invite email was ever queued/sent to them on this agent's behalf
        ever_invited = exists().where(and_(
            func.lower(ScheduledEmail.recipient_email) == guest_email,
            ScheduledEmail.template_name == "visitor_confirmation",
            ScheduledEmail.template_variables["agent_email"].astext == agent_email,
        ))
        rows = (await db.execute(
            select(OpenHouseVisitor, OpenHouseEvent.address)
            .join(OpenHouseEvent, OpenHouseEvent.id == OpenHouseVisitor.open_house_event_id)
            .where(
                OpenHouseEvent.agent_id == agent_id,
                OpenHouseEvent.auto_showcase.is_(True),
                OpenHouseVisitor.has_agent != "YES",
                OpenHouseVisitor.created_at >= cutoff,
                ~has_showcase,
                ~ever_had_showcase,
                ~ever_invited,
            )
            .order_by(OpenHouseVisitor.created_at.desc())
            .limit(limit)
        )).all()

        # One entry per guest - the same person signing in at two open houses is one showcase.
        seen: set[str] = set()
        out: list[dict] = []
        for visitor, address in rows:
            key = (visitor.email or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({
                "visitor_id": visitor.id,
                "name": visitor.full_name,
                "email": visitor.email,
                "open_house_event_id": visitor.open_house_event_id,
                "property_address": address,
                "signed_in_at": visitor.created_at.isoformat() if visitor.created_at else None,
            })
        return out

    @staticmethod
    async def backfill_missed_showcases(db: AsyncSession, agent_id: str) -> int:
        """Runs once an agent moves onto a Showcase plan: builds a showcase for every guest in
        missed_showcases() and sends each one their invite (a few minutes out, staggered so a
        big backlog doesn't land as one burst). Idempotent - missed_showcases() excludes anyone
        who already has a showcase, so a repeat call (webhook retry, second plan change) is a
        no-op. Returns how many showcases were created."""
        from app.schemas.open_house import HasAgent

        agent =(await db.execute(select(User).where(User.id == agent_id))).scalar_one_or_none()
        from app.utils.auth import owner_has_access, SHOWCASE_ACCESS_TIERS
        if not owner_has_access(agent, SHOWCASE_ACCESS_TIERS):
            return 0

        created = 0
        for index, item in enumerate(await OpenHouseService.missed_showcases(db, agent_id)):
            visitor = (await db.execute(
                select(OpenHouseVisitor).where(OpenHouseVisitor.id == item["visitor_id"])
            )).scalar_one_or_none()
            if not visitor or not visitor.open_house_event_id:
                continue
            try:
                form_data = OpenHouseFormSubmission(
                    full_name=visitor.full_name,
                    email=visitor.email,
                    phone=visitor.phone or "",
                    has_agent=HasAgent(visitor.has_agent) if visitor.has_agent in HasAgent._value2member_map_ else HasAgent.NO,
                    open_house_event_id=visitor.open_house_event_id,
                    agent_id=agent_id,
                )
            except Exception as e:
                # e.g. a legacy row with an email that no longer validates - skip just that one.
                logger.warning("skipping missed showcase backfill for visitor", extra={"visitor_id": visitor.id, "error": str(e)})
                continue
            result = await OpenHouseService.create_showcase_and_invite(
                db, visitor, form_data, send_after=timedelta(minutes=5 + index),
            )
            if result.get("success"):
                created += 1
        if created:
            logger.info("backfilled missed showcases after upgrade", extra={"agent_id": agent_id, "showcases_created": created})
        return created

    @staticmethod
    async def _populate_collection_with_properties(
        db: AsyncSession,
        collection: Collection,
        preferences: CollectionPreferencesSchema
    ) -> int:
        """
        Populate collection with properties from local mirror.
        Optimized with 'Smart Discovery' iterative radius tuning.
        """
        try:
            # 1. SMART DISCOVERY (Iterative Radius Tuning)
            current_radius = 6.0

            # Initial count check
            count = await property_service.get_properties_count_by_preferences(db, preferences)
            logger.info(f"Smart Discovery: Initial count at 6 miles for collection {collection.id} is {count}")

            if count > 30:
                for r in [3.0, 1.5]:
                    preferences.diameter = r
                    new_count = await property_service.get_properties_count_by_preferences(db, preferences)
                    # Only accept the smaller radius if it doesn't result in an empty collection
                    if new_count > 0:
                        current_radius = r
                        count = new_count

                    if count <= 30 or new_count == 0:
                        break
            elif count < 3:
                # Try expanding the search
                for r in [12.0, 20.0]:
                    preferences.diameter = r
                    new_count = await property_service.get_properties_count_by_preferences(db, preferences)
                    # Only accept the larger radius if it actually finds more properties
                    if new_count >= count:
                        current_radius = r
                        count = new_count

                    if count >= 3:
                        break

            # 2. SAVE SMART RADIUS
            await db.execute(
                update(CollectionPreferences)
                .where(CollectionPreferences.collection_id == collection.id)
                .values(diameter=current_radius)
            )
            await db.commit()

            # 3. FINAL POPULATE using standardized utility
            from app.services.collections_service import CollectionsService
            population_result = await CollectionsService.repopulate_collection_from_preferences(
                db, collection.id, commit=True
            )

            return population_result.get('properties_found', 0)

        except Exception as e:
            logger.error(f"Smart Discovery population failed: {e}", exc_info=True)
            return 0

    @staticmethod
    async def get_open_house_event_by_id(db: AsyncSession, open_house_event_id: str) -> Optional[dict]:
        """Get open house event details by ID from database"""
        try:
            stmt = select(OpenHouseEvent).where(OpenHouseEvent.id == open_house_event_id)
            result = await db.execute(stmt)
            open_house_record = result.scalar_one_or_none()

            if not open_house_record:
                return None

            return {
                "id": open_house_record.id,
                "agent_id": open_house_record.agent_id,
                "address": open_house_record.address,
                "abbreviated_address": open_house_record.abbreviated_address,
                "city": open_house_record.city,
                "state": open_house_record.state,
                "zipCode": open_house_record.zipcode,
                "price": open_house_record.price,
                "beds": open_house_record.bedrooms,
                "baths": open_house_record.bathrooms,
                "squareFeet": open_house_record.living_area,
                "lotSize": open_house_record.lot_size,
                "propertyType": open_house_record.house_type,
                "homeStatus": open_house_record.home_status,
                "imageSrc": open_house_record.cover_image_url,
                "listingKey": open_house_record.listing_key,
                "created_at": open_house_record.created_at
            }

        except Exception as e:
            logger.error(f"Failed to fetch open house event: {e}")
            return None

    @staticmethod
    async def get_property_by_qr_code(db: AsyncSession, qr_code: str) -> Optional[dict]:
        """Get property information by open house event ID"""
        return await OpenHouseService.get_open_house_event_by_id(db, qr_code)
