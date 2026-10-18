from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update
from typing import List, Optional
from datetime import datetime, timezone

from app.services.email_avatar_service import agent_email_vars
from app.models.database import PropertyTour, Collection, Property, User, Notification, LeadEvent
from app.schemas.property_tour import (
    PropertyTourCreate,
    PropertyTourResponse,
    PropertyTourCompletionUpdate,
    PropertyTourConfirmUpdate
)
from app.services.email_service import EmailService
from app.services.leads_service import upsert_lead
from app.services import calendar_sync_service
from app.config.logging import get_logger
from app.services.agent_email_service import agent_public_phone

logger = get_logger(__name__)


class PropertyTourService:
    """Service for managing property tour requests within collections"""

    @staticmethod
    async def _mark_tour_event_viewed(db: AsyncSession, tour_id: str) -> None:
        """Clear the lead card's "showing request" tag for this specific tour - mirrors the
        agent_viewed_at signal mark_collection_seen already sets when the agent opens the
        showcase, but scoped to just this tour's LeadEvent (source_id == tour.id) so confirming
        or completing a tour from the Leads/Open-House-Manager UI (without ever opening the
        showcase itself) also dismisses it, instead of the icon sticking around forever."""
        await db.execute(
            update(LeadEvent)
            .where(
                LeadEvent.source_type == 'property_tour',
                LeadEvent.source_id == tour_id,
                LeadEvent.agent_viewed_at.is_(None),
            )
            .values(agent_viewed_at=datetime.now(timezone.utc))
        )

    @staticmethod
    def _format_date(date_str: str) -> str:
        """YYYY-MM-DD -> "Saturday, October 10" (how an agent reads a showing request)."""
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            return f"{date_obj.strftime('%A, %B')} {date_obj.day}"
        except:
            return date_str  # Return original if parsing fails

    @staticmethod
    def _format_time(time_str: str) -> str:
        """Convert HH:MM (24-hour) to 12-hour AM/PM format"""
        try:
            time_obj = datetime.strptime(time_str, "%H:%M")
            # %-I (no leading zero) is glibc-only - build it by hand so it works on every OS.
            return f"{time_obj.hour % 12 or 12}:{time_obj.minute:02d} {'AM' if time_obj.hour < 12 else 'PM'}"
        except:
            return time_str  # Return original if parsing fails

    @classmethod
    async def create_tour_request(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str,
        tour_data: PropertyTourCreate,
        user_id: Optional[str] = None
    ) -> PropertyTourResponse:
        """Create a new property tour request"""

        # Verify collection exists and is public
        collection_result = await db.execute(
            select(Collection).where(Collection.id == collection_id)
        )
        collection = collection_result.scalar_one_or_none()

        if not collection:
            raise ValueError("Collection not found")

        if not collection.is_public:
            raise ValueError("Collection is not publicly accessible")

        # Determine visitor information (from collection or from request)
        visitor_name = collection.visitor_name or tour_data.visitor_name
        visitor_email = collection.visitor_email or tour_data.visitor_email
        visitor_phone = collection.visitor_phone or tour_data.visitor_phone

        # Get visitor information - if still missing, then fail
        if not visitor_name or not visitor_email:
            raise ValueError("Visitor name and email are required to schedule a tour")

        # Update collection with visitor info if it was missing but provided in request
        needs_update = False
        if not collection.visitor_name and tour_data.visitor_name:
            collection.visitor_name = tour_data.visitor_name
            needs_update = True
        if not collection.visitor_email and tour_data.visitor_email:
            collection.visitor_email = tour_data.visitor_email
            needs_update = True
        if not collection.visitor_phone and tour_data.visitor_phone:
            collection.visitor_phone = tour_data.visitor_phone
            needs_update = True
            
        if needs_update:
            db.add(collection)
            await db.commit()
            await db.refresh(collection)

        # Verify property exists
        property_result = await db.execute(
            select(Property).where(Property.id == property_id)
        )
        property_obj = property_result.scalar_one_or_none()

        if not property_obj:
            raise ValueError("Property not found")

        # Check if a tour already exists for this collection + property
        existing_tour_result = await db.execute(
            select(PropertyTour).where(
                and_(
                    PropertyTour.collection_id == collection_id,
                    PropertyTour.property_id == property_id
                )
            )
        )
        existing_tour = existing_tour_result.scalar_one_or_none()

        if existing_tour:
            raise ValueError("A tour has already been requested for this property")

        # Create tour request using visitor info
        current_time = datetime.now(timezone.utc)
        tour = PropertyTour(
            collection_id=collection_id,
            property_id=property_id,
            visitor_name=visitor_name,
            visitor_email=visitor_email,
            visitor_phone=visitor_phone,
            preferred_date=tour_data.preferred_date,
            preferred_time=tour_data.preferred_time,
            preferred_date_2=tour_data.preferred_date_2,
            preferred_time_2=tour_data.preferred_time_2,
            preferred_date_3=tour_data.preferred_date_3,
            preferred_time_3=tour_data.preferred_time_3,
            message=tour_data.message,
            is_completed=False,
            created_at=current_time,
            updated_at=current_time
        )

        db.add(tour)
        await db.commit()
        await db.refresh(tour)

        # Same "did a visitor actually engage with this showcase" signal every other visitor
        # action updates (views, likes, comments) - tour requests were missing from it.
        if user_id is None:
            try:
                from sqlalchemy import update
                await db.execute(
                    update(Collection)
                    .where(Collection.id == collection_id)
                    .values(last_visitor_activity_at=current_time)
                )
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to update collection visitor activity timestamp: {e}")

        tour_address = f"{property_obj.street_address}, {property_obj.city}, {property_obj.state} {property_obj.zipcode or ''}".strip()

        # Surface the preferred date/time options and any message on the lead's activity timeline
        # (rendered from raw_snapshot.answers), not just the bare "requested a showing" summary.
        tour_answers = []
        for label, pref_date, pref_time in (
            ("Preferred Time 1", tour_data.preferred_date, tour_data.preferred_time),
            ("Preferred Time 2", tour_data.preferred_date_2, tour_data.preferred_time_2),
            ("Preferred Time 3", tour_data.preferred_date_3, tour_data.preferred_time_3),
        ):
            if pref_date and pref_time:
                tour_answers.append({"label": label, "value": f"{cls._format_date(pref_date)} at {cls._format_time(pref_time)}"})
        if tour_data.message:
            tour_answers.append({"label": "Message", "value": tour_data.message})

        await upsert_lead(
            db,
            owner_id=collection.owner_id,
            name=visitor_name,
            email=visitor_email,
            phone=visitor_phone,
            source_type="property_tour",
            source_id=tour.id,
            summary=f"Requested a showing - {tour_address}",
            occurred_at=current_time,
            property_address=tour_address,
            collection_id=collection_id,
            raw_snapshot={"answers": tour_answers} if tour_answers else None,
        )

        # Send email notification to the agent
        agent_result = await db.execute(
            select(User).where(User.id == collection.owner_id)
        )
        agent = agent_result.scalar_one_or_none()
        if agent and agent.email:
            # Build preferred dates list with formatted dates and times
            preferred_dates = []
            if tour_data.preferred_date and tour_data.preferred_time:
                formatted_date = cls._format_date(tour_data.preferred_date)
                formatted_time = cls._format_time(tour_data.preferred_time)
                preferred_dates.append(f"{formatted_date} at {formatted_time}")
            if tour_data.preferred_date_2 and tour_data.preferred_time_2:
                formatted_date = cls._format_date(tour_data.preferred_date_2)
                formatted_time = cls._format_time(tour_data.preferred_time_2)
                preferred_dates.append(f"{formatted_date} at {formatted_time}")
            if tour_data.preferred_date_3 and tour_data.preferred_time_3:
                formatted_date = cls._format_date(tour_data.preferred_date_3)
                formatted_time = cls._format_time(tour_data.preferred_time_3)
                preferred_dates.append(f"{formatted_date} at {formatted_time}")

            email_service = EmailService()
            
            # Create full address string
            full_address = f"{property_obj.street_address}, {property_obj.city}, {property_obj.state} {property_obj.zipcode or ''}".strip()
            
            from app.services.notification_preferences_service import channel_enabled
            if channel_enabled(agent, "TOUR_REQUEST", "email"):
              from app.services.agent_email_service import send_simple_bg
              send_simple_bg(
                to_email=agent.email,
                subject=f"New Tour Request - {property_obj.street_address}",
                template="tour_request",
                template_variables={
                    "agent_name": agent.first_name,
                    "visitor_name": visitor_name,
                    "visitor_email": visitor_email,
                    "visitor_phone": visitor_phone or "",
                    "property_address": full_address,
                    "property_image": property_obj.img_src,
                    "preferred_dates": ", ".join(preferred_dates) if preferred_dates else "No specific dates provided",
                    "message": tour_data.message or ""
                }
            )

        # Create in-app notification for agent
        try:
            # Skip notification if the user is the agent (owner) themselves
            if not (user_id and user_id == collection.owner_id):
                # Format first preferred date for notification
                preferred_date_str = ""
                if tour_data.preferred_date and tour_data.preferred_time:
                    formatted_date = cls._format_date(tour_data.preferred_date)
                    formatted_time = cls._format_time(tour_data.preferred_time)
                    preferred_date_str = f" for {formatted_date} at {formatted_time}"

                notification = Notification(
                    agent_id=collection.owner_id,
                    type="TOUR_REQUEST",
                    reference_type="TOUR",
                    reference_id=tour.id,
                    title=f"New Tour Request: {visitor_name}",
                    message=f"Requested tour at {property_obj.street_address}{preferred_date_str}",
                    collection_id=collection.id,
                    collection_name=collection.name,
                    property_id=property_id,
                    property_address=property_obj.street_address,
                    visitor_name=visitor_name,
                    link=f"/showcases?showcase={collection.id}&property={property_id}",
                    is_read=False,
                    created_at=datetime.now(timezone.utc)
                )
                db.add(notification)
                await db.commit()
        except Exception as e:
            logger.error("Failed to create tour request notification", extra={"error": str(e)})
            # Don't fail the tour request if notification creation fails

        return PropertyTourResponse.from_orm(tour)

    @classmethod
    async def get_collection_tours(
        cls,
        db: AsyncSession,
        collection_id: str
    ) -> List[PropertyTourResponse]:
        """Get all tour requests for a collection"""

        result = await db.execute(
            select(PropertyTour)
            .where(PropertyTour.collection_id == collection_id)
            .order_by(PropertyTour.created_at.desc())
        )
        tours = result.scalars().all()

        return [PropertyTourResponse.from_orm(tour) for tour in tours]

    @classmethod
    async def get_property_tours(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str
    ) -> List[PropertyTourResponse]:
        """Get all tour requests for a specific property in a collection"""

        result = await db.execute(
            select(PropertyTour)
            .where(
                and_(
                    PropertyTour.collection_id == collection_id,
                    PropertyTour.property_id == property_id
                )
            )
            .order_by(PropertyTour.created_at.desc())
        )
        tours = result.scalars().all()

        return [PropertyTourResponse.from_orm(tour) for tour in tours]

    @classmethod
    async def get_tour_by_id(
        cls,
        db: AsyncSession,
        tour_id: str
    ) -> Optional[PropertyTourResponse]:
        """Get a specific tour request by ID"""

        result = await db.execute(
            select(PropertyTour).where(PropertyTour.id == tour_id)
        )
        tour = result.scalar_one_or_none()

        if not tour:
            return None

        return PropertyTourResponse.from_orm(tour)

    @classmethod
    async def update_tour_completion(
        cls,
        db: AsyncSession,
        tour_id: str,
        completion_update: PropertyTourCompletionUpdate,
        user_id: str
    ) -> Optional[PropertyTourResponse]:
        """Update the completion flag of a tour request (agent only)"""

        # Get the tour
        result = await db.execute(
            select(PropertyTour).where(PropertyTour.id == tour_id)
        )
        tour = result.scalar_one_or_none()

        if not tour:
            return None

        # Verify the user owns the collection
        collection_result = await db.execute(
            select(Collection).where(
                and_(
                    Collection.id == tour.collection_id,
                    Collection.owner_id == user_id
                )
            )
        )
        collection = collection_result.scalar_one_or_none()

        if not collection:
            raise ValueError("Unauthorized to update this tour request")

        # Update completion flag
        tour.is_completed = completion_update.is_completed
        tour.updated_at = datetime.now(timezone.utc)

        if completion_update.is_completed:
            await cls._mark_tour_event_viewed(db, tour_id)

        await db.commit()
        await db.refresh(tour)

        # Keep the calendar in step: a completed tour comes off, a reopened one goes back on
        # (only matters when it was confirmed - _sync_tour no-ops otherwise).
        if tour.confirmed_date and tour.confirmed_time:
            calendar_sync_service.sync_tour_bg(user_id, tour_id)
        # Done = handled: its "Tour 12 Oak St" follow-up is checked off and off the calendar too.
        if completion_update.is_completed:
            from app.services.tasks_service import complete_request_followups
            await complete_request_followups(db, user_id, source_ids=(tour_id,), rules={"showing_request"})
            await db.refresh(tour)

        return PropertyTourResponse.from_orm(tour)

    @classmethod
    async def confirm_tour(
        cls,
        db: AsyncSession,
        tour_id: str,
        confirm_update: PropertyTourConfirmUpdate,
        user_id: str
    ) -> Optional[PropertyTourResponse]:
        """Confirm a tour request with the agreed date/time (agent only) - this is what makes
        it show as an "Upcoming Tour" on the visitor's showcase."""

        result = await db.execute(
            select(PropertyTour).where(PropertyTour.id == tour_id)
        )
        tour = result.scalar_one_or_none()

        if not tour:
            return None

        collection_result = await db.execute(
            select(Collection).where(
                and_(
                    Collection.id == tour.collection_id,
                    Collection.owner_id == user_id
                )
            )
        )
        collection = collection_result.scalar_one_or_none()

        if not collection:
            raise ValueError("Unauthorized to update this tour request")

        # The time goes into the client's confirmation email and the agent's calendar - a date/time
        # that doesn't parse used to be saved as-is (and printed as "Invalid Date" on the Showcase).
        try:
            datetime.strptime(f"{confirm_update.confirmed_date} {confirm_update.confirmed_time}", "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            raise ValueError("Pick a valid date and time.")

        tour.confirmed_date = confirm_update.confirmed_date
        tour.confirmed_time = confirm_update.confirmed_time
        tour.updated_at = datetime.now(timezone.utc)

        await cls._mark_tour_event_viewed(db, tour_id)

        await db.commit()
        await db.refresh(tour)

        await cls._email_tour_confirmed(db, tour, collection)

        # Push the confirmed tour onto the agent's Google Calendar right now (bounded) so the UI
        # can report exactly what happened - added, no calendar connected, or a real failure.
        resp = PropertyTourResponse.from_orm(tour)
        try:
            resp.calendar_status = await calendar_sync_service.sync_tour_now(user_id, tour_id)
        except Exception:
            resp.calendar_status = "error"
        return resp

    @staticmethod
    async def _email_tour_confirmed(db: AsyncSession, tour: PropertyTour, collection: Collection) -> None:
        """The buyer's "your tour is confirmed" email - from the agent's name, replies go to the
        agent. Sent every time the agent sets (or changes) the confirmed time. Never raises."""
        try:
            from app.services.agent_email_service import email_person
            from app.services.blacklist_service import BlacklistService
            from app.services.website.site_service import resolve_owner_public_base_url
            to = tour.visitor_email or collection.visitor_email
            if not to or await BlacklistService.is_blacklisted(db, to):
                return
            agent = await db.get(User, collection.owner_id)
            prop = await db.get(Property, tour.property_id)
            if agent is None or prop is None:
                return
            try:
                when = datetime.strptime(f"{tour.confirmed_date} {tour.confirmed_time}", "%Y-%m-%d %H:%M")
                date_label = f"{when.strftime('%A, %B')} {when.day}"
                time_label = when.strftime("%I:%M %p").lstrip("0")
            except ValueError:
                date_label, time_label = tour.confirmed_date or "", tour.confirmed_time or ""
            agent_name = f"{agent.first_name or ''} {agent.last_name or ''}".strip()
            base = await resolve_owner_public_base_url(db, agent)
            first = (tour.visitor_name or collection.visitor_name or "").strip().split(" ")[0]
            from app.services.agent_roundup_service import nice_street
            street = nice_street(prop.street_address, prop.city)
            from app.services.showcase_links import showcase_path, home_suffix
            # A real calendar invite for the client (calendar_invite_service).
            from app.services.calendar_invite_service import build_invite, eastern_start
            phone = await agent_public_phone(db, agent)
            start = eastern_start(tour.confirmed_date, tour.confirmed_time)
            address = prop.full_address or ", ".join(p for p in (prop.street_address, prop.city, prop.state) if p)
            ics = build_invite(uid=f"tour-{tour.id}", start=start, street=street, address=address, agent_name=agent_name,
                               agent_email=agent.email, agent_phone=phone, client_name=tour.visitor_name or collection.visitor_name,
                               client_email=to, link=f"{base}{await showcase_path(collection)}{home_suffix(prop.street_address)}") if start else None
            email_person(to, "tour_confirmed", f"Your tour of {street} is confirmed - {date_label} at {time_label}", {
                "_ics": ics,
                "recipient_name": first[:1].upper() + first[1:] if first else "there",
                "street_address": street, "city": (prop.city or "").title(), "state": prop.state or "",
                "image": prop.img_src or "", "price": f"${prop.price:,.0f}" if prop.price else "",
                "date_label": date_label, "time_label": time_label,
                "agent_name": agent_name, "agent_email": agent.email or "", "agent_phone": await agent_public_phone(db, agent),
                "collection_link": f"{base}{await showcase_path(collection)}",
                **(await agent_email_vars(db, agent)),   # face, title, brokerage
            }, reply_to=agent.email, from_name=agent_name or "OpenHousePal", owner_id=agent.id)
        except Exception:  # noqa: BLE001 - an email must never break confirming the tour
            logger.warning("tour confirmed email failed", exc_info=True)

    @classmethod
    async def unconfirm_tour(
        cls,
        db: AsyncSession,
        tour_id: str,
        user_id: str
    ) -> Optional[PropertyTourResponse]:
        """Clear a tour's confirmation (agent only) - e.g. the agreed time fell through."""

        result = await db.execute(
            select(PropertyTour).where(PropertyTour.id == tour_id)
        )
        tour = result.scalar_one_or_none()

        if not tour:
            return None

        collection_result = await db.execute(
            select(Collection).where(
                and_(
                    Collection.id == tour.collection_id,
                    Collection.owner_id == user_id
                )
            )
        )
        collection = collection_result.scalar_one_or_none()

        if not collection:
            raise ValueError("Unauthorized to update this tour request")

        tour.confirmed_date = None
        tour.confirmed_time = None
        tour.updated_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(tour)

        # Pull the event back off the agent's calendar now that it's no longer a confirmed tour.
        calendar_sync_service.unsync_tour_bg(user_id, tour_id)
        return PropertyTourResponse.from_orm(tour)
