import pytest
import asyncio
import os
from app.services.bright_mls_service import bright_mls_service
from app.schemas.collection_preferences import CollectionPreferencesBase
from dotenv import load_dotenv

# Load real environment variables
load_dotenv()

@pytest.fixture
async def mls_service():
    yield bright_mls_service

@pytest.mark.asyncio
async def test_real_auth(mls_service):
    """Test that we can actually authenticate with Bright MLS"""
    token = await mls_service._get_access_token()
    assert token is not None
    assert len(token) > 0
    print(f"
✅ Authenticated successfully. Token prefix: {token[:10]}...")

@pytest.mark.asyncio
async def test_real_property_search_by_address(mls_service):
    """Test fetching a real property by address"""
    # Using a known address that likely exists in Bright MLS (Philly area)
    # Adjust this to an address you know exists if this fails
    address = "300 Valley Pl, Radnor, PA 19087"
    try:
        prop = await mls_service.get_property_by_address(address)
        assert prop is not None
        assert prop["address"].lower().strip() == "300 valley pl"
        assert prop["city"] == "Radnor"
        assert "listing_key" in prop
        print(f"
✅ Found property: {prop['address']}, {prop['city']} (MLS ID: {prop['mls_id']})")
    except Exception as e:
        pytest.fail(f"Real property search failed: {e}")

@pytest.mark.asyncio
async def test_real_property_search_by_preferences(mls_service):
    """Test fetching properties using real preferences"""
    prefs = CollectionPreferencesBase(
        cities=["Philadelphia, PA"],
        min_price=400000,
        max_price=600000,
        min_beds=3,
        is_single_family=True
    )
    
    properties = await mls_service.get_properties_by_preferences(prefs, max_properties=5)
    assert isinstance(properties, list)
    assert len(properties) > 0
    
    for p in properties:
        assert p["price"] >= 400000
        assert p["price"] <= 600000
        assert p["bedrooms"] >= 3
        
    print(f"
✅ Found {len(properties)} properties matching preferences in Philadelphia.")

@pytest.mark.asyncio
async def test_real_media_fetch(mls_service):
    """Test fetching real media (photos) for a property"""
    # First find a property
    prefs = CollectionPreferencesBase(cities=["Wayne, PA"], max_price=1000000)
    properties = await mls_service.get_properties_by_preferences(prefs, max_properties=1)
    
    if not properties:
        pytest.skip("No properties found to test media fetch")
        
    listing_key = properties[0]["listing_key"]
    media = await mls_service._fetch_all_media(listing_key)
    
    assert isinstance(media, list)
    # Some properties might not have photos, but most do
    print(f"
✅ Found {len(media)} photos for listing {listing_key}")
