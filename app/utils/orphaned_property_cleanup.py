import os
import asyncio
from datetime import datetime
from typing import Dict, Any, List
from sqlalchemy import select, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.database import Property, PropertyInteraction, PropertyComment, collection_properties


async def find_orphaned_properties(db: AsyncSession, batch_size: int = 100) -> List[str]:
    """
    Find properties that have no relationship to any collection
    Returns list of orphaned property IDs
    """
    # Query to find properties with no collection relationships
    query = text("""
        SELECT p.id
        FROM properties p
        LEFT JOIN collection_properties cp ON p.id = cp.property_id
        WHERE cp.property_id IS NULL
        LIMIT :batch_size
    """)

    result = await db.execute(query, {"batch_size": batch_size})
    orphaned_ids = [row[0] for row in result.fetchall()]

    return orphaned_ids


async def cleanup_property_dependencies(db: AsyncSession, property_ids: List[str]) -> Dict[str, int]:
    """
    Clean up dependent data before deleting properties
    Returns count of deleted records by type
    """
    cleanup_stats = {
        'interactions_deleted': 0,
        'comments_deleted': 0
    }

    if not property_ids:
        return cleanup_stats

    # Delete PropertyInteractions
    interactions_result = await db.execute(
        delete(PropertyInteraction).where(PropertyInteraction.property_id.in_(property_ids))
    )
    cleanup_stats['interactions_deleted'] = interactions_result.rowcount

    # Delete PropertyComments
    comments_result = await db.execute(
        delete(PropertyComment).where(PropertyComment.property_id.in_(property_ids))
    )
    cleanup_stats['comments_deleted'] = comments_result.rowcount

    return cleanup_stats


async def delete_orphaned_properties(db: AsyncSession, property_ids: List[str]) -> int:
    """
    Delete the orphaned properties themselves
    Returns count of deleted properties
    """
    if not property_ids:
        return 0

    result = await db.execute(
        delete(Property).where(Property.id.in_(property_ids))
    )

    return result.rowcount


async def cleanup_orphaned_properties_batch(dry_run: bool = False, batch_size: int = 100) -> Dict[str, Any]:
    """
    Clean up orphaned properties in batches
    This is the main cleanup function
    """
    print(f"[ORPHANED_CLEANUP] Starting orphaned property cleanup at {datetime.utcnow()}")
    print(f"[ORPHANED_CLEANUP] Mode: {'DRY RUN' if dry_run else 'LIVE'}, Batch size: {batch_size}")

    cleanup_results = {
        'started_at': datetime.utcnow(),
        'dry_run': dry_run,
        'batch_size': batch_size,
        'total_orphaned_found': 0,
        'total_properties_deleted': 0,
        'total_interactions_deleted': 0,
        'total_comments_deleted': 0,
        'batches_processed': 0,
        'errors': [],
        'success': True
    }

    try:
        async with AsyncSessionLocal() as db:
            batch_count = 0

            while True:
                # Find orphaned properties in this batch
                orphaned_ids = await find_orphaned_properties(db, batch_size)

                if not orphaned_ids:
                    print(f"[ORPHANED_CLEANUP] No more orphaned properties found")
                    break

                batch_count += 1
                cleanup_results['batches_processed'] = batch_count
                cleanup_results['total_orphaned_found'] += len(orphaned_ids)

                print(f"[ORPHANED_CLEANUP] Batch {batch_count}: Found {len(orphaned_ids)} orphaned properties")

                if dry_run:
                    print(f"[ORPHANED_CLEANUP] DRY RUN - Would delete properties: {orphaned_ids[:5]}{'...' if len(orphaned_ids) > 5 else ''}")
                    continue

                try:
                    # Clean up dependent data first
                    dependency_stats = await cleanup_property_dependencies(db, orphaned_ids)
                    cleanup_results['total_interactions_deleted'] += dependency_stats['interactions_deleted']
                    cleanup_results['total_comments_deleted'] += dependency_stats['comments_deleted']

                    print(f"[ORPHANED_CLEANUP] Cleaned up {dependency_stats['interactions_deleted']} interactions, {dependency_stats['comments_deleted']} comments")

                    # Delete the orphaned properties
                    deleted_count = await delete_orphaned_properties(db, orphaned_ids)
                    cleanup_results['total_properties_deleted'] += deleted_count

                    print(f"[ORPHANED_CLEANUP] Deleted {deleted_count} orphaned properties")

                    # Commit the batch
                    await db.commit()

                    # If we got fewer results than batch_size, we're done
                    if len(orphaned_ids) < batch_size:
                        break

                except Exception as e:
                    await db.rollback()
                    error_msg = f"Error processing batch {batch_count}: {str(e)}"
                    print(f"[ORPHANED_CLEANUP] ERROR: {error_msg}")
                    cleanup_results['errors'].append(error_msg)
                    # Continue with next batch instead of failing completely
                    continue

        cleanup_results['completed_at'] = datetime.utcnow()
        cleanup_results['duration_seconds'] = (
            cleanup_results['completed_at'] - cleanup_results['started_at']
        ).total_seconds()

        print(f"[ORPHANED_CLEANUP] Completed! Processed {cleanup_results['batches_processed']} batches")
        print(f"[ORPHANED_CLEANUP] Found {cleanup_results['total_orphaned_found']} orphaned properties")

        if not dry_run:
            print(f"[ORPHANED_CLEANUP] Deleted {cleanup_results['total_properties_deleted']} properties")
            print(f"[ORPHANED_CLEANUP] Deleted {cleanup_results['total_interactions_deleted']} interactions")
            print(f"[ORPHANED_CLEANUP] Deleted {cleanup_results['total_comments_deleted']} comments")

    except Exception as e:
        error_msg = f"Critical error during orphaned property cleanup: {str(e)}"
        print(f"[ORPHANED_CLEANUP] CRITICAL ERROR: {error_msg}")
        cleanup_results['success'] = False
        cleanup_results['errors'].append(error_msg)

    return cleanup_results


async def scheduled_orphaned_property_cleanup():
    """
    Entry point function to be called by APScheduler
    Checks configuration and runs cleanup
    """
    if not os.getenv("ORPHANED_CLEANUP_ENABLED", "false").lower() == "true":
        print("[ORPHANED_CLEANUP] Orphaned property cleanup is disabled (ORPHANED_CLEANUP_ENABLED=false)")
        return

    # Get configuration
    dry_run = os.getenv("ORPHANED_CLEANUP_DRY_RUN", "false").lower() == "true"
    batch_size = int(os.getenv("ORPHANED_CLEANUP_BATCH_SIZE", "100"))

    try:
        result = await cleanup_orphaned_properties_batch(dry_run=dry_run, batch_size=batch_size)

        if result['success']:
            if dry_run:
                print(f"[ORPHANED_CLEANUP] ✅ Dry run completed - Found {result['total_orphaned_found']} orphaned properties")
            else:
                print(f"[ORPHANED_CLEANUP] ✅ Cleanup completed - Deleted {result['total_properties_deleted']} properties")
        else:
            print(f"[ORPHANED_CLEANUP] ❌ Cleanup completed with errors: {result['errors']}")

    except Exception as e:
        print(f"[ORPHANED_CLEANUP] 💥 Critical error in scheduled cleanup: {str(e)}")


async def count_orphaned_properties() -> int:
    """
    Utility function to count orphaned properties without deleting them
    Useful for monitoring and reporting
    """
    try:
        async with AsyncSessionLocal() as db:
            query = text("""
                SELECT COUNT(*)
                FROM properties p
                LEFT JOIN collection_properties cp ON p.id = cp.property_id
                WHERE cp.property_id IS NULL
            """)

            result = await db.execute(query)
            count = result.scalar()

            print(f"📊 Found {count} orphaned properties in database")
            return count

    except Exception as e:
        print(f"❌ Error counting orphaned properties: {str(e)}")
        return 0


if __name__ == "__main__":
    """For testing - run this script directly to count orphaned properties"""
    print("🔍 Checking for orphaned properties...")
    asyncio.run(count_orphaned_properties())