import asyncio
import os
import sys
import argparse
from datetime import datetime, timezone
from typing import List, Dict, Any

# Add the server directory to the path so we can import our app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import AsyncSessionLocal
from app.models.database import Property
from app.services.bright_mls_service import BrightMlsService

async def get_off_market_keys(db: AsyncSession) -> List[str]:
    """Fetch all listing keys currently marked as OFF_MARKET-BRIGHT."""
    print("Reading OFF_MARKET-BRIGHT keys from database...")
    stmt = select(Property.listing_key).where(Property.home_status == 'OFF_MARKET-BRIGHT')
    result = await db.execute(stmt)
    keys = [row.listing_key for row in result.all()]
    print(f"Found {len(keys)} properties to audit.")
    return keys

async def audit_and_recover(dry_run: bool = True, limit: int = None):
    """
    Audit properties marked as OFF_MARKET-BRIGHT and restore their status if they are actually active.
    Uses ultra-light requests for maximum performance.
    """
    print(f"--- Starting Optimized Audit (Dry Run: {dry_run}) ---")
    
    mls_service = BrightMlsService()
    
    stats = {
        "total": 0,
        "checked": 0,
        "mismatches_found": 0,
        "restored": 0,
        "remained_off_market": 0,
        "not_found_in_mls": 0,
        "errors": 0
    }
    
    try:
        async with AsyncSessionLocal() as db:
            # 1. Get targets
            all_keys = await get_off_market_keys(db)
            if limit:
                all_keys = all_keys[:limit]
                print(f"Limit applied: only checking first {limit} properties.")
            
            stats["total"] = len(all_keys)
            if not all_keys:
                print("No properties to check.")
                return

            # 2. Process in optimized batches
            chunk_size = 200 
            
            for i in range(0, len(all_keys), chunk_size):
                chunk = all_keys[i:i + chunk_size]
                keys_str = ",".join([str(k) for k in chunk])
                
                try:
                    # Query Bright MLS for ONLY status
                    params = {
                        "$filter": f"ListingKey in ({keys_str})",
                        "$select": "ListingKey,MlsStatus",
                        "$top": chunk_size
                    }
                    data = await mls_service._make_request("BrightProperties", params=params)
                    live_statuses = {str(p["ListingKey"]): p.get("MlsStatus", "") for p in data.get("value", [])}
                    
                    stats["checked"] += len(chunk)
                    
                    to_restore = [] 
                    
                    for key in chunk:
                        if key not in live_statuses:
                            stats["not_found_in_mls"] += 1
                            continue
                            
                        live_status = live_statuses[key]
                        live_status_upper = live_status.upper()
                        
                        # Check if it should be restored
                        if any(s in live_status_upper for s in ["ACTIVE", "COMING SOON", "PENDING"]):
                            stats["mismatches_found"] += 1
                            print(f"  [MISMATCH] {key} is locally OFF_MARKET but live status is {live_status}")
                            to_restore.append({"l_key": key, "new_status": live_status_upper})
                        else:
                            stats["remained_off_market"] += 1
                    
                    # 3. Surgical Update
                    if to_restore and not dry_run:
                        try:
                            for item in to_restore:
                                stmt = (
                                    update(Property)
                                    .where(Property.listing_key == item["l_key"])
                                    .values(
                                        home_status=item["new_status"],
                                        updated_at=datetime.now(timezone.utc)
                                    )
                                )
                                await db.execute(stmt)
                                stats["restored"] += 1
                            
                            await db.commit()
                            print(f"  [FIXED] {len(to_restore)} properties restored in this batch.")
                        except Exception as db_err:
                            print(f"  [DB ERROR] Failed to update batch: {db_err}")
                            await db.rollback()
                            stats["errors"] += len(to_restore)
                            
                except Exception as e:
                    print(f"Failed to process batch starting at {i}: {e}")
                    stats["errors"] += 1
                
                # Progress update
                if (i // chunk_size + 1) % 5 == 0 or (i + len(chunk) >= stats["total"]):
                    print(f"Progress: {stats['checked']}/{stats['total']} checked. Mismatches found so far: {stats['mismatches_found']}")

            # Final Report
            print("\n" + "="*40)
            print("ULTRA-LIGHT AUDIT COMPLETE")
            print("="*40)
            print(f"Total Audited:         {stats['total']}")
            print(f"Mismatches Detected:   {stats['mismatches_found']}")
            if not dry_run:
                print(f"Successfully Restored: {stats['restored']}")
            else:
                print(f"Dry Run: No properties were actually updated.")
            print(f"Confirmed Off-Market:  {stats['remained_off_market']}")
            print(f"No longer in MLS Feed: {stats['not_found_in_mls']}")
            if stats['errors'] > 0:
                print(f"Processing Errors:     {stats['errors']}")
            print("="*40)

    except Exception as e:
        print(f"Critical error during audit: {e}")
    finally:
        await mls_service.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit and recover OFF_MARKET-BRIGHT properties.")
    parser.add_argument("--run", action="store_true", help="Actually apply fixes (default is dry-run)")
    parser.add_argument("--limit", type=int, help="Limit the number of properties to check")
    
    args = parser.parse_args()
    
    asyncio.run(audit_and_recover(dry_run=not args.run, limit=args.limit))
