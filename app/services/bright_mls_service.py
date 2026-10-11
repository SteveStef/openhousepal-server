import httpx
import os
import asyncio
import time
import math
import logging
import re
import xml.etree.ElementTree as ET
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

# Fields every BrightOpenHouses request asks for (OpenHouseKey is added on top when Bright accepts it).
OPEN_HOUSE_SELECT = "OpenHouseListingKey,OpenHouseDate,OpenHouseStartTime,OpenHouseEndTime,OpenHouseModificationTimestamp,MlsStatus"


class BrightMlsService:
    """
    Lean Service for interacting with Bright MLS RESO Web API.
    Focused on high-speed data extraction and standardized mapping.
    """

    def __init__(self):
        # Flipped off the first time Bright rejects OpenHouseKey in a $select (see _get_open_houses).
        self._open_house_key_supported = True
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

        # Bounded retry-with-backoff for transient throttling (429) and Bright-side blips (5xx).
        # The sync itself is already page-capped; this just keeps one busy moment on Bright's end
        # from silently dropping a batch of updates until the next cron run. Not a general retry
        # loop - a 4xx that isn't 429 still fails fast.
        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                response = await self.client.get(url, headers=headers, params=params)

                if response.status_code == 404:
                    return {"value": []}

                if response.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts - 1:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        delay = 0.0
                    delay = max(delay, 2 ** attempt)  # 1s, 2s, 4s floors
                    logger.warning(
                        "Bright %s -> %s, retrying in %.1fs (attempt %d/%d)",
                        endpoint, response.status_code, delay, attempt + 1, max_attempts,
                    )
                    await asyncio.sleep(delay)
                    continue

                if response.status_code != 200:
                    logger.error(f"MLS API Error: {response.status_code} - {response.text}")
                    response.raise_for_status()

                return response.json()

            except httpx.TransportError as e:
                if attempt < max_attempts - 1:
                    delay = 2 ** attempt
                    logger.warning("Bright %s transport error (%s), retrying in %.1fs", endpoint, e, delay)
                    await asyncio.sleep(delay)
                    continue
                logger.error(f"Request failed to {endpoint}: {e}")
                raise HTTPException(status_code=500, detail=f"MLS Request Failed: {str(e)}")
            except Exception as e:
                logger.error(f"Request failed to {endpoint}: {e}")
                raise HTTPException(status_code=500, detail=f"MLS Request Failed: {str(e)}")

    async def get_metadata_entity_sets(self) -> List[str]:
        """Read the Bright RESO metadata so we can confirm optional resources before syncing them."""
        token = await self._get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/xml",
        }
        response = await self.client.get(f"{self.api_base_url}/$metadata", headers=headers)
        response.raise_for_status()

        root = ET.fromstring(response.text)
        entity_sets: List[str] = []
        for elem in root.iter():
            if elem.tag.endswith("EntitySet"):
                name = elem.attrib.get("Name")
                if name:
                    entity_sets.append(name)
        if not entity_sets:
            entity_sets = re.findall(r"<EntitySet\s+Name=\"([^\"]+)\"", response.text)
        return sorted(set(entity_sets))

    async def discover_price_history_resources(self) -> List[str]:
        """Return Bright resources whose names look related to price/listing history."""
        candidates = []
        for name in await self.get_metadata_entity_sets():
            lowered = name.lower()
            if ("history" in lowered or "event" in lowered or "change" in lowered) and (
                "price" in lowered or "listing" in lowered or "property" in lowered
            ):
                candidates.append(name)
        return candidates

    async def get_active_properties_for_price_history_probe(self, top: int = 25) -> List[Dict[str, Any]]:
        """Fetch active listings that have enough identifiers to probe Bright price history."""
        params = {
            "$filter": "MlsStatus eq 'ACTIVE-BRIGHT'",
            "$top": top,
            "$select": "ListingKey,ListingId,FullStreetAddress,UnparsedAddress,City,StateOrProvince,ListPrice,OriginalListPrice,PreviousListPrice,PriceChangeTimestamp,MajorChangeTimestamp,MajorChangeType,StatusChangeTimestamp",
            "$orderby": "ModificationTimestamp desc",
        }
        data = await self._make_request("BrightProperties", params=params)
        return data.get("value", [])

    async def probe_price_history_for_listing(self, listing_key: str, listing_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Probe Bright for the exact price-history resource without wiring unconfirmed data into sync.
        Set BRIGHT_PRICE_HISTORY_ENDPOINT once Bright confirms the endpoint name; otherwise this tries
        metadata-discovered history-like resources and reports the first successful payload.
        """
        configured = os.getenv("BRIGHT_PRICE_HISTORY_ENDPOINT")
        resources = [configured] if configured else await self.discover_price_history_resources()
        filters = [
            f"ListingKey eq '{listing_key}'",
            f"ResourceRecordKey eq '{listing_key}'",
        ]
        if listing_id:
            filters.append(f"ListingId eq '{listing_id}'")

        attempts = []
        for resource in [r for r in resources if r]:
            for filter_expr in filters:
                params = {
                    "$filter": filter_expr,
                    "$top": 25,
                }
                try:
                    data = await self._make_request(resource, params=params)
                    rows = data.get("value", [])
                    attempts.append({"resource": resource, "filter": filter_expr, "count": len(rows)})
                    if rows:
                        return {"resource": resource, "filter": filter_expr, "rows": rows, "attempts": attempts}
                except Exception as exc:
                    attempts.append({"resource": resource, "filter": filter_expr, "error": str(exc)})

        return {"resource": None, "filter": None, "rows": [], "attempts": attempts}

    async def get_price_change_fields_for_listing(self, listing_key: str) -> Dict[str, Any]:
        """
        Bright's current metadata does not expose a separate full price-history resource.
        This is the confirmed BrightProperties request for all available price-change fields.
        """
        params = {
            "$filter": f"ListingKey eq {listing_key}",
            "$top": 1,
            "$select": "ListingKey,ListingId,ListPrice,OriginalListPrice,PreviousListPrice,PriceChangeTimestamp,MajorChangeTimestamp,MajorChangeType,StatusChangeTimestamp",
        }
        data = await self._make_request("BrightProperties", params=params)
        rows = data.get("value", [])
        return rows[0] if rows else {}

    async def get_property_by_listing_key(self, listing_key: str) -> Optional[Dict[str, Any]]:
        """Live, single-record status re-pull for one listing (e.g. a CMA subject property just
        before report generation). Read-only $top=1 OData read - not part of the bulk sync, no
        Discord webhook, no side effects. Returns None on a bad key or empty result rather than
        raising, so callers can treat it as best-effort and fall back to stored data."""
        params = {
            "$filter": f"ListingKey eq {listing_key}",
            "$top": 1,
            "$select": "ListingKey,MlsStatus,StandardStatus,PurchaseContractDate,ContractStatusChangeDate,StatusChangeTimestamp,CloseDate,DaysOnMarket",
        }
        data = await self._make_request("BrightProperties", params=params)
        rows = data.get("value", [])
        return rows[0] if rows else None

    async def get_listings_for_tax_id(self, tax_id: str) -> List[Dict[str, Any]]:
        """Every listing Bright has for one parcel (any status, back to 2012) - the price history
        backfill. One parcel per query: Bright rejects OR-ing several parcels ("Query Too Complex")."""
        safe = (tax_id or "").replace("'", "''")
        data = await self._make_request("BrightProperties", params={
            "$filter": f"ListingTaxID eq '{safe}'",
            "$top": 50,
            "$select": self._get_full_field_list(),
        })
        return data.get("value", [])

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

    async def get_closed_properties_modified_since(self, since_timestamp: str, top: int = 200, skip: int = 0) -> List[Dict[str, Any]]:
        """Incremental pull of CLOSED-BRIGHT listings whose record changed since the checkpoint.

        Catches both (a) the active/pending -> closed transition (a listing's
        ModificationTimestamp bumps when the sale settles) and (b) later corrections to an
        already-closed record (a ClosePrice / CloseDate fix).

        Deliberately separate from get_properties_modified_since: in practice Bright only returns
        ClosePrice / CloseDate populated when the query itself scopes to closed status, so the
        general modified-since feed carries every closed listing with a null sale price. The CMA
        engine needs the verified closed price, so it gets its own leg."""
        params = {
            "$filter": f"MlsStatus eq 'CLOSED-BRIGHT' and ModificationTimestamp gt {since_timestamp}",
            "$top": top,
            "$skip": skip,
            "$select": self._get_full_field_list(),
            "$orderby": "ModificationTimestamp asc",
        }
        data = await self._make_request("BrightProperties", params=params)
        return data.get("value", [])

    async def get_open_houses_modified_since(self, since_timestamp: str, top: int = 200, skip: int = 0) -> List[Dict[str, Any]]:
        """Open house date/time isn't part of the Property/listing record in Bright's RESO model -
        it lives in its own BrightOpenHouses resource, keyed off OpenHouseListingKey, with its own
        OpenHouseModificationTimestamp. Needs its own incremental sync for the same reason
        get_properties_modified_since exists for BrightProperties."""
        params = {
            "$filter": f"OpenHouseModificationTimestamp gt {since_timestamp}",
            "$top": top,
            "$skip": skip,
            "$orderby": "OpenHouseModificationTimestamp asc"
        }
        return await self._get_open_houses(params)

    async def get_upcoming_open_houses(self, top: int = 200, skip: int = 0) -> List[Dict[str, Any]]:
        """Full (non-incremental) pull of every currently-scheduled future open house, regardless
        of when it was last modified. Used for the one-time backfill - the incremental
        get_open_houses_modified_since loop only catches opens that get touched *after* sync
        starts, so anything already scheduled before that point would otherwise never show up."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        params = {
            "$filter": f"OpenHouseDate ge {today}",
            "$top": top,
            "$skip": skip,
            "$orderby": "OpenHouseDate asc"
        }
        return await self._get_open_houses(params)

    async def _get_open_houses(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """BrightOpenHouses request with OpenHouseKey (the RESO id of one open house, so the sync
        can tell a Saturday and a Sunday slot apart and follow a reschedule) in $select. That
        field isn't confirmed on Bright's feed - if Bright rejects it, retry once with the old
        field list (the sync then keys slots by date+time) and stop asking for it this process."""
        if self._open_house_key_supported:
            try:
                data = await self._make_request("BrightOpenHouses", params={**params, "$select": f"OpenHouseKey,{OPEN_HOUSE_SELECT}"})
                return data.get("value", [])
            except HTTPException as e:
                logger.warning(f"BrightOpenHouses rejected OpenHouseKey in $select - falling back to the old field list ({e.detail})")
                if "400" in str(e.detail):
                    self._open_house_key_supported = False
        data = await self._make_request("BrightOpenHouses", params={**params, "$select": OPEN_HOUSE_SELECT})
        return data.get("value", [])

    async def get_recent_deletions(self, since_timestamp: str) -> List[Dict[str, str]]:
        """
        Fetches hard-deleted ListingKeys from the Deletion route.
        Table 'CWA_BRIGHT_ALL' contains the property deletions.
        """
        params = {
            "$filter": f"DeletionTimestamp gt {since_timestamp} and TableName eq 'CWA_BRIGHT_ALL'",
            "$select": "DeleteKey,DeletionTimestamp",
            "$orderby": "DeletionTimestamp asc"
        }
        try:
            data = await self._make_request("Deletion", params=params)
            return [{"key": str(d["DeleteKey"]), "timestamp": d["DeletionTimestamp"]} for d in data.get("value", [])]
        except Exception as e:
            logger.error(f"Failed to fetch deletions: {e}")
            return []

    async def get_media_for_properties(self, listing_keys: List[str]) -> Dict[str, List[str]]:
        """Batch fetch photos for multiple properties.

        Bright's BrightMedia resource returns a single default-size page (~25 rows) when no
        `$top` is given - and this is an `in (...)` query spanning up to 50 listings at once, so
        that one page was being shared across all of them (a handful of photos per listing, then
        nothing). Now it pages explicitly with `$top`/`$skip` until a short page comes back or a
        per-chunk safety cap is hit, so every listing gets its full set. Photos are kept in
        MediaDisplayOrder and de-duplicated per listing.
        """
        if not listing_keys: return {}
        photo_map: Dict[str, List[str]] = {}
        seen: Dict[str, set] = {}
        chunk_size = 50
        page_size = 200
        max_pages_per_chunk = 15  # 50 listings x up to ~60 photos = 3000 rows -> 15 pages
        for i in range(0, len(listing_keys), chunk_size):
            chunk = listing_keys[i:i + chunk_size]
            base_params = {
                "$filter": f"ResourceRecordKey in ({','.join(chunk)}) and MediaCategory eq 'Photo'",
                # MediaURLHD (2048x1536) was missing here, so get_best_photo_url never saw it and every
                # photo fell back to the 1440x1080 HiRes copy.
                "$select": "ResourceRecordKey,MediaURL,MediaURLHD,MediaURLHiRes,MediaURLFull,MediaModificationTimestamp",
                "$orderby": "ResourceRecordKey asc,MediaDisplayOrder asc",
                "$top": page_size,
            }
            for page in range(max_pages_per_chunk):
                params = {**base_params, "$skip": page * page_size}
                try:
                    data = await self._make_request("BrightMedia", params=params)
                except Exception as e:
                    logger.warning(f"Failed to fetch media chunk (page {page}): {e}")
                    break
                rows = data.get("value", [])
                for m in rows:
                    key = str(m["ResourceRecordKey"])
                    photo_url = get_best_photo_url(m)
                    if not photo_url:
                        continue
                    bucket = photo_map.setdefault(key, [])
                    seen_for_key = seen.setdefault(key, set())
                    if photo_url in seen_for_key:
                        continue
                    seen_for_key.add(photo_url)
                    bucket.append(photo_url)
                if len(rows) < page_size:
                    break
        return photo_map

    @staticmethod
    def _sold_filter(city: str, state: str, date_from: str, date_to: Optional[str] = None) -> str:
        parts = [
            f"City eq '{city}'", f"StateOrProvince eq '{state}'",
            "MlsStatus eq 'CLOSED-BRIGHT'", f"CloseDate ge {date_from}",
        ]
        if date_to:
            parts.append(f"CloseDate le {date_to}")
        return " and ".join(parts)

    async def count_sold_properties(self, city: str, state: str, date_from: str, date_to: Optional[str] = None) -> int:
        """Cheap, no-property-data probe for how many CLOSED-BRIGHT listings match a city/state/
        date range - see backend/scripts/pull_sold_comps.py. Uses $count=true + $top=0 (not the
        /$count sub-path, which returns a bare integer rather than JSON and would break
        _make_request's response.json() call), so this transfers no property rows or photos,
        just a count."""
        params = {"$filter": self._sold_filter(city, state, date_from, date_to), "$top": 0, "$count": "true"}
        data = await self._make_request("BrightProperties", params=params)
        return data.get("@odata.count", len(data.get("value", [])))

    async def get_sold_properties(self, city: str, state: str, date_from: str, date_to: Optional[str] = None, top: int = 200, skip: int = 0) -> List[Dict[str, Any]]:
        """Paginated full-data pull of CLOSED-BRIGHT listings for a city/state/date range - unlike
        get_properties_modified_since, this deliberately queries by MlsStatus + CloseDate rather
        than ModificationTimestamp, since a historical backfill needs listings regardless of when
        they were last touched. See backend/scripts/pull_sold_comps.py."""
        params = {
            "$filter": self._sold_filter(city, state, date_from, date_to),
            "$top": top, "$skip": skip,
            "$select": self._get_full_field_list(),
            "$orderby": "CloseDate desc",
        }
        data = await self._make_request("BrightProperties", params=params)
        return data.get("value", [])

    @staticmethod
    def _closed_window_filter(date_from: str, date_to: str) -> str:
        # sales only - leases (about a third of "closed" records) aren't comps for a CMA
        return (f"MlsStatus eq 'CLOSED-BRIGHT' and CloseDate ge {date_from} and CloseDate lt {date_to} "
                "and PropertyType ne 'Residential Lease' and PropertyType ne 'CommercialLease'")

    async def count_closed_window(self, date_from: str, date_to: str) -> int:
        """How many sales closed in [date_from, date_to) across all of Bright - no rows transferred."""
        data = await self._make_request("BrightProperties", params={
            "$filter": self._closed_window_filter(date_from, date_to), "$top": 0, "$count": "true"})
        return data.get("@odata.count", 0)

    async def get_closed_window(self, date_from: str, date_to: str, top: int = 200, skip: int = 0) -> List[Dict[str, Any]]:
        """One page of every sale that closed in [date_from, date_to) across Bright, full fields
        (Bright fills ClosePrice/CloseDate only on closed-scoped queries) - the 3-year sold pull."""
        data = await self._make_request("BrightProperties", params={
            "$filter": self._closed_window_filter(date_from, date_to),
            "$top": top, "$skip": skip, "$select": self._get_full_field_list(), "$orderby": "ListingKey",
        })
        return data.get("value", [])

    async def get_listings_by_status(self, mls_status: str, top: int = 200, skip: int = 0) -> List[Dict[str, Any]]:
        """One page of every listing in one status (e.g. 'ACTIVE-BRIGHT') - the one-time refresh
        that fills newly-requested fields on listings the 5-minute sync won't touch until they change."""
        safe = mls_status.replace("'", "''")
        data = await self._make_request("BrightProperties", params={
            "$filter": f"MlsStatus eq '{safe}'", "$top": top, "$skip": skip,
            "$select": self._get_full_field_list(), "$orderby": "ListingKey",
        })
        return data.get("value", [])

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

