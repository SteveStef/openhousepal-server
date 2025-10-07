from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import List, Optional
from datetime import datetime

from app.models.database import PropertyTour, Collection, Property, User
from app.schemas.property_tour import (
    PropertyTourCreate,
    PropertyTourResponse,
    PropertyTourStatusUpdate
)
from app.utils.emails import send_simple_message


class PropertyTourService:
    """Service for managing property tour requests within collections"""

    @classmethod
    async def create_tour_request(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str,
        tour_data: PropertyTourCreate
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

        # Get visitor information from collection
        if not collection.visitor_name or not collection.visitor_email or not collection.visitor_phone:
            raise ValueError("Collection does not have complete visitor information")

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

        # Create tour request using visitor info from collection
        current_time = datetime.now()
        tour = PropertyTour(
            collection_id=collection_id,
            property_id=property_id,
            visitor_name=collection.visitor_name,
            visitor_email=collection.visitor_email,
            visitor_phone=collection.visitor_phone,
            preferred_date=tour_data.preferred_date,
            preferred_time=tour_data.preferred_time,
            preferred_date_2=tour_data.preferred_date_2,
            preferred_time_2=tour_data.preferred_time_2,
            preferred_date_3=tour_data.preferred_date_3,
            preferred_time_3=tour_data.preferred_time_3,
            message=tour_data.message,
            status="PENDING",
            created_at=current_time,
            updated_at=current_time
        )

        db.add(tour)
        await db.commit()
        await db.refresh(tour)

        # Send email notification to the agent
        try:
            # Get agent info from collection owner
            agent_result = await db.execute(
                select(User).where(User.id == collection.owner_id)
            )
            agent = agent_result.scalar_one_or_none()

            if agent and agent.email:
                # Build email content
                email_body = f"""You have a new tour request!

Property: {property_obj.street_address}, {property_obj.city}, {property_obj.state}
Collection: {collection.name}

Visitor Information:
Name: {tour.visitor_name}
Email: {tour.visitor_email}
Phone: {tour.visitor_phone}

Preferred Times:
1. {tour.preferred_date} at {tour.preferred_time}"""

                # Add second preferred time if provided
                if tour.preferred_date_2 and tour.preferred_time_2:
                    email_body += f"\n2. {tour.preferred_date_2} at {tour.preferred_time_2}"

                # Add third preferred time if provided
                if tour.preferred_date_3 and tour.preferred_time_3:
                    email_body += f"\n3. {tour.preferred_date_3} at {tour.preferred_time_3}"

                # Add message if provided
                if tour.message:
                    email_body += f"\n\nMessage from visitor:\n{tour.message}"

                email_body += "\n\nPlease contact the visitor to confirm the tour."

                # Send the email
                status_code, response = send_simple_message(
                    from_email="noreply@entrypoint.com",
                    to_email=agent.email,
                    subject=f"New Tour Request for {property_obj.street_address}",
                    message=email_body
                )

                print(f"Email sent to agent {agent.email}: Status {status_code}")

        except Exception as e:
            # Log error but don't fail the tour request
            print(f"Error sending tour notification email: {e}")

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
    async def update_tour_status(
        cls,
        db: AsyncSession,
        tour_id: str,
        status_update: PropertyTourStatusUpdate,
        user_id: str
    ) -> Optional[PropertyTourResponse]:
        """Update the status of a tour request (agent only)"""

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

        # Validate status
        valid_statuses = ["PENDING", "CONFIRMED", "COMPLETED", "CANCELLED"]
        if status_update.status not in valid_statuses:
            raise ValueError(f"Invalid status. Must be one of: {', '.join(valid_statuses)}")

        # Update status
        tour.status = status_update.status
        tour.updated_at = datetime.now()

        await db.commit()
        await db.refresh(tour)

        return PropertyTourResponse.from_orm(tour)
