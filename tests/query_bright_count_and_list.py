import asyncio
import os
import sys
import json
from pathlib import Path
from collections import Counter
from datetime import datetime
import zoneinfo

# Add the 'server' directory to Python's search path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from app.services.bright_mls_service import bright_mls_service
from app.utils.mls_mapper import map_reso_to_internal
from dotenv import load_dotenv

# Load environment variables from server/.env
load_dotenv(root_dir / ".env")

def convert_to_utc_timestamp(date_str: str) -> str:
    """
    Converts a date string like 'March 5, 3:00 PM' to a UTC ISO timestamp.
    Assumes EST/EDT (America/New_York) as the source timezone.
    """
    # Define local timezone (EST/EDT)
    local_tz = zoneinfo.ZoneInfo("America/New_York")
    
    # Current year to fill in if missing
    current_year = datetime.now().year
    
    # Try common formats
    formats = [
        "%B %d, %I:%M %p",  # March 5, 3:00 PM
        "%B %d, %I %p",     # March 5, 3 PM
        "%m/%d/%Y %I:%M %p",
        "%Y-%m-%dT%H:%M:%S"
    ]
    
    dt = None
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            # If the year wasn't in the format, use current year
            if dt.year == 1900:
                dt = dt.replace(year=current_year)
            break
        except ValueError:
            continue
            
    if not dt:
        raise ValueError(f"Could not parse date string: {date_str}")
        
    # Attach local timezone and convert to UTC
    local_dt = dt.replace(tzinfo=local_tz)
    utc_dt = local_dt.astimezone(zoneinfo.ZoneInfo("UTC"))
    
    # Return in Bright MLS expected format: 2026-03-05T20:00:00Z
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

async def query_bright_properties(target_date_str: str):
    try:
        timestamp = convert_to_utc_timestamp(target_date_str)
        print(f"🕒 Local Time: {target_date_str} (EST/EDT)")
        print(f"🌐 UTC Target: {timestamp}")
        print(f"🚀 Querying Bright MLS...\n")
    except Exception as e:
        print(f"❌ Date Error: {e}")
        return

    if not os.getenv("BRIGHT_MLS_CLIENT"):
        print("❌ ERROR: BRIGHT_MLS_CLIENT not found in .env file.")
        return

    try:
        # 1. Get the total count
        count_params = {
            "$filter": f"ModificationTimestamp gt {timestamp}",
            "$top": 0,
            "$count": "true"
        }

        count_data = await bright_mls_service._make_request("BrightProperties", params=count_params)
        total_count = count_data.get("@odata.count", 0)
        print(f"📊 Total properties modified: {total_count}")

        if total_count == 0:
            return

        # 2. Fetch minimal fields for breakdown
        print(f"📥 Processing home types for {total_count} properties...")
        
        home_type_counts = Counter()
        batch_size = 500
        skip = 0
        
        while skip < total_count:
            params = {
                "$filter": f"ModificationTimestamp gt {timestamp}",
                "$top": batch_size,
                "$skip": skip,
                "$select": "PropertyType,StructureDesignType",
                "$orderby": "ModificationTimestamp asc"
            }
            
            data = await bright_mls_service._make_request("BrightProperties", params=params)
            batch = data.get("value", [])
            
            if not batch:
                break
                
            for item in batch:
                mapped = map_reso_to_internal(item)
                home_type_counts[mapped["home_type"]] += 1
            
            skip += len(batch)
            print(f"   Processed {skip}/{total_count}...")

        print("\n🏠 Breakdown by Home Type:")
        for h_type, count in home_type_counts.most_common():
            print(f"   - {h_type}: {count}")

    except Exception as e:
        print(f"\n❌ API Error: {e}")
    finally:
        await bright_mls_service.close()
        print("\n🏁 Query complete.")

if __name__ == "__main__":
    # You can now use natural strings here
    user_input_date = "March 5, 2:00 PM"
    
    asyncio.run(query_bright_properties(user_input_date))
