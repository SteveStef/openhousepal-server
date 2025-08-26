import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from app.services.property_sync_service import PropertySyncService

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class PropertySyncScheduler:
    def __init__(self, interval_hours: int = 3):
        self.interval_hours = interval_hours
        self.sync_service = PropertySyncService()
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        
    async def run_scheduled_sync(self):
        """
        Run the scheduled property sync task
        """
        logger.info(f"Starting scheduled property sync (runs every {self.interval_hours} hours)")
        
        while self.is_running:
            try:
                logger.info("Executing scheduled property sync...")
                result = await self.sync_service.sync_all_active_collections()
                
                if result['success']:
                    logger.info(
                        f"Scheduled sync completed successfully. "
                        f"Processed {result['collections_processed']} collections, "
                        f"added {result['total_new_properties']} new properties"
                    )
                else:
                    logger.error(f"Scheduled sync completed with errors: {result['errors']}")
                
            except Exception as e:
                logger.error(f"Critical error during scheduled sync: {str(e)}")
            
            # Wait for the next sync interval
            await asyncio.sleep(self.interval_hours * 3600)  # Convert hours to seconds
    
    def start(self):
        """
        Start the scheduler (only if enabled via environment variable)
        """
        # Check if property sync is enabled
        if not os.getenv("ENABLE_PROPERTY_SYNC", "false").lower() == "true":
            logger.info("Property sync is disabled (ENABLE_PROPERTY_SYNC=false)")
            return
        
        if not self.is_running:
            self.is_running = True
            self.task = asyncio.create_task(self.run_scheduled_sync())
            logger.info(f"Property sync scheduler started (interval: {self.interval_hours} hours)")
        else:
            logger.warning("Scheduler is already running")
    
    async def stop(self):
        """
        Stop the scheduler
        """
        if self.is_running:
            self.is_running = False
            if self.task:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
            logger.info("Property sync scheduler stopped")
        else:
            logger.warning("Scheduler is not running")

# Global scheduler instance
scheduler = PropertySyncScheduler()

async def start_scheduler():
    """
    Function to start the global scheduler
    Called during application startup
    """
    scheduler.start()

async def stop_scheduler():
    """
    Function to stop the global scheduler
    Called during application shutdown
    """
    await scheduler.stop()