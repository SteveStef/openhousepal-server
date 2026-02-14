
import httpx
import os
import asyncio
import time
from fastapi import HTTPException
from app.schemas.collection_preferences import CollectionPreferencesBase as CollectionPreferencesSchema

from dotenv import load_dotenv
load_dotenv()

class BrightMlsService:
    def __init__(self):

        self.client_id = os.getenv("BRIGHT_MLS_CLIENT")
        self.client_secret = os.getenv("BRIGHT_MLS_SECRET")
        self.is_prod = os.getenv("BRIGHT_MLS_ENV", "test").lower() == "prod"
        self.token_url = os.getenv("BRIGHT_TOKEN_URL")
        self.api_base_url = os.getenv("BRIGHT_BASE_URL")

        self._access_token = None
        self._token_expires_at = 0

    async def _get_access_token(self) -> str:
        current_time = time.time()
        if self._access_token and current_time < (self._token_expires_at - 60):
            return self._access_token

        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(self.token_url, data=payload)
                response.raise_for_status()
                data = response.json()
                
                self._access_token = data.get("access_token")
                expires_in = data.get("expires_in", 3600)
                self._token_expires_at = current_time + expires_in
                
                return self._access_token
        except Exception as e:
            raise HTTPException(status_code=500, detail="MLS Authentication Failed")

    async def get_property_by_address(self, address: str): # this is for the kit (need lots of images)
        pass

    async def get_property_by_location(self, preferences: CollectionPreferencesSchema):
        pass

    async def get_similar_properties(self, listing_key: str):
        pass

    async def get_property_by_id(self, listing_key: str):
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        url = f"{self.api_base_url}/BrightProperties"
        params = {
            "$filter": f"ListingKey eq {listing_key}",
            "$top": 1
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers, params=params)

                if response.status_code != 200:
                    raise HTTPException(status_code=response.status_code, detail="MLS Provider Error")

                data = response.json()
                return data.get("value", {})

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Error fetching property details", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    async def get_property_nearby(self, preferences: CollectionPreferencesSchema): # radius based search
        pass


if __name__ == "__main__":
    service = BrightMlsService()
    print(asyncio.run(service.get_property_by_id("804485664218")))

