import httpx
import os
import asyncio
import time
import math
import logging
import re
from typing import Dict, Any, List, Optional
from fastapi import HTTPException
from datetime import datetime, timezone
from dotenv import load_dotenv
from app.models.database import HomeType
from app.utils.mls_mapper import BRIGHT_PROPERTY_SELECT_FIELDS, map_reso_to_internal, parse_dt, clean_address, get_best_photo_url

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class BrightMlsService:
    """
    Lean Service for interacting with Bright MLS RESO Web API.
    Focused on high-speed data extraction and standardized mapping.
    """

    def __init__(self):
        # Configuration
        self.client_id = os.getenv("BRIGHT_MLS_CLIENT")
        self.client_secret = os.getenv("BRIGHT_MLS_SECRET")
        self.is_prod = os.getenv("BRIGHT_MLS_ENV", "test").lower() == "prod"
        
        if self.is_prod:
            self.token_url = os.getenv("BRIGHT_TOKEN_URL")
            self.api_base_url = os.getenv("BRIGHT_BASE_URL")
        else:
            # Test/SandBox URLs
            self.token_url = "https://brightmls-test.okta.com/oauth2/default/v1/token"
            self.api_base_url = "https://bright-reso.tst.brightmls.com/RESO/OData/bright"

        self._access_token = None
        self._token_expires_at = 0
        
        # Persistent client for connection pooling
        self.client = httpx.AsyncClient(timeout=60.0)

        if not self.client_id or not self.client_secret:
            logger.warning("BRIGHT_MLS_CLIENT or BRIGHT_MLS_SECRET not found in environment")

    async def close(self):
        """Close the underlying httpx client"""
        await self.client.aclose()

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
            response = await self.client.post(self.token_url, data=payload)
            response.raise_for_status()
            data = response.json()
            
            self._access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            self._token_expires_at = current_time + expires_in
            
            return self._access_token
        except Exception as e:
            logger.error(f"Failed to authenticate with Bright MLS: {e}")
            raise HTTPException(status_code=500, detail="MLS Authentication Failed")

    async def _make_request(self, endpoint: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        endpoint = endpoint.lstrip('/')
        url = f"{self.api_base_url}/{endpoint}"

        try:
            response = await self.client.get(url, headers=headers, params=params)

            if response.status_code != 200:
                logger.error(f"MLS API Error: {response.status_code} - {response.text}")
                if response.status_code == 404:
                    return {"value": []}
                response.raise_for_status()

            return response.json()

        except Exception as e:
            logger.error(f"Request failed to {endpoint}: {e}")
            raise HTTPException(status_code=500, detail=f"MLS Request Failed: {str(e)}")

    def _get_full_field_list(self) -> str:
        """Centralized list of all 90+ fields from mls_mapper."""
        return ",".join(BRIGHT_PROPERTY_SELECT_FIELDS)

    async def get_properties_modified_since(self, since_timestamp: str, top: int = 200, skip: int = 0) -> List[Dict[str, Any]]:
        """Fetch all properties modified since a specific timestamp to capture status changes."""
        params = {
            # REMOVED MlsStatus filter to capture transitions (e.g., ACTIVE -> PENDING)
            "$filter": f"ModificationTimestamp gt {since_timestamp}",
            "$top": top,
            "$skip": skip,
            "$select": self._get_full_field_list(),
            "$orderby": "ModificationTimestamp asc"
        }
        data = await self._make_request("BrightProperties", params=params)
        return data.get("value", [])

    async def get_recent_deletions(self, since_timestamp: str) -> List[str]:
        """
        Fetches hard-deleted ListingKeys from the Deletion route.
        Table 'CWA_BRIGHT_ALL' contains the property deletions.
        """
        params = {
            "$filter": f"DeletionTimestamp gt {since_timestamp} and TableName eq 'CWA_BRIGHT_ALL'",
            "$select": "DeleteKey",
            "$orderby": "DeletionTimestamp asc"
        }
        try:
            data = await self._make_request("Deletion", params=params)
            return [str(d["DeleteKey"]) for d in data.get("value", [])]
        except Exception as e:
            logger.error(f"Failed to fetch deletions: {e}")
            return []

    async def get_media_for_properties(self, listing_keys: List[str]) -> Dict[str, List[str]]:
        """Batch fetch photos for multiple properties."""
        if not listing_keys: return {}
        photo_map = {}
        chunk_size = 50
        for i in range(0, len(listing_keys), chunk_size):
            chunk = listing_keys[i:i + chunk_size]
            params = {
                "$filter": f"ResourceRecordKey in ({','.join(chunk)}) and MediaCategory eq 'Photo'",
                "$select": "ResourceRecordKey,MediaURL,MediaURLHiRes,MediaURLFull",
                "$orderby": "MediaDisplayOrder asc"
            }
            try:
                data = await self._make_request("BrightMedia", params=params)
                for m in data.get("value", []):
                    key = str(m["ResourceRecordKey"])
                    if key not in photo_map: photo_map[key] = []
                    # Use centralized helper to get best resolution
                    photo_url = get_best_photo_url(m)
                    if photo_url:
                        photo_map[key].append(photo_url)
            except Exception as e:
                logger.warning(f"Failed to fetch media chunk: {e}")
        return photo_map

    async def bright_mls_id_exists(self, agent_mls_id: str) -> bool:
        """Validates if a Bright Member (Agent) exists via the Members endpoint."""

        params = {
                "$filter": f"MemberMlsId eq '{agent_mls_id}'",
                "$top": 1,
                "$select": "MemberKey"
                }

        try:
            data = await self._make_request("BrightMembers", params=params)
            return len(data.get("value", [])) > 0

        except Exception as e:
            logger.error(f"Error validating Bright Agent ID: {e}")
            return False

bright_mls_service = BrightMlsService()

