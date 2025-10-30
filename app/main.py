from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import uvicorn
import os
from dotenv import load_dotenv
from app.api import router
from app.scheduler import start_scheduler, stop_scheduler
from app.utils.clean_cache import cleanup_expired_property_cache
from app.utils.property_sync_scheduler import scheduled_property_sync
from app.services.paypal_service import PayPalService

CLIENT_URL = os.getenv("CLIENT_URL", "http://localhost:3000")
load_dotenv()

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - handles startup and shutdown"""

    print("Using APScheduler for all scheduled tasks (cache cleanup + property sync)")

    # This is for the property details cache
    cache_hour = int(os.getenv("CACHE_CLEANUP_HOUR", 2))
    cache_mins = int(os.getenv("CACHE_CLEANUP_MINUTE", 0))

    scheduler.add_job(
        cleanup_expired_property_cache,
        CronTrigger(hour=cache_hour, minute=cache_mins),  # Daily at 2:00 AM
        id="cleanup_property_cache",
        name="Clean up expired property cache",
        replace_existing=True
    )

    # This is for the property sync (every 5 hours)
    sync_interval_hours = int(os.getenv("PROPERTY_SYNC_INTERVAL_HOURS", 5))

    scheduler.add_job(
        scheduled_property_sync,
        CronTrigger(hour=f"*/{sync_interval_hours}"),  # Every 5 hours: 0, 5, 10, 15, 20
        id="property_sync",
        name="Sync properties from Zillow API",
        replace_existing=True
    )

    scheduler.start()
    print(f"APScheduler started:")
    print(f"  - Cache cleanup: Daily at {cache_hour:02d}:{cache_mins:02d}")
    print(f"  - Property sync: Every {sync_interval_hours} hours")
    print("  - Orphaned cleanup: Inline with collection deletion")

    yield

    # Shutdown
    print("Shutting down application...")
    scheduler.shutdown()
    print("APScheduler stopped")

app = FastAPI(title="Open House Pal API", lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[CLIENT_URL],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(router)

@app.get("/health")
async def health():
    #await scheduled_property_sync()
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)

