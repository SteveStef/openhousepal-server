import asyncio
import os
import sys
import httpx
import logging
import re
import json
from typing import Dict, Any, List
from datetime import datetime
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import text
from dotenv import load_dotenv

# Add the app directory to sys.path so we can import internal modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property, HomeType, SchoolDistrict, SystemSettings
from app.utils.mls_mapper import BRIGHT_PROPERTY_SELECT_FIELDS, map_reso_to_internal, parse_dt, clean_address, get_best_photo_url

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("test_seed")

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

# --- Helper Functions ---

async def get_access_token(client: httpx.AsyncClient) -> str:
    payload = {"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    response = await client.post(TOKEN_URL, data=payload)
    response.raise_for_status()
    return response.json().get("access_token")

async def seed_diverse_local_properties():
    if not CLIENT_ID or not CLIENT_SECRET:
        logger.error("Missing Bright MLS credentials.")
        return

    # Using Postal Codes to target a specific area
    ZIPS = "'19120', '19111', '19143', '19124'"
    
    # We fetch by PropertyType and let our internal mapper handle the sub-types.
    # This avoids the "Query Too Complex" OData error.
    categories = [
        {"name": "Residential", "filter": f"PostalCode in ({ZIPS}) and PropertyType eq 'Residential'", "top": 10},
        {"name": "Multi-Family", "filter": f"PropertyType eq 'Multi-Family'", "top": 5},
        {"name": "Land", "filter": f"PropertyType eq 'Land'", "top": 5},
        {"name": "Rentals", "filter": f"PostalCode in ({ZIPS}) and PropertyType eq 'Residential Lease'", "top": 5},
    ]

    async with httpx.AsyncClient(timeout=60.0) as client:
        logger.info("--- Authenticating ---")
        token = await get_access_token(client)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        select_fields = ",".join(BRIGHT_PROPERTY_SELECT_FIELDS)

        all_raw_properties = []
        for cat in categories:
            logger.info(f"--- Fetching {cat['top']} properties for: {cat['name']} ---")
            # Flattened filter to keep it simple
            params = {
                "$filter": f"MlsStatus in ('ACTIVE-BRIGHT', 'COMING SOON-BRIGHT') and {cat['filter']}",
                "$top": cat['top'],
                "$select": select_fields
            }
            try:
                res = await client.get(f"{API_BASE_URL}/BrightProperties", headers=headers, params=params)
                if res.status_code == 200:
                    props = res.json().get("value", [])
                    logger.info(f"  Found {len(props)} properties.")
                    all_raw_properties.extend(props)
                else:
                    logger.error(f"  Error {res.status_code}: {res.text}")
            except Exception as e:
                logger.error(f"  Exception: {e}")

        if not all_raw_properties:
            logger.warning("No properties found.")
            return

        # Batch Fetch Photos
        listing_keys = [str(p["ListingKey"]) for p in all_raw_properties]
        photo_map = {}
        if listing_keys:
            # Media fetching also needs to be chunked to avoid long URLs
            chunk_size = 50
            for i in range(0, len(listing_keys), chunk_size):
                chunk = listing_keys[i:i + chunk_size]
                logger.info(f"--- Fetching photos for chunk of {len(chunk)} ---")
                media_params = {
                    "$filter": f"ResourceRecordKey in ({','.join(chunk)}) and MediaCategory eq 'Photo'",
                    "$select": "ResourceRecordKey,MediaURL,MediaURLHiRes,MediaURLFull",
                    "$orderby": "MediaDisplayOrder asc"
                }
                try:
                    media_res = await client.get(f"{API_BASE_URL}/BrightMedia", headers=headers, params=media_params)
                    if media_res.status_code == 200:
                        for m in media_res.json().get("value", []):
                            key = str(m["ResourceRecordKey"])
                            if key not in photo_map: photo_map[key] = []
                            # Use centralized helper to get best resolution
                            photo_url = get_best_photo_url(m)
                            if photo_url:
                                photo_map[key].append(photo_url)
                except Exception as e:
                    logger.warning(f"Media fetch failed: {e}")

        async with AsyncSessionLocal() as db:
            for item in all_raw_properties:
                data = map_reso_to_internal(item, photo_map)
                
                # 1. Upsert Property
                stmt = insert(Property).values(**data)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['listing_key'],
                    set_={k: v for k, v in data.items() if k != 'listing_key'}
                )
                
                # 2. Upsert School District
                sd_name = data.get("school_district_name")
                sd_state = data.get("state")
                if sd_name and sd_state:
                    sd_stmt = insert(SchoolDistrict).values(
                        name=sd_name,
                        state=sd_state
                    ).on_conflict_do_nothing()
                    await db.execute(sd_stmt)

                try:
                    await db.execute(stmt)
                    logger.info(f"✓ Upserted: {data['street_address']} ({data['listing_key']})")
                except Exception as e:
                    logger.error(f"✗ Failed {data['listing_key']}: {e}")
            await db.commit()
            
            # 3. Initialize last_property_sync_time System Setting
            # Find the latest ModificationTimestamp from the properties we just seeded
            timestamps = [p.get("ModificationTimestamp") for p in all_raw_properties if p.get("ModificationTimestamp")]
            if timestamps:
                latest_ts = max(timestamps)
                logger.info(f"Setting initial sync checkpoint to: {latest_ts}")
                
                ss_stmt = insert(SystemSettings).values(
                    key="last_property_sync_time",
                    value={"timestamp": latest_ts}
                ).on_conflict_do_update(
                    index_elements=['key'],
                    set_={"value": {"timestamp": latest_ts}}
                )
                await db.execute(ss_stmt)
                await db.commit()

            logger.info(f"--- Test Seeding Complete: Total {len(all_raw_properties)} properties ---")

if __name__ == "__main__":
    asyncio.run(seed_diverse_local_properties())
