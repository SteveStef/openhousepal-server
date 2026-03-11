from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import List, Optional
from datetime import datetime

from app.models.database import PropertyTour, Collection, Property, User, Notification
from app.schemas.property_tour import (
    PropertyTourCreate,
    PropertyTourResponse,
    PropertyTourCompletionUpdate
)
from app.services.email_service import EmailService
from app.config.logging import get_logger

logger = get_logger(__name__)


class PropertyTourService:
    """Service for managing property tour requests within collections"""

    @staticmethod
    def _format_date(date_str: str) -> str:
        """Convert YYYY-MM-DD to MM/DD/YYYY"""
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            return date_obj.strftime("%m/%d/%Y")
        except:
            return date_str  # Return original if parsing fails

    @staticmethod
    def _format_time(time_str: str) -> str:
        """Convert HH:MM (24-hour) to 12-hour AM/PM format"""
        try:
            time_obj = datetime.strptime(time_str, "%H:%M")
            return time_obj.strftime("%-I:%M %p")  # %-I removes leading zero from hour
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
        current_time = datetime.now()
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
            
            email_service.send_simple_message(
                to_email=agent.email,
                subject=f"New Tour Request - {property_obj.street_address}",
                template="tour_request",
                template_variables={
                    "agent_name": agent.first_name,
                    "visitor_name": visitor_name,
                    "visitor_email": visitor_email,
                    "visitor_phone": visitor_phone or "Not provided",
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
                    created_at=datetime.utcnow()
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
        tour.updated_at = datetime.now()

        await db.commit()
        await db.refresh(tour)

        return PropertyTourResponse.from_orm(tour)
