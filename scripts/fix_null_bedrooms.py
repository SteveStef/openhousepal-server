import asyncio
import os
import sys
import logging
from typing import List, Dict, Any

# Add the server directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy.future import select
from sqlalchemy import update, and_
from app.database import AsyncSessionLocal
from app.models.database import Property
from app.services.bright_mls_service import BrightMlsService
from app.utils.mls_mapper import map_reso_to_internal

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("fix_null_bedrooms")

async def fix_null_bedrooms():
    """
    1. Identifies properties in local DB with bedrooms=None
    2. Fetches their full data from Bright MLS
    3. Re-maps and updates the local DB
    """
    logger.info("Starting fix_null_bedrooms script...")
    
    mls_service = BrightMlsService()
    
    async with AsyncSessionLocal() as db:
        try:
            # 1. Find residential properties with null bedrooms
            # We focus on Residential, Condo, Townhouse, Multi-Family, and Lease
            residential_types = [
                'SINGLE_FAMILY', 'TOWNHOUSE', 'CONDO', 
                'MULTI_FAMILY', 'RESIDENTIAL_LEASE', 'FARM'
            ]
            
            stmt = select(Property).where(
                and_(
                    Property.bedrooms == None,
                    Property.home_type.in_(residential_types)
                )
            )
            result = await db.execute(stmt)
            properties = result.scalars().all()
            
            if not properties:
                logger.info("No properties with NULL bedrooms found. Exiting.")
                return

            listing_keys = [p.listing_key for p in properties]
            logger.info(f"Found {len(listing_keys)} properties with NULL bedrooms.")

            # 2. Batch fetch from Bright MLS (OData 'in' limit ~50-100 keys)
            chunk_size = 50
            total_updated = 0

            for i in range(0, len(listing_keys), chunk_size):
                chunk = listing_keys[i:i + chunk_size]
                formatted_keys = ",".join(chunk)  # Bright MLS expects Int64 for ListingKey, so no quotes
                
                params = {
                    "$filter": f"ListingKey in ({formatted_keys})",
                    "$select": mls_service._get_full_field_list()
                }
                
                logger.info(f"Fetching batch {i//chunk_size + 1} from Bright MLS...")
                data = await mls_service._make_request("BrightProperties", params=params)
                mls_items = data.get("value", [])

                if not mls_items:
                    logger.warning(f"No results found for keys in batch starting with {chunk[0]}")
                    continue

                # 3. Re-map and update database
                for item in mls_items:
                    l_key = str(item.get("ListingKey"))
                    mapped = map_reso_to_internal(item)
                    
                    new_bedrooms = mapped.get("bedrooms")
                    if new_bedrooms is not None:
                        # Perform targeted update
                        await db.execute(
                            update(Property)
                            .where(Property.listing_key == l_key)
                            .values(bedrooms=new_bedrooms)
                        )
                        total_updated += 1
                        #logger.info(f"Updated {l_key}: New Bedrooms = {new_bedrooms}")
                    else:
                        #logger.info(f"Property {l_key} still has NULL bedrooms after re-fetch.")

                # Commit each batch
                await db.commit()

            logger.info(f"Task complete. Total properties updated: {total_updated}")

        except Exception as e:
            logger.error(f"An error occurred: {e}", exc_info=True)
            await db.rollback()
        finally:
            await mls_service.close()

if __name__ == "__main__":
    asyncio.run(fix_null_bedrooms())
