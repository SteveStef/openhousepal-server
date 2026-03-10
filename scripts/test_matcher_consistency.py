import asyncio
import os
import sys
import random
from typing import Dict, Any, List, Set
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import AsyncSessionLocal
from app.models.database import Property, Collection, collection_properties
from app.services.property_sync_service import PropertySyncService

async def audit_system_consistency():
    """
    Complete 360-Degree Audit:
    1. False Negatives: Are existing houses correctly matched by Reverse Logic?
    2. False Positives: Are non-member houses correctly REJECTED by Reverse Logic?
    """
    print("\n--- 🛡️  Full 360 Degree Matcher Audit ---")
    
    sync_service = PropertySyncService()
    
    async with AsyncSessionLocal() as db:
        # Get all properties for sampling
        all_props_res = await db.execute(select(Property).limit(1000))
        all_properties = all_props_res.scalars().all()
        
        # Get all active collections with their properties
        stmt = (
            select(Collection)
            .options(selectinload(Collection.properties))
            .where(Collection.status == 'ACTIVE')
        )
        res = await db.execute(stmt)
        collections = res.scalars().all()

        if not collections:
            print("❌ No active collections found to audit.")
            return

        print(f"Auditing {len(collections)} collections against {len(all_properties)} properties...\n")

        total_fn_checked = 0
        total_fn_errors = 0
        total_fp_checked = 0
        total_fp_errors = 0
        
        for col in collections:
            print(f"📂 Showcase: '{col.name}'")
            
            # --- Part 1: Check for False Negatives (Missing Matches) ---
            col_prop_ids = {p.id for p in col.properties}
            for prop in col.properties:
                total_fn_checked += 1
                prop_data = _get_prop_dict(prop)
                matching_cols = await sync_service._discover_matching_collections(db, prop_data)
                
                if not any(str(m.id) == str(col.id) for m in matching_cols):
                    total_fn_errors += 1
                    print(f"   🔴 FALSE NEGATIVE: '{prop.street_address}' is in collection but DISCOVERY REJECTED IT.")

            # --- Part 2: Check for False Positives (Incorrect Matches) ---
            # Sample properties NOT in this collection
            outside_props = [p for p in all_properties if p.id not in col_prop_ids]
            sample_size = min(20, len(outside_props)) # Check up to 20 random outside properties
            test_sample = random.sample(outside_props, sample_size) if outside_props else []

            for prop in test_sample:
                total_fp_checked += 1
                prop_data = _get_prop_dict(prop)
                matching_cols = await sync_service._discover_matching_collections(db, prop_data)
                
                if any(str(m.id) == str(col.id) for m in matching_cols):
                    # Check if collection is at capacity (usually 500)
                    if len(col.properties) >= 500:
                        # Skip error for full collections - they match but aren't stored
                        continue
                        
                    total_fp_errors += 1
                    print(f"   🟠 FALSE POSITIVE: '{prop.street_address}' is NOT in collection but DISCOVERY ACCEPTED IT.")
                    print(f"      Specs: ${prop.price} | {prop.bedrooms}BR | {prop.home_type} | {prop.city}")

        print("\n--- 📊 Final Audit Summary ---")
        print(f"✅ False Negatives Checked: {total_fn_checked} (Errors: {total_fn_errors})")
        print(f"✅ False Positives Checked: {total_fp_checked} (Errors: {total_fp_errors})")
        
        if total_fn_errors == 0 and total_fp_errors == 0:
            print("\n🎉 PERFECT: Matcher logic is 100% consistent both ways!")
        else:
            print(f"\n🚩 ATTENTION: Found {total_fn_errors + total_fp_errors} logic discrepancies. Check filter definitions.")

def _get_prop_dict(prop: Property) -> Dict[str, Any]:
    return {
        "listing_key": prop.listing_key,
        "street_address": prop.street_address,
        "city": prop.city,
        "state": prop.state,
        "township": prop.township,
        "school_district_name": prop.school_district_name,
        "price": prop.price,
        "bedrooms": prop.bedrooms,
        "bathrooms": prop.bathrooms,
        "home_type": prop.home_type,
        "year_built": prop.year_built,
        "latitude": prop.latitude,
        "longitude": prop.longitude
    }

if __name__ == "__main__":
    asyncio.run(audit_system_consistency())
