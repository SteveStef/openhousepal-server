from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import List, Optional
from datetime import datetime, timezone
import os
from urllib.parse import quote

from app.services.email_avatar_service import agent_email_vars
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
from app.services.blacklist_service import BlacklistService
from app.services.leads_service import upsert_lead
from app.services.website.site_service import resolve_owner_public_base_url
from app.config.logging import get_logger
from app.services.agent_email_service import agent_public_phone

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

        current_time = datetime.now(timezone.utc)
        was_liked, was_disliked = (bool(interaction.liked), bool(interaction.disliked)) if interaction else (False, False)

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

        # The client's own like / dislike goes on their lead's activity (not the agent's own clicks,
        # not taking one back).
        if user_id is None and ((interaction.liked and not was_liked) or (interaction.disliked and not was_disliked)):
            from app.services.leads_service import log_showcase_activity
            liked = bool(interaction.liked)
            await log_showcase_activity(db, collection_id, "showcase_like" if liked else "showcase_dislike",
                                        "Liked {home} in their Showcase" if liked else "Passed on {home} in their Showcase",
                                        property_id=property_id, dedupe_hours=1)
            await db.refresh(interaction)

        # Send email to agent if visitor liked the property (NOT if agent themselves liked it)
        if interaction.liked:
            collection_result = await db.execute(
                select(Collection).where(Collection.id == collection_id)
            )
            collection = collection_result.scalar_one_or_none()

            # Only send email if the interaction was performed by an anonymous visitor
            if collection and user_id is None:
                agent_result = await db.execute(
                    select(User).where(User.id == collection.owner_id)
                )
                agent = agent_result.scalar_one_or_none()

                property_result = await db.execute(
                    select(Property).where(Property.id == property_id)
                )
                property_obj = property_result.scalar_one_or_none()

                if agent and agent.email and property_obj:
                    # No email per like - it joins the agent's hourly "liked homes" roll-up
                    # (agent_roundup_service), so a client hearting 12 homes is one email, not 12.
                    from app.services.agent_roundup_service import queue_like
                    queue_like(db, agent, collection, property_obj, property_obj.img_src, property_obj.price)
                    await db.commit()

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
                            created_at=datetime.now(timezone.utc)
                        )
                        db.add(notification)
                        await db.commit()
            except Exception as e:
                logger.error("Failed to create property interaction notification", extra={"error": str(e)})
                # Don't fail the interaction if notification creation fails

        # Update the collection's activity timestamp if this is a visitor
        if user_id is None:
            try:
                from sqlalchemy import update
                await db.execute(
                    update(Collection)
                    .where(Collection.id == collection_id)
                    .values(last_visitor_activity_at=datetime.now(timezone.utc))
                )
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to update collection visitor activity timestamp: {e}")

        return PropertyInteractionResponse.from_orm(interaction)

    @classmethod
    async def track_property_view(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str,
        user_id: Optional[str] = None
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

        current_time = datetime.now(timezone.utc)

        if not interaction:
            interaction = PropertyInteraction(
                collection_id=collection_id,
                property_id=property_id,
                liked=False,
                disliked=False,
                view_count=0,
                created_at=current_time,
            )
            db.add(interaction)

        if user_id is None:
            # Visitor view - drives the visitor-facing "unviewed" NEW badge and
            # Collection.last_visitor_activity_at (the Showcases/Leads/Dashboard "Active" signal).
            interaction.view_count = (interaction.view_count or 0) + 1
            interaction.last_viewed_at = current_time
            try:
                from sqlalchemy import update
                await db.execute(
                    update(Collection)
                    .where(Collection.id == collection_id)
                    .values(last_visitor_activity_at=current_time)
                )
            except Exception as e:
                logger.error(f"Failed to update collection visitor activity timestamp: {e}")
        else:
            # Agent previewing their own showcase - clears this property's own NEW tag in the
            # agent view without inflating the visitor-facing view_count/last_viewed_at.
            interaction.agent_viewed_at = current_time

        interaction.updated_at = current_time
        await db.commit()
        await db.refresh(interaction)

        if user_id is None:
            # "Looked at 22 Birch Ln" - once per home per half-day, not once per tap.
            from app.services.leads_service import log_showcase_activity
            await log_showcase_activity(db, collection_id, "showcase_view", "Looked at {home} in their Showcase",
                                        property_id=property_id, dedupe_hours=12)
            await db.refresh(interaction)

        return PropertyInteractionResponse.from_orm(interaction)

    @classmethod
    async def mark_comments_read(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str,
        is_agent: bool
    ) -> None:
        """Record that this side (agent or visitor) has just read this property's comment
        thread - clears their own unread badge without touching the other side's."""
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

        current_time = datetime.now(timezone.utc)

        if not interaction:
            interaction = PropertyInteraction(
                collection_id=collection_id,
                property_id=property_id,
                liked=False,
                disliked=False,
                view_count=0,
                created_at=current_time,
            )
            db.add(interaction)

        if is_agent:
            interaction.agent_comments_read_at = current_time
        else:
            interaction.visitor_comments_read_at = current_time

        interaction.updated_at = current_time
        await db.commit()

    @classmethod
    async def _is_showcase_agent(cls, db: AsyncSession, collection_id: str, user_id: Optional[str]) -> bool:
        """True when the signed-in commenter owns this Showcase (the agent), not just any account."""
        if not user_id:
            return False
        from app.models.database import Collection
        owner_id = (await db.execute(select(Collection.owner_id).where(Collection.id == collection_id))).scalar_one_or_none()
        return owner_id == user_id

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

        current_time = datetime.now(timezone.utc)
        comment = PropertyComment(
            collection_id=collection_id,
            property_id=property_id,
            content=content,
            visitor_name=comment_data.visitor_name,
            visitor_email=comment_data.visitor_email,
            is_agent=await cls._is_showcase_agent(db, collection_id, user_id),
            created_at=current_time,
            updated_at=current_time
        )

        db.add(comment)
        await db.commit()
        await db.refresh(comment)

        # Send email notification to agent (NOT if agent themselves commented)
        collection_result = await db.execute(
            select(Collection).where(Collection.id == collection_id)
        )
        collection = collection_result.scalar_one_or_none()

        # Only send email and notification if the interaction was performed by an anonymous visitor
        if collection and user_id is None:
            agent_result = await db.execute(
                select(User).where(User.id == collection.owner_id)
            )
            agent = agent_result.scalar_one_or_none()

            property_result = await db.execute(
                select(Property).where(Property.id == property_id)
            )
            property_obj = property_result.scalar_one_or_none()

            # The Showcase belongs to a known person - the guest page only sends a first name, so
            # fall back to the Showcase's own contact or the comment never reaches their lead
            # (no activity, no "reply to their comment" follow-up).
            lead_email = comment_data.visitor_email or collection.visitor_email
            lead_phone = None if comment_data.visitor_email else getattr(collection, "visitor_phone", None)
            if lead_email or lead_phone or comment_data.visitor_name:
                comment_address = None
                if property_obj:
                    comment_address = f"{property_obj.street_address}, {property_obj.city}, {property_obj.state} {property_obj.zipcode or ''}".strip()
                await upsert_lead(
                    db,
                    owner_id=collection.owner_id,
                    name=collection.visitor_name or comment_data.visitor_name,
                    email=lead_email,
                    phone=lead_phone,
                    source_type="property_comment",
                    source_id=comment.id,
                    summary=f"Commented on a property{' - ' + comment_address if comment_address else ''}",
                    occurred_at=current_time,
                    property_address=comment_address,
                    collection_id=collection_id,
                    raw_snapshot={"content": content},
                )

            if agent and agent.email and property_obj:
                frontend_url = os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'http://localhost:3000'))
                # Straight to that home's comment thread in the agent's Showcase view, ready to reply.
                collection_link = f"{frontend_url.rstrip('/')}/showcases?showcase={collection.id}&property={property_obj.id}&comments=1"
                
                # Create full address string
                full_address = f"{property_obj.street_address}, {property_obj.city}, {property_obj.state} {property_obj.zipcode or ''}".strip()

                from app.services.notification_preferences_service import channel_enabled
                email_service = EmailService()
                if channel_enabled(agent, "PROPERTY_COMMENT", "email"):
                  from app.services.agent_email_service import send_simple_bg
                  send_simple_bg(
                    to_email=agent.email,
                    subject=f"New Comment on Property - {property_obj.street_address}",
                    template="property_comment",
                    template_variables={
                        "agent_name": agent.first_name,
                        "visitor_name": comment.visitor_name or collection.visitor_name or "A visitor",
                        "property_address": full_address,
                        "property_image": property_obj.img_src,
                        "message": content,
                        "collection_link": collection_link
                    }
                )

                # Create in-app notification for agent
                try:
                    # Truncate comment for notification if it's too long
                    comment_preview = content[:100] + "..." if len(content) > 100 else content

                    notification = Notification(
                        agent_id=collection.owner_id,
                        type="PROPERTY_COMMENT",
                        reference_type="COMMENT",
                        reference_id=comment.id,
                        title=f"New Comment: {comment.visitor_name or 'Anonymous'}",
                        message=f"Commented on {property_obj.street_address}: \"{comment_preview}\"",
                        collection_id=collection.id,
                        collection_name=collection.name,
                        property_id=property_obj.id,
                        property_address=property_obj.street_address,
                        visitor_name=comment.visitor_name or "Anonymous",
                        link=f"/showcases?showcase={collection.id}&property={property_obj.id}&comments=1",
                        is_read=False,
                        created_at=datetime.now(timezone.utc)
                    )
                    db.add(notification)
                    await db.commit()
                except Exception as e:
                    logger.error("Failed to create property comment notification", extra={"error": str(e)})

        # Update the collection's activity timestamp if this is a visitor
        if user_id is None:
            try:
                from sqlalchemy import update
                await db.execute(
                    update(Collection)
                    .where(Collection.id == collection_id)
                    .values(last_visitor_activity_at=datetime.now(timezone.utc))
                )
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to update collection visitor activity timestamp: {e}")

        # If the agent (owner) comments, email the visitor
        elif collection and user_id == collection.owner_id and collection.visitor_email:
            # Check blacklist
            if await BlacklistService.is_blacklisted(db, collection.visitor_email):
                logger.info(f"Skipping agent comment email to blacklisted visitor: {collection.visitor_email}")
                # Don't send email but return success as comment was saved
                response = PropertyCommentResponse.from_orm(comment)
                response.author = comment.visitor_name or "Anonymous"
                return response

            agent = collection.owner
            property_result = await db.execute(
                select(Property).where(Property.id == property_id)
            )
            property_obj = property_result.scalar_one_or_none()

            if agent and property_obj:
                # Use public share link for visitor - on the agent's own IDX domain if they have one
                base_url = await resolve_owner_public_base_url(db, agent)
                collection_link = f"{base_url}/showcase/{collection.share_token}"
                
                # Create full address string
                full_address = f"{property_obj.street_address}, {property_obj.city}, {property_obj.state} {property_obj.zipcode or ''}".strip()

                email_service = EmailService()
                from app.services.agent_email_service import send_simple_bg
                send_simple_bg(
                    to_email=collection.visitor_email,
                    subject=f"Your agent commented on a property: {property_obj.street_address}",
                    template="agent_comment_response",
                    template_variables={
                        "agent_name": f"{agent.first_name} {agent.last_name}",
                        "recipient_name": ((collection.visitor_name or "").strip().split(" ")[0].title() or "there"),
                        "message": content,
                        "property_image": property_obj.img_src,
                        "property_address": full_address,
                        # "4 bd · 2.5 ba · 3,130 sq ft", not "4.0 bd · 2.5 ba · 3130.0 sq ft"
                        "property_beds": f"{property_obj.bedrooms:g}" if property_obj.bedrooms else "",
                        "property_baths": f"{property_obj.bathrooms:g}" if property_obj.bathrooms else "",
                        "property_sqft": f"{property_obj.living_area:,.0f}" if property_obj.living_area else "",
                        "property_price": f"${property_obj.price:,.0f}" if property_obj.price else "",
                        "collection_link": collection_link,
                        "agent_email": agent.email,
                        "agent_phone": await agent_public_phone(db, agent),
                        "Unsub": f"{os.getenv('FRONTEND_URL', os.getenv('CLIENT_URL', 'https://openhousepal.com')).rstrip('/')}/unsubscribe?email={quote(collection.visitor_email or '', safe='')}",
                        **(await agent_email_vars(db, agent)),   # face, title, brokerage
                    },
                    reply_to=agent.email,
                    # from the agent (it is their reply) - and from their own mailbox if connected
                    from_name=f"{agent.first_name or ''} {agent.last_name or ''}".strip() or "OpenHousePal",
                    owner_id=agent.id,
                )
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
