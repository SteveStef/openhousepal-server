import asyncio
import os
import sys
import httpx
import logging
import re
import argparse
from typing import Dict, Any, List
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from dotenv import load_dotenv

# Add the app directory to sys.path so we can import internal modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property, SchoolDistrict, SystemSettings
from app.utils.mls_mapper import BRIGHT_PROPERTY_SELECT_FIELDS, map_reso_to_internal, get_best_photo_url

load_dotenv()

# Configure logging
LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'logs'))
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'seed_all_active.log')),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("property_seed")

# Suppress verbose httpx logs
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- Configuration ---
CLIENT_ID = os.getenv("BRIGHT_MLS_CLIENT")
CLIENT_SECRET = os.getenv("BRIGHT_MLS_SECRET")
TOKEN_URL = os.getenv("BRIGHT_TOKEN_URL")
API_BASE_URL = os.getenv("BRIGHT_BASE_URL")

PAGE_SIZE = 500  # Number of properties to fetch per request

async def get_access_token(client: httpx.AsyncClient) -> str:
    """Authenticates with Bright MLS to get an access token."""
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    response = await client.post(TOKEN_URL, data=payload)
    response.raise_for_status()
    return response.json().get("access_token")

async def fetch_media_for_keys(client: httpx.AsyncClient, headers: Dict[str, str], keys: List[str]) -> Dict[str, List[str]]:
    if not keys: return {}
    photo_map = {}
    chunk_size = 80
    
    logger.info(f"  Fetching photos for {len(keys)} properties...")
    
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i:i + chunk_size]
        media_params = {
            "$filter": f"ResourceRecordKey in ({','.join(chunk)}) and MediaCategory eq 'Photo'",
            "$select": "ResourceRecordKey,MediaURL,MediaURLHiRes,MediaURLFull",
            "$orderby": "MediaDisplayOrder asc"
        }
        try:
            res = await client.get(f"{API_BASE_URL}/BrightMedia", headers=headers, params=media_params)
            res.raise_for_status()
            
            for m in res.json().get("value", []):
                key = str(m["ResourceRecordKey"])
                if key not in photo_map: photo_map[key] = []
                # Use centralized helper to get best resolution
                photo_url = get_best_photo_url(m)
                if photo_url:
                    photo_map[key].append(photo_url)
        except Exception as e:
            logger.error(f"FATAL: Media fetch failed for chunk. Aborting batch to prevent data without photos. Error: {e}")
            raise  # Re-raise to trigger the break in the main loop
    
    return photo_map

async def seed_properties(start_skip: int = 0):
    if not CLIENT_ID or not CLIENT_SECRET:
        logger.error("Missing Bright MLS credentials.")
        return

    async with httpx.AsyncClient(timeout=60.0) as client:
        logger.info("Authenticating...")
        try:
            token = await get_access_token(client)
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            return

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        # Simple paging logic: no time-based filter.
        base_filter = "MlsStatus in ('ACTIVE-BRIGHT', 'COMING SOON-BRIGHT')"
        
        select_fields = ",".join(BRIGHT_PROPERTY_SELECT_FIELDS)

        skip = start_skip
        total_seeded = 0
        global_latest_ts = None
        last_listing_key = None
        
        while True:
            current_filter = base_filter
            if last_listing_key:
                current_filter += f" and ListingKey gt {last_listing_key}"
            
            logger.info(f"Seeding batch (Total Seeded: {total_seeded}, Last Key: {last_listing_key or 'None'})...")
            
            # Ordering by ListingKey to ensure stable pagination
            params = {
                "$filter": current_filter,
                "$top": PAGE_SIZE,
                "$select": select_fields,
                "$orderby": "ListingKey asc"
            }
            
            # Use skip only for the initial start if specified
            if not last_listing_key and skip > 0:
                params["$skip"] = skip

            try:
                response = await client.get(f"{API_BASE_URL}/BrightProperties", headers=headers, params=params)
                if response.status_code != 200:
                    logger.error(f"API Error at Total Seeded={total_seeded}: {response.status_code} - {response.text}")
                    break
                
                logger.info(f"Fetched properties batch (Status: {response.status_code} OK)")
                
                items = response.json().get("value", [])
                if not items:
                    logger.info("Done seeding all properties.")
                    break

                # Update the last_listing_key for the next batch
                last_listing_key = items[-1]["ListingKey"]

                # Track the latest ModificationTimestamp in this batch
                batch_timestamps = [item.get("ModificationTimestamp") for item in items if item.get("ModificationTimestamp")]
                if batch_timestamps:
                    batch_max = max(batch_timestamps)
                    if not global_latest_ts or batch_max > global_latest_ts:
                        global_latest_ts = batch_max

                listing_keys = [str(item["ListingKey"]) for item in items]
                
                # Fetching media is now fatal—if it fails, the exception breaks the loop
                # and the database commit below is never reached.
                photo_map = await fetch_media_for_keys(client, headers, listing_keys)

                async with AsyncSessionLocal() as db:
                    # 1. Prepare all property data
                    property_data_list = [map_reso_to_internal(item, photo_map) for item in items]
                    
                    if property_data_list:
                        # 2. Bulk Upsert Properties in chunks of 250 to avoid Postgres parameter limit (32767)
                        db_chunk_size = 250
                        for i in range(0, len(property_data_list), db_chunk_size):
                            chunk = property_data_list[i:i + db_chunk_size]
                            
                            stmt = insert(Property).values(chunk)
                            update_columns = {
                                col.name: getattr(stmt.excluded, col.name)
                                for col in Property.__table__.columns
                                if col.name not in ['id', 'listing_key']
                            }
                            
                            stmt = stmt.on_conflict_do_update(
                                index_elements=['listing_key'],
                                set_=update_columns
                            )
                            await db.execute(stmt)

                        # 3. Bulk Upsert School Districts
                        sd_data = []
                        seen_sds = set()
                        for p in property_data_list:
                            name, state = p.get("school_district_name"), p.get("state")
                            if name and state and (name, state) not in seen_sds:
                                sd_data.append({"name": name, "state": state})
                                seen_sds.add((name, state))
                        
                        if sd_data:
                            sd_stmt = insert(SchoolDistrict).values(sd_data).on_conflict_do_nothing()
                            await db.execute(sd_stmt)
                            
                    await db.commit()
                
                total_seeded += len(items)
                logger.info(f"✓ Seeded {len(items)} (Total: {total_seeded}, Last Key: {last_listing_key})")
                
                if len(items) < PAGE_SIZE:
                    break

            except Exception as e:
                logger.error(f"!!! CRITICAL FAILURE at Total Seeded={total_seeded} !!!")
                logger.error(f"Error Details: {e}")
                logger.info(f"Last successful ListingKey: {last_listing_key}")
                break

        # --- Final Step: Update System Settings Checkpoint ---
        if global_latest_ts:
            logger.info(f"Setting final sync checkpoint to: {global_latest_ts}")
            async with AsyncSessionLocal() as db:
                ss_stmt = insert(SystemSettings).values(
                    key="last_property_sync_time",
                    value={"timestamp": global_latest_ts}
                ).on_conflict_do_update(
                    index_elements=['key'],
                    set_={"value": {"timestamp": global_latest_ts}}
                )
                await db.execute(ss_stmt)
                await db.commit()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed all active properties from Bright MLS.")
    parser.add_argument("--skip", type=int, default=0, help="Number of records to skip (default: 0)")
    args = parser.parse_args()

    logger.info(f"Starting Seed from skip={args.skip} (All fields included, sorted by ListingKey)...")
    asyncio.run(seed_properties(args.skip))
