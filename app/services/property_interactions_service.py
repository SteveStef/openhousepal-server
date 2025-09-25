from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import List
from datetime import datetime

from app.models.database import PropertyInteraction, PropertyComment
from app.schemas.property_interactions import (
    PropertyInteractionUpdate,
    PropertyCommentCreate,
    PropertyInteractionResponse,
    PropertyCommentResponse,
    PropertyInteractionStats,
    PropertyInteractionSummary
)


class PropertyInteractionsService:
    """Service for managing anonymous property interactions within collections"""
    
    @classmethod
    async def create_property_interaction(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str,
        interaction_data: PropertyInteractionUpdate
    ) -> PropertyInteractionResponse:
        """Create a new anonymous property interaction"""
        
        # Create new interaction - no user tracking, so multiple interactions allowed
        current_time = datetime.now()
        interaction = PropertyInteraction(
            collection_id=collection_id,
            property_id=property_id,
            liked=interaction_data.liked or False,
            disliked=interaction_data.disliked or False,
            favorited=interaction_data.favorited or False,
            created_at=current_time,
            updated_at=current_time
        )
        
        # Apply mutual exclusivity rules
        if interaction.liked and interaction.disliked:
            if interaction_data.liked:
                interaction.disliked = False
            else:
                interaction.liked = False
        
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
        comment_data: PropertyCommentCreate
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
        
        favorites_result = await db.execute(
            select(func.count(PropertyInteraction.id))
            .where(
                and_(
                    PropertyInteraction.collection_id == collection_id,
                    PropertyInteraction.property_id == property_id,
                    PropertyInteraction.favorited == True
                )
            )
        )
        favorites = favorites_result.scalar() or 0
        
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
            favorites=favorites,
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