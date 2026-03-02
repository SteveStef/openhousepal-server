import asyncio
import os
import sys
import random
import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy import select, func, delete
from sqlalchemy.dialects.postgresql import insert

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import (
    Property, Collection, CollectionPreferences, User, SchoolDistrict,
    collection_properties, PropertyInteraction, PropertyComment, PropertyTour
)
from app.services.collections_service import CollectionsService
from app.services.property_service import property_service

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("stress_test")

# Constants
TEST_EMAIL = "admin@openhousepal.com"
COLLECTION_COUNT = 100

async def get_or_create_test_agent(db):
    """Ensure we have a test agent with a PREMIUM plan"""
    result = await db.execute(select(User).where(User.email == TEST_EMAIL))
    user = result.scalar_one_or_none()
    
    if not user:
        logger.error(f"Required user {TEST_EMAIL} not found. Please ensure the admin user exists.")
        sys.exit(1)
    
    # Ensure user is premium for testing
    user.plan_tier = "PREMIUM"
    user.subscription_status = "ACTIVE"
    user.broker_authorized = True
    await db.commit()
        
    return user

async def analyze_property_data(db):
    """Fetch all properties and school districts to use as templates for preferences"""
    prop_res = await db.execute(select(Property).limit(500))
    sd_res = await db.execute(select(SchoolDistrict))
    
    return {
        "properties": prop_res.scalars().all(),
        "school_districts": sd_res.scalars().all()
    }

def generate_random_preferences(data):
    """Base preferences on a real property to guarantee at least one match"""
    all_props = data["properties"]
    all_sds = data["school_districts"]
    target = random.choice(all_props)
    
    # Price with 20% buffer
    min_price = int(target.price * 0.8) if target.price else 0
    max_price = int(target.price * 1.2) if target.price else 0
    
    # Beds/Baths (match exactly or less)
    min_beds = max(0, int(target.bedrooms or 0) - 1)
    min_baths = max(0, float(target.bathrooms or 0) - 1)
    
    # Location logic - Pick ONE category to mimic UI behavior
    cities = []
    townships = []
    school_districts = []
    
    loc_type = random.choice(["CITY", "TOWNSHIP", "SCHOOL"])
    
    if loc_type == "CITY" and target.city:
        cities = [f"{target.city.title()}, {target.state}"]
    elif loc_type == "TOWNSHIP" and target.township:
        # Standardize format to "Name Township, PA"
        townships = [f"{target.township.title()} Township, {target.state}"]
    elif loc_type == "SCHOOL" and target.school_district_name:
        # Look for a matching school district in our reference table
        match = next((sd for sd in all_sds if sd.name.upper() == target.school_district_name.upper()), None)
        if match:
            school_districts = [f"{match.name}, {match.state}"]
        else:
            # Fallback to just the raw name if not in table
            school_districts = [f"{target.school_district_name}, {target.state}"]
    else:
        # Final fallback
        cities = [f"{target.city.title()}, {target.state}"] if target.city else []

    # Home types (Always include the real one, plus potentially others)
    types = [target.home_type] if target.home_type else ["SINGLE_FAMILY"]
    
    # Randomly add more types to broaden the search (like a real user might)
    possible_extras = ["SINGLE_FAMILY", "CONDO", "TOWNHOUSE", "MULTI_FAMILY", "LOT_LAND", "RESIDENTIAL_LEASE", "COMMERCIAL", "FARM"]
    types.extend(random.sample(possible_extras, random.randint(0, 2)))
    types = list(set(types)) # De-duplicate
    
    return {
        "min_beds": min_beds,
        "min_baths": min_baths,
        "min_price": min_price,
        "max_price": max_price,
        "cities": cities,
        "townships": townships,
        "school_districts": school_districts,
        "is_single_family": "SINGLE_FAMILY" in types,
        "is_condo": "CONDO" in types,
        "is_town_house": "TOWNHOUSE" in types or "TOWN_HOUSE" in types,
        "is_multi_family": "MULTI_FAMILY" in types,
        "is_lot_land": "LOT_LAND" in types or "LAND" in types,
        "is_apartment": "RESIDENTIAL_LEASE" in types or "APARTMENT" in types,
        "is_commercial": "COMMERCIAL" in types,
        "is_farm": "FARM" in types
    }

async def run_stress_test(count=COLLECTION_COUNT, cleanup=False):
    async with AsyncSessionLocal() as db:
        agent = await get_or_create_test_agent(db)
        
        if cleanup:
            logger.info(f"Cleaning up collections for {TEST_EMAIL}...")
            # Get collection IDs
            col_ids_res = await db.execute(select(Collection.id).where(Collection.owner_id == agent.id))
            col_ids = [r[0] for r in col_ids_res.fetchall()]
            
            if col_ids:
                # Delete links
                await db.execute(delete(collection_properties).where(collection_properties.c.collection_id.in_(col_ids)))
                # Delete interactions
                await db.execute(delete(PropertyInteraction).where(PropertyInteraction.collection_id.in_(col_ids)))
                # Delete comments
                await db.execute(delete(PropertyComment).where(PropertyComment.collection_id.in_(col_ids)))
                # Delete tours
                await db.execute(delete(PropertyTour).where(PropertyTour.collection_id.in_(col_ids)))
                # Delete preferences
                await db.execute(delete(CollectionPreferences).where(CollectionPreferences.collection_id.in_(col_ids)))
                # Delete collections
                await db.execute(delete(Collection).where(Collection.id.in_(col_ids)))
                await db.commit()
                logger.info(f"Deleted {len(col_ids)} collections.")
            
            if cleanup and count == 0:
                return

        logger.info("Analyzing property data for realistic matching...")
        analysis_data = await analyze_property_data(db)
        
        logger.info(f"Starting creation of {count} collections...")
        start_time = datetime.now()
        
        total_links = 0
        
        for i in range(count):
            prefs_raw = generate_random_preferences(analysis_data)
            
            # Create Collection
            col_id = str(uuid.uuid4())
            visitor_name = f"Test Visitor {i+1}"
            collection = Collection(
                id=col_id,
                owner_id=agent.id,
                name=f"Stress Test: {visitor_name}",
                visitor_name=visitor_name,
                visitor_email=f"visitor{i+1}@example.com",
                status="ACTIVE",
                is_public=True,
                share_token=CollectionsService.generate_share_token(),
                created_at=datetime.now(timezone.utc)
            )
            db.add(collection)
            
            # Create Preferences
            prefs = CollectionPreferences(
                collection_id=col_id,
                **prefs_raw
            )
            db.add(prefs)
            await db.commit()
            
            # Match Properties
            match_res = await CollectionsService.repopulate_collection_from_preferences(db, col_id, commit=True)
            
            if match_res['success']:
                total_links += match_res['new_links_created']
                if (i + 1) % 5 == 0:
                    logger.info(f"Processed {i+1}/{count} collections. (Last Match: {match_res['properties_found']} found, {match_res['new_links_created']} linked)")
                    logger.info(f"   Sample Prefs: Loc={prefs_raw['cities'] or prefs_raw['townships'] or prefs_raw['school_districts']} | ${prefs_raw['min_price']}-${prefs_raw['max_price']} | {prefs_raw['min_beds']}+ beds")
            else:
                logger.error(f"Failed to match collection {i+1}: {match_res.get('error')}")

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        logger.info("\n" + "="*40)
        logger.info("STRESS TEST COMPLETE")
        logger.info("="*40)
        logger.info(f"Total Collections Created: {count}")
        logger.info(f"Total Matches Found:      {total_links}")
        logger.info(f"Avg Matches per Coll:     {total_links / count:.1f}")
        logger.info(f"Total Duration:           {duration:.2f} seconds")
        logger.info(f"Avg Time per Collection:  {duration / count:.3f} seconds")
        logger.info("="*40)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.get_all_active_properties = True # Dummy for identification
    parser.add_argument("--count", type=int, default=COLLECTION_COUNT)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    
    asyncio.run(run_stress_test(count=args.count, cleanup=args.cleanup))
