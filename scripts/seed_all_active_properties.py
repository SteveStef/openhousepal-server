import asyncio
import os
import sys
import httpx
import logging
import re
from typing import Dict, Any, List
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from dotenv import load_dotenv

# Add the app directory to sys.path so we can import internal modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property, HomeType, SchoolDistrict
from app.utils.mls_mapper import BRIGHT_PROPERTY_SELECT_FIELDS, map_reso_to_internal, parse_dt, clean_address, get_best_photo_url

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("property_seed")

# --- Configuration ---
CLIENT_ID = os.getenv("BRIGHT_MLS_CLIENT")
CLIENT_SECRET = os.getenv("BRIGHT_MLS_SECRET")
IS_PROD = os.getenv("BRIGHT_MLS_ENV", "test").lower() == "prod"

if IS_PROD:
    TOKEN_URL = os.getenv("BRIGHT_TOKEN_URL")
    API_BASE_URL = os.getenv("BRIGHT_BASE_URL")
else:
    TOKEN_URL = "https://brightmls-test.okta.com/oauth2/default/v1/token"
    API_BASE_URL = "https://bright-reso.tst.brightmls.com/RESO/OData/bright"

PAGE_SIZE = 200  # Number of properties to fetch per request

# --- Helper Functions ---

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
    chunk_size = 50
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i:i + chunk_size]
        media_params = {
            "$filter": f"ResourceRecordKey in ({','.join(chunk)}) and MediaCategory eq 'Photo'",
            "$select": "ResourceRecordKey,MediaURL,MediaURLHiRes,MediaURLFull",
            "$orderby": "MediaDisplayOrder asc"
        }
        try:
            res = await client.get(f"{API_BASE_URL}/BrightMedia", headers=headers, params=media_params)
            if res.status_code == 200:
                for m in res.json().get("value", []):
                    key = str(m["ResourceRecordKey"])
                    if key not in photo_map: photo_map[key] = []
                    # Use centralized helper to get best resolution
                    photo_url = get_best_photo_url(m)
                    if photo_url:
                        photo_map[key].append(photo_url)
        except Exception as e:
            logger.warning(f"Media fetch failed for chunk: {e}")
    return photo_map

async def seed_properties():
    if not CLIENT_ID or not CLIENT_SECRET:
        logger.error("Missing Bright MLS credentials.")
        return

    async with httpx.AsyncClient(timeout=60.0) as client:
        logger.info("Authenticating...")
        token = await get_access_token(client)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        # Simple paging logic: no time-based filter.
        base_filter = "MlsStatus in ('ACTIVE-BRIGHT', 'COMING SOON-BRIGHT')"
        
        select_fields = ",".join(BRIGHT_PROPERTY_SELECT_FIELDS)

        skip = 0
        total_seeded = 0
        
        while True:
            logger.info(f"Seeding batch (skip={skip})...")
            # Ordering by ListingKey to ensure stable pagination
            params = {
                "$filter": base_filter,
                "$top": PAGE_SIZE,
                "$skip": skip,
                "$select": select_fields,
                "$orderby": "ListingKey asc"
            }
            
            try:
                response = await client.get(f"{API_BASE_URL}/BrightProperties", headers=headers, params=params)
                if response.status_code != 200:
                    logger.error(f"Error: {response.status_code} - {response.text}")
                    break
                
                items = response.json().get("value", [])
                if not items:
                    logger.info("Done seeding all properties.")
                    break

                listing_keys = [str(item["ListingKey"]) for item in items]
                photo_map = await fetch_media_for_keys(client, headers, listing_keys)

                async with AsyncSessionLocal() as db:
                    for item in items:
                        mapped_data = map_reso_to_internal(item, photo_map)
                        
                        # 1. Upsert Property
                        stmt = insert(Property).values(**mapped_data)
                        update_dict = {k: v for k, v in mapped_data.items() if k not in ['id', 'listing_key']}
                        stmt = stmt.on_conflict_do_update(index_elements=['listing_key'], set_=update_dict)
                        await db.execute(stmt)

                        # 2. Upsert School District Reference
                        sd_name = mapped_data.get("school_district_name")
                        sd_state = mapped_data.get("state")
                        if sd_name and sd_state:
                            sd_stmt = insert(SchoolDistrict).values(
                                name=sd_name,
                                state=sd_state
                            ).on_conflict_do_nothing()
                            await db.execute(sd_stmt)
                            
                    await db.commit()
                
                total_seeded += len(items)
                logger.info(f"✓ Seeded {len(items)} (Total: {total_seeded})")
                
                if len(items) < PAGE_SIZE:
                    break
                skip += PAGE_SIZE

            except Exception as e:
                logger.exception(f"Seed failed: {e}")
                break

if __name__ == "__main__":
    logger.info("Starting One-Time Seed (All fields included, sorted by ListingKey)...")
    asyncio.run(seed_properties())
