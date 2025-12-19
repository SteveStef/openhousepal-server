from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import List, Optional
from datetime import datetime
import os

from app.models.database import PropertyInteraction, PropertyComment, Collection, Property, User, Notification
from app.schemas.property_interactions import (
    PropertyInteractionUpdate,
    PropertyCommentCreate,
    PropertyInteractionResponse,
    PropertyCommentResponse,
    PropertyInteractionStats,
    PropertyInteractionSummary
)
from app.services.email_service import EmailService
from app.config.logging import get_logger

logger = get_logger(__name__)


class PropertyInteractionsService:
    """Service for managing anonymous property interactions within collections"""
    
    @classmethod
    async def create_property_interaction(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str,
        interaction_data: PropertyInteractionUpdate,
        user_id: Optional[str] = None
    ) -> PropertyInteractionResponse:
        """Create or update a property interaction"""

        # Try to find existing interaction for this collection and property
        result = await db.execute(
            select(PropertyInteraction)
            .where(
                and_(
                    PropertyInteraction.collection_id == collection_id,
                    PropertyInteraction.property_id == property_id
                )
            )
        )
        interaction = result.scalar_one_or_none()

        current_time = datetime.now()

        if interaction:
            # Update existing interaction
            interaction.liked = interaction_data.liked if interaction_data.liked is not None else interaction.liked
            interaction.disliked = interaction_data.disliked if interaction_data.disliked is not None else interaction.disliked
            interaction.updated_at = current_time
        else:
            # Create new interaction
            interaction = PropertyInteraction(
                collection_id=collection_id,
                property_id=property_id,
                liked=interaction_data.liked or False,
                disliked=interaction_data.disliked or False,
                created_at=current_time,
                updated_at=current_time
            )
            db.add(interaction)

        # Apply mutual exclusivity rules (liked and disliked can't both be true)
        if interaction.liked and interaction.disliked:
            if interaction_data.liked:
                interaction.disliked = False
            else:
                interaction.liked = False

        await db.commit()
        await db.refresh(interaction)

        # Send email to agent if visitor liked the property
        if interaction.liked:
            collection_result = await db.execute(
                select(Collection).where(Collection.id == collection_id)
            )
            collection = collection_result.scalar_one_or_none()

            if collection:
                agent_result = await db.execute(
                    select(User).where(User.id == collection.owner_id)
                )
                agent = agent_result.scalar_one_or_none()

                property_result = await db.execute(
                    select(Property).where(Property.id == property_id)
                )
                property_obj = property_result.scalar_one_or_none()

                if agent and agent.email and property_obj:
                    frontend_url = os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'http://localhost:3000'))
                    collection_link = f"{frontend_url}/showcases?showcase={collection_id}"

                    email_service = EmailService()
                    email_service.send_simple_message(
                        to_email=agent.email,
                        subject=f"A Visitor Liked a Property - {property_obj.street_address}",
                        template="visitor_liked_property",
                        template_variables={
                            "agent_name": agent.first_name,
                            "visitor_name": collection.visitor_name or "A visitor",
                            "property_address": property_obj.street_address,
                            "collection_link": collection_link
                        }
                    )

        # Create in-app notification for property interactions (like, dislike)
        if interaction.liked or interaction.disliked:
            try:
                # Get collection and agent info for notification
                collection_result = await db.execute(
                    select(Collection).where(Collection.id == collection_id)
                )
                collection = collection_result.scalar_one_or_none()

                if collection:
                    # Skip notification if the user is the agent (owner) themselves
                    if user_id and user_id == collection.owner_id:
                        return PropertyInteractionResponse.from_orm(interaction)

                    property_result = await db.execute(
                        select(Property).where(Property.id == property_id)
                    )
                    property_obj = property_result.scalar_one_or_none()

                    if property_obj:
                        # Determine interaction type for notification message
                        if interaction.liked:
                            interaction_type = "liked"
                            title = f"{collection.visitor_name or 'A visitor'} liked a property"
                        elif interaction.disliked:
                            interaction_type = "disliked"
                            title = f"{collection.visitor_name or 'A visitor'} disliked a property"
                        else:
                            interaction_type = "interacted with"
                            title = f"{collection.visitor_name or 'A visitor'} interacted with a property"

                        notification = Notification(
                            agent_id=collection.owner_id,
                            type="PROPERTY_INTERACTION",
                            reference_type="INTERACTION",
                            reference_id=interaction.id,
                            title=title,
                            message=f"{interaction_type.capitalize()} {property_obj.street_address}",
                            collection_id=collection_id,
                            collection_name=collection.name,
                            property_id=property_id,
                            property_address=property_obj.street_address,
                            visitor_name=collection.visitor_name,
                            link=f"/showcases?showcase={collection_id}&property={property_id}",
                            is_read=False,
                            created_at=datetime.utcnow()
                        )
                        db.add(notification)
                        await db.commit()
            except Exception as e:
                logger.error("Failed to create property interaction notification", extra={"error": str(e)})
                # Don't fail the interaction if notification creation fails

        return PropertyInteractionResponse.from_orm(interaction)

    @classmethod
    async def track_property_view(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str
    ) -> PropertyInteractionResponse:
        """Track a property view by incrementing view_count and updating last_viewed_at"""

        # Try to find existing interaction for this collection and property
        result = await db.execute(
            select(PropertyInteraction)
            .where(
                and_(
                    PropertyInteraction.collection_id == collection_id,
                    PropertyInteraction.property_id == property_id
                )
            )
        )
        interaction = result.scalar_one_or_none()

        current_time = datetime.now()

        if interaction:
            # Update existing interaction
            interaction.view_count = (interaction.view_count or 0) + 1
            interaction.last_viewed_at = current_time
            interaction.updated_at = current_time
        else:
            # Create new interaction with view tracking
            interaction = PropertyInteraction(
                collection_id=collection_id,
                property_id=property_id,
                liked=False,
                disliked=False,
                view_count=1,
                last_viewed_at=current_time,
                created_at=current_time,
                updated_at=current_time
            )
            db.add(interaction)

        await db.commit()
        await db.refresh(interaction)

        return PropertyInteractionResponse.from_orm(interaction)

    @classmethod
    async def add_property_comment(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str,
        comment_data: PropertyCommentCreate,
        user_id: Optional[str] = None
    ) -> PropertyCommentResponse:
        """Add an anonymous comment to a property"""

        content = comment_data.content or comment_data.comment
        if not content:
            raise ValueError("Comment content is required")

        current_time = datetime.now()
        comment = PropertyComment(
            collection_id=collection_id,
            property_id=property_id,
            content=content,
            visitor_name=comment_data.visitor_name,
            visitor_email=comment_data.visitor_email,
            created_at=current_time,
            updated_at=current_time
        )

        db.add(comment)
        await db.commit()
        await db.refresh(comment)

        # Send email notification to agent
        collection_result = await db.execute(
            select(Collection).where(Collection.id == collection_id)
        )
        collection = collection_result.scalar_one_or_none()

        if collection:
            agent_result = await db.execute(
                select(User).where(User.id == collection.owner_id)
            )
            agent = agent_result.scalar_one_or_none()

            property_result = await db.execute(
                select(Property).where(Property.id == property_id)
            )
            property_obj = property_result.scalar_one_or_none()

            if agent and agent.email and property_obj:
                frontend_url = os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'http://localhost:3000'))
                collection_link = f"{frontend_url}/showcases"

                email_service = EmailService()
                email_service.send_simple_message(
                    to_email=agent.email,
                    subject=f"New Comment on Property - {property_obj.street_address}",
                    template="property_comment",
                    template_variables={
                        "recipient_name": agent.first_name,
                        "commenter_name": comment.visitor_name or "A visitor",
                        "property_address": property_obj.street_address,
                        "comment_text": content,
                        "collection_link": collection_link
                    }
                )

                # Create in-app notification for agent
                try:
                    # Skip notification if the user is the agent (owner) themselves
                    if user_id and user_id == collection.owner_id:
                        # Don't create notification for agent's own comment
                        pass
                    else:
                        # Truncate comment for notification if it's too long
                        comment_preview = content[:100] + "..." if len(content) > 100 else content

                        notification = Notification(
                            agent_id=collection.owner_id,
                            type="PROPERTY_COMMENT",
                            reference_type="COMMENT",
                            reference_id=comment.id,
                            title=f"New Comment: {comment.visitor_name or 'Anonymous'}",
                            message=f"Commented on {property_obj.street_address}: \"{comment_preview}\"",
                            collection_id=collection_id,
                            collection_name=collection.name,
                            property_id=property_id,
                            property_address=property_obj.street_address,
                            visitor_name=comment.visitor_name,
                            link=f"/showcases?showcase={collection_id}&property={property_id}",
                            is_read=False,
                            created_at=datetime.utcnow()
                        )
                        db.add(notification)
                        await db.commit()
                except Exception as e:
                    logger.error("Failed to create property comment notification", extra={"error": str(e)})
                    # Don't fail the comment creation if notification creation fails

        # Create response and populate author field from visitor_name
        response = PropertyCommentResponse.from_orm(comment)
        response.author = comment.visitor_name or "Anonymous"

        return response
    
    @classmethod
    async def get_property_comments(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str
    ) -> List[PropertyCommentResponse]:
        """Get all comments for a property in a collection"""

        result = await db.execute(
            select(PropertyComment)
            .where(
                and_(
                    PropertyComment.collection_id == collection_id,
                    PropertyComment.property_id == property_id
                )
            )
            .order_by(PropertyComment.created_at.desc())
        )
        comments = result.scalars().all()

        # Convert to response models and populate author field
        response_comments = []
        for comment in comments:
            response = PropertyCommentResponse.from_orm(comment)
            response.author = comment.visitor_name or "Anonymous"
            response_comments.append(response)

        return response_comments
    
    @classmethod
    async def get_property_stats(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str
    ) -> PropertyInteractionStats:
        """Get interaction statistics for a property"""
        
        # Get interaction counts
        likes_result = await db.execute(
            select(func.count(PropertyInteraction.id))
            .where(
                and_(
                    PropertyInteraction.collection_id == collection_id,
                    PropertyInteraction.property_id == property_id,
                    PropertyInteraction.liked == True
                )
            )
        )
        likes = likes_result.scalar() or 0
        
        dislikes_result = await db.execute(
            select(func.count(PropertyInteraction.id))
            .where(
                and_(
                    PropertyInteraction.collection_id == collection_id,
                    PropertyInteraction.property_id == property_id,
                    PropertyInteraction.disliked == True
                )
            )
        )
        dislikes = dislikes_result.scalar() or 0

        # Get comment count
        comments_result = await db.execute(
            select(func.count(PropertyComment.id))
            .where(
                and_(
                    PropertyComment.collection_id == collection_id,
                    PropertyComment.property_id == property_id
                )
            )
        )
        comments = comments_result.scalar() or 0
        
        return PropertyInteractionStats(
            property_id=property_id,
            likes=likes,
            dislikes=dislikes,
            comments=comments
        )
    
    @classmethod
    async def get_property_interaction_summary(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str
    ) -> PropertyInteractionSummary:
        """Get complete interaction summary for a property"""
        
        # Get comprehensive stats
        stats = await cls.get_property_stats(
            db, collection_id, property_id
        )
        
        # Get all comments
        comments = await cls.get_property_comments(
            db, collection_id, property_id
        )
        
        return PropertyInteractionSummary(
            stats=stats,
            comments=comments
        )
