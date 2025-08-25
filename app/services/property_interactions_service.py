from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from sqlalchemy.orm import selectinload
from typing import Optional, List
from datetime import datetime

from app.models.database import PropertyInteraction, PropertyComment, User
from app.schemas.property_interactions import (
    PropertyInteractionUpdate,
    PropertyCommentCreate,
    PropertyInteractionResponse,
    PropertyCommentResponse,
    PropertyInteractionStats,
    PropertyInteractionSummary
)


class PropertyInteractionsService:
    """Service for managing property interactions within collections"""
    
    @classmethod
    async def update_property_interaction(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str,
        user_id: Optional[str] = None,
        visitor_email: Optional[str] = None,
        interaction_data: PropertyInteractionUpdate = None
    ) -> PropertyInteractionResponse:
        """Update or create a property interaction"""
        
        # Build the filter condition based on user type
        if user_id:
            filter_condition = and_(
                PropertyInteraction.collection_id == collection_id,
                PropertyInteraction.property_id == property_id,
                PropertyInteraction.user_id == user_id
            )
        else:
            filter_condition = and_(
                PropertyInteraction.collection_id == collection_id,
                PropertyInteraction.property_id == property_id,
                PropertyInteraction.visitor_email == visitor_email
            )
        
        # Find existing interaction
        result = await db.execute(
            select(PropertyInteraction).where(filter_condition)
        )
        interaction = result.scalar_one_or_none()
        
        if interaction:
            # Update existing interaction
            if interaction_data.liked is not None:
                interaction.liked = interaction_data.liked
                # Mutual exclusivity: if liked is True, disliked should be False
                if interaction_data.liked:
                    interaction.disliked = False
            
            if interaction_data.disliked is not None:
                interaction.disliked = interaction_data.disliked
                # Mutual exclusivity: if disliked is True, liked should be False
                if interaction_data.disliked:
                    interaction.liked = False
                    
            if interaction_data.favorited is not None:
                interaction.favorited = interaction_data.favorited
                
            interaction.updated_at = datetime.utcnow()
        else:
            # Create new interaction
            interaction = PropertyInteraction(
                collection_id=collection_id,
                property_id=property_id,
                user_id=user_id,
                visitor_email=visitor_email,
                liked=interaction_data.liked or False,
                disliked=interaction_data.disliked or False,
                favorited=interaction_data.favorited or False
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
        comment_data: PropertyCommentCreate,
        user_id: Optional[str] = None,
        visitor_email: Optional[str] = None,
        visitor_name: Optional[str] = None
    ) -> PropertyCommentResponse:
        """Add a comment to a property"""
        
        comment = PropertyComment(
            collection_id=collection_id,
            property_id=property_id,
            user_id=user_id,
            visitor_email=visitor_email,
            visitor_name=visitor_name,
            content=comment_data.content
        )
        
        db.add(comment)
        await db.commit()
        await db.refresh(comment)
        
        # Load user for author name if available
        if comment.user_id:
            result = await db.execute(
                select(User).where(User.id == comment.user_id)
            )
            user = result.scalar_one_or_none()
            author = f"{user.first_name} {user.last_name}" if user and user.first_name else user.email if user else "Unknown User"
        else:
            author = comment.visitor_name or comment.visitor_email or "Anonymous"
        
        # Convert to response with computed author field
        comment_dict = comment.__dict__.copy()
        comment_dict['author'] = author
        
        return PropertyCommentResponse(**comment_dict)
    
    @classmethod
    async def get_property_interactions(
        cls,
        db: AsyncSession,
        collection_id: str,
        property_id: str,
        user_id: Optional[str] = None,
        visitor_email: Optional[str] = None
    ) -> Optional[PropertyInteractionResponse]:
        """Get user's interactions with a specific property"""
        
        if user_id:
            filter_condition = and_(
                PropertyInteraction.collection_id == collection_id,
                PropertyInteraction.property_id == property_id,
                PropertyInteraction.user_id == user_id
            )
        else:
            filter_condition = and_(
                PropertyInteraction.collection_id == collection_id,
                PropertyInteraction.property_id == property_id,
                PropertyInteraction.visitor_email == visitor_email
            )
        
        result = await db.execute(
            select(PropertyInteraction).where(filter_condition)
        )
        interaction = result.scalar_one_or_none()
        
        if interaction:
            return PropertyInteractionResponse.from_orm(interaction)
        return None
    
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
            .options(selectinload(PropertyComment.user))
            .where(
                and_(
                    PropertyComment.collection_id == collection_id,
                    PropertyComment.property_id == property_id
                )
            )
            .order_by(PropertyComment.created_at.desc())
        )
        comments = result.scalars().all()
        
        response_comments = []
        for comment in comments:
            # Determine author name
            if comment.user:
                author = f"{comment.user.first_name} {comment.user.last_name}" if comment.user.first_name else comment.user.email
            else:
                author = comment.visitor_name or comment.visitor_email or "Anonymous"
            
            # Convert to response with computed author field
            comment_dict = comment.__dict__.copy()
            comment_dict['author'] = author
            response_comments.append(PropertyCommentResponse(**comment_dict))
        
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
        property_id: str,
        user_id: Optional[str] = None,
        visitor_email: Optional[str] = None
    ) -> PropertyInteractionSummary:
        """Get complete interaction summary for a property"""
        
        # Get user's interaction, stats, and comments concurrently
        interaction = await cls.get_property_interactions(
            db, collection_id, property_id, user_id, visitor_email
        )
        
        stats = await cls.get_property_stats(
            db, collection_id, property_id
        )
        
        comments = await cls.get_property_comments(
            db, collection_id, property_id
        )
        
        return PropertyInteractionSummary(
            interaction=interaction,
            stats=stats,
            comments=comments
        )