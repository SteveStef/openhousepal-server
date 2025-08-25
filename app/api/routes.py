from fastapi import APIRouter, Query, HTTPException
import httpx
import os
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

try:
    from ..models.property import (
        HomeStatus, ZillowSearchResponse, PropertyDetailResponse
    )
except ImportError:
    from models.property import (
        HomeStatus, ZillowSearchResponse, PropertyDetailResponse
    )

router = APIRouter()

RAPID_API_KEY = os.getenv("RAPID_API_KEY")
ZILLOW_BASE_URL = "https://zillow56.p.rapidapi.com"

@router.get("/api/search", response_model=ZillowSearchResponse)
async def search_properties(
    location: str = Query(..., description="Location can be an address, neighborhood, city, or ZIP code"),
    page: Optional[int] = Query(0, description="Page number"),
    output: Optional[str] = Query("json", description="Output format"),
    status: Optional[HomeStatus] = Query(HomeStatus.FOR_SALE, description="Status type of the properties"),
    sortSelection: Optional[str] = Query("priorityscore", description="Sorting criteria"),
    listing_type: Optional[str] = Query("by_agent", description="Listing type"),
    price_min: Optional[int] = Query(0, description="Minimum price"),
    price_max: Optional[int] = Query(0, description="Maximum price"),
    beds_min: Optional[int] = Query(0, description="Minimum bedrooms"),
    beds_max: Optional[int] = Query(0, description="Maximum bedrooms"),
    baths_min: Optional[int] = Query(0, description="Minimum bathrooms"),
    baths_max: Optional[int] = Query(0, description="Maximum bathrooms"),
    sqft_min: Optional[int] = Query(0, description="Minimum square footage"),
    sqft_max: Optional[int] = Query(0, description="Maximum square footage"),
    doz: Optional[str] = Query("any", description="Days on Zillow"),
    isSingleFamily: Optional[bool] = Query(None),
    isMultiFamily: Optional[bool] = Query(None),
    isApartment: Optional[bool] = Query(None),
    isCondo: Optional[bool] = Query(None),
    isTownhouse: Optional[bool] = Query(None),
    isLotLand: Optional[bool] = Query(None),
    # Phase 1: High-value parameters
    monthlyPayment_min: Optional[int] = Query(0, description="Minimum monthly payment"),
    monthlyPayment_max: Optional[int] = Query(0, description="Maximum monthly payment"),
    hoa_min: Optional[int] = Query(0, description="Minimum HOA fee"),
    hoa_max: Optional[int] = Query(0, description="Maximum HOA fee"),
    built_min: Optional[int] = Query(0, description="Minimum year built"),
    built_max: Optional[int] = Query(0, description="Maximum year built"),
    hasPool: Optional[bool] = Query(None, description="Has swimming pool"),
    hasGarage: Optional[bool] = Query(None, description="Has garage"),
    lotSize_min: Optional[int] = Query(0, description="Minimum lot size in sqft"),
    lotSize_max: Optional[int] = Query(0, description="Maximum lot size in sqft"),
    parkingSpots_min: Optional[int] = Query(0, description="Minimum parking spots"),
    isNewConstruction: Optional[bool] = Query(None, description="New construction only"),
    keywords: Optional[str] = Query(None, description="Search keywords"),
    # Phase 2: Specialized parameters
    greatSchoolsRating_min: Optional[int] = Query(0, description="Minimum school rating"),
    isWaterfront: Optional[bool] = Query(None, description="Waterfront property"),
    isMountainView: Optional[bool] = Query(None, description="Mountain view"),
    isWaterView: Optional[bool] = Query(None, description="Water view"),
    isForSaleByOwner: Optional[bool] = Query(None, description="For sale by owner"),
    onlyPriceReduction: Optional[bool] = Query(None, description="Price reduced properties only"),
    is3dHome: Optional[bool] = Query(None, description="Has 3D tour"),
    onlyWithPhotos: Optional[bool] = Query(None, description="Properties with photos only")
):
    if not RAPID_API_KEY:
        raise HTTPException(status_code=500, detail="RapidAPI key not configured")
    
    headers = {
        'x-rapidapi-key': RAPID_API_KEY,
        'x-rapidapi-host': "zillow56.p.rapidapi.com"
    }
    
    params = {
        "location": location,
        "output": output,
        "status": status.value if status else "forSale",
        "sortSelection": sortSelection,
        "listing_type": listing_type,
        "doz": doz
    }
    
    if page > 0:
        params["page"] = page
    if price_min > 0:
        params["price_min"] = price_min
    if price_max > 0:
        params["price_max"] = price_max
    if beds_min > 0:
        params["beds_min"] = beds_min
    if beds_max > 0:
        params["beds_max"] = beds_max
    if baths_min > 0:
        params["baths_min"] = baths_min
    if baths_max > 0:
        params["baths_max"] = baths_max
    if sqft_min > 0:
        params["sqft_min"] = sqft_min
    if sqft_max > 0:
        params["sqft_max"] = sqft_max
    
    # Property type filters
    if isSingleFamily is not None:
        params["isSingleFamily"] = str(isSingleFamily).lower()
    if isMultiFamily is not None:
        params["isMultiFamily"] = str(isMultiFamily).lower()
    if isApartment is not None:
        params["isApartment"] = str(isApartment).lower()
    if isCondo is not None:
        params["isCondo"] = str(isCondo).lower()
    if isTownhouse is not None:
        params["isTownhouse"] = str(isTownhouse).lower()
    if isLotLand is not None:
        params["isLotLand"] = str(isLotLand).lower()
    
    # Phase 1: High-value parameters
    if monthlyPayment_min > 0:
        params["monthlyPayment_min"] = monthlyPayment_min
    if monthlyPayment_max > 0:
        params["monthlyPayment_max"] = monthlyPayment_max
    if hoa_min > 0:
        params["hoa_min"] = hoa_min
    if hoa_max > 0:
        params["hoa_max"] = hoa_max
    if built_min > 0:
        params["built_min"] = built_min
    if built_max > 0:
        params["built_max"] = built_max
    if lotSize_min > 0:
        params["lotSize_min"] = lotSize_min
    if lotSize_max > 0:
        params["lotSize_max"] = lotSize_max
    if parkingSpots_min > 0:
        params["parkingSpots_min"] = parkingSpots_min
    if greatSchoolsRating_min > 0:
        params["greatSchoolsRating_min"] = greatSchoolsRating_min
    if keywords:
        params["keywords"] = keywords
    
    # Boolean filters
    if hasPool is not None:
        params["hasPool"] = str(hasPool).lower()
    if hasGarage is not None:
        params["hasGarage"] = str(hasGarage).lower()
    if isNewConstruction is not None:
        params["isNewConstruction"] = str(isNewConstruction).lower()
    
    # Phase 2: Specialized parameters
    if isWaterfront is not None:
        params["isWaterfront"] = str(isWaterfront).lower()
    if isMountainView is not None:
        params["isMountainView"] = str(isMountainView).lower()
    if isWaterView is not None:
        params["isWaterView"] = str(isWaterView).lower()
    if isForSaleByOwner is not None:
        params["isForSaleByOwner"] = str(isForSaleByOwner).lower()
    if onlyPriceReduction is not None:
        params["onlyPriceReduction"] = str(onlyPriceReduction).lower()
    if is3dHome is not None:
        params["is3dHome"] = str(is3dHome).lower()
    if onlyWithPhotos is not None:
        params["onlyWithPhotos"] = str(onlyWithPhotos).lower()
    
    url = f"{ZILLOW_BASE_URL}/search"
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params, timeout=30.0)
            
            if response.status_code == 200:
                data = response.json()
                return ZillowSearchResponse(**data)
            elif response.status_code == 401:
                raise HTTPException(status_code=401, detail="Invalid RapidAPI key")
            elif response.status_code == 429:
                raise HTTPException(status_code=429, detail="Rate limit exceeded")
            else:
                raise HTTPException(status_code=response.status_code, detail=f"External API error: {response.text}")
                
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Request to external API timed out")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to external API: {str(e)}")
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Invalid response from external API: {str(e)}")

@router.get("/api/property", response_model=PropertyDetailResponse)
async def get_property_by_address(
    address: str = Query(..., description="Property address to search for")
):
    if not RAPID_API_KEY:
        raise HTTPException(status_code=500, detail="RapidAPI key not configured")
    
    headers = {
        'x-rapidapi-key': RAPID_API_KEY,
        'x-rapidapi-host': "zillow56.p.rapidapi.com"
    }
    
    params = {
        "address": address
    }
    
    url = f"{ZILLOW_BASE_URL}/search_address"
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params, timeout=30.0)
            
            if response.status_code == 200:
                data = response.json()
                # The API returns the property data directly, not in a wrapper
                return PropertyDetailResponse(**data)
            elif response.status_code == 401:
                raise HTTPException(status_code=401, detail="Invalid RapidAPI key")
            elif response.status_code == 404:
                raise HTTPException(status_code=404, detail="Property not found")
            elif response.status_code == 429:
                raise HTTPException(status_code=429, detail="Rate limit exceeded")
            else:
                raise HTTPException(status_code=response.status_code, detail=f"External API error: {response.text}")
                
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Request to external API timed out")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to external API: {str(e)}")
    except ValidationError as e:
        raise HTTPException(status_code=502, detail=f"Invalid response from external API: {str(e)}")
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Invalid response from external API: {str(e)}")

