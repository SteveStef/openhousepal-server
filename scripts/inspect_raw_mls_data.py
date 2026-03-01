import asyncio
import os
import sys
import json
import httpx
from dotenv import load_dotenv

# Add server directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

load_dotenv()

async def check_township_patterns():
    client_id = os.getenv("BRIGHT_MLS_CLIENT")
    client_secret = os.getenv("BRIGHT_MLS_SECRET")
    token_url = "https://brightmls.okta.com/oauth2/default/v1/token"
    api_url = "https://bright-reso.brightmls.com/RESO/OData/bright/BrightProperties"

    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1. Auth
        payload = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        auth_res = await client.post(token_url, data=payload)
        token = auth_res.json().get("access_token")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        # 2. Fetch 100 properties
        print("Fetching unique IncorporatedCityName patterns...")
        params = {
            "$filter": "MlsStatus eq 'ACTIVE-BRIGHT' and PropertyType eq 'Residential'",
            "$top": 100,
            "$select": "IncorporatedCityName"
        }
        
        response = await client.get(api_url, headers=headers, params=params)
        
        if response.status_code == 200:
            items = response.json().get("value", [])
            names = [item.get("IncorporatedCityName") for item in items if item.get("IncorporatedCityName")]
            unique_names = sorted(list(set(names)))
            
            print(f"\nUnique values found ({len(unique_names)} total):")
            for name in unique_names:
                print(f"  - '{name}'")
        else:
            print(f"❌ Error: {response.status_code}")

if __name__ == "__main__":
    asyncio.run(check_township_patterns())
