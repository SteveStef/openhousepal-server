import os
import httpx
import json
from dotenv import load_dotenv

load_dotenv()

# Configuration
CLIENT_ID = os.getenv("BRIGHT_MLS_CLIENT")
CLIENT_SECRET = os.getenv("BRIGHT_MLS_SECRET")

# Endpoints from server/docs/bright_mls.md
TOKEN_URL = "https://brightmls-test.okta.com/oauth2/default/v1/token"
API_BASE_URL = "https://bright-reso.tst.brightmls.com/RESO/OData/bright"

def get_access_token():
    """Authenticates using client credentials and returns an access token."""
    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET
    }
    
    try:
        response = httpx.post(TOKEN_URL, data=payload)
        response.raise_for_status()
        data = response.json()
        return data.get("access_token"), data.get("expires_in")
    except httpx.HTTPStatusError as e:
        print(f"Authentication failed with status {e.response.status_code}: {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"An error occurred while requesting {e.request.url!r}: {e}")
        return None

def fetch_properties(token):
    """Fetches a sample of properties from the test endpoint."""
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Bright WebAPI/1.0",
        "Cache-Control": "no-cache",
        "Accept": "application/json"
    }
    # Using the correct resource name 'BrightProperties'
    url = f"{API_BASE_URL}/BrightProperties?$top=1"
    
    try:
        response = httpx.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        print(f"API Request failed with status {e.response.status_code}: {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"An error occurred while requesting {e.request.url!r}: {e}")
        return None

if __name__ == "__main__":
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Error: BRIGHT_MLS_CLIENT_ID or BRIGHT_MLS_SECRET not found in .env file.")
    else:
        print("Starting Bright MLS Test Request (using httpx)...")
        print(f"Using Client ID: {CLIENT_ID[:5]}...")
        
        token, expires = "eyJraWQiOiI2XzdRVTRzWkhZZE04MUhZZGRQenN6Yi00eXBwckFDSDcxMlFTaUYyclZNIiwiYWxnIjoiUlMyNTYifQ.eyJ2ZXIiOjEsImp0aSI6IkFULnFrYmVCY1ZRUDJkWE9pVGJZSWJGU3pKZGVwWC0xYUdPeFduc3c3c1ZBbk0iLCJpc3MiOiJodHRwczovL29rdGEudHN0LmJyaWdodG1scy5jb20vb2F1dGgyL2RlZmF1bHQiLCJhdWQiOiJhcGk6Ly9kZWZhdWx0IiwiaWF0IjoxNzY5NTMxOTc0LCJleHAiOjE3Njk1MzU1NzQsImNpZCI6IjBvYWR6NTRidWp6MGVHb1pPNHg3Iiwic2NwIjpbImNsaWVudGNyZWQiXSwic3ViIjoiMG9hZHo1NGJ1anowZUdvWk80eDcifQ.Rb1kIasl-wmZnmP_7gcAQgN7v1PGsurz1OEpqASEgL-wfcjTpvRXkePMICwvcPIxnn_CQ-ulCjECyiJBM78dly1Nf1J9NlkzK4dUP8Au1FHmDynr1i7s6XTbKCQVAyec-ZeE_LzxELwjKTDPjbBx-c7hHTjW467CgghpS9w1IMtF4n4wXnvnqmLjoggzNZyet6pmCZ96P2h1bRzxQulcQ7SqYk8M3gM9UgkhLkeERfVazJ7jsei9nLOCyp6vOIT_d_Mkmf6vHGn4anoWSX8dEx1QHc_iJTgb11QloFxUF3adwQbLrg8sFrQjGa1-xvDSkSJMgBQ1vpIx5NkJatnJjA", 3600

        if token:
            print("Authentication successful.")
            data = fetch_properties(token)
            if data:
                print("\nSuccess! Received Data:")
                print(json.dumps(data, indent=2))
        else:
            print("Failed to obtain access token.")
