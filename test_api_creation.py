#!/usr/bin/env python3
"""
Test script to verify open house creation API endpoint with nested address structure
"""
import requests
import json

# Test data that mimics the nested structure from Zillow API
test_property_data = {
    "address": {
        "city": "Devon",
        "community": None,
        "neighborhood": None,
        "state": "PA", 
        "streetAddress": "6 Samantha Way",
        "subdivision": "Brownstones At Berkley",
        "zipcode": "19333"
    },
    "abbreviatedAddress": "6 Samantha Way",
    "propertyType": "SINGLE_FAMILY",
    "latitude": 40.046825,
    "longitude": -75.41332,
    "bedrooms": 4,
    "bathrooms": 5.0,
    "livingArea": 3400,
    "price": 1461500,
    "yearBuilt": 2023,
    "homeStatus": "RECENTLY_SOLD"
}

# First, let's create a test user and get auth token
def create_test_user():
    user_data = {
        "email": "test@example.com",
        "password": "testpassword123",
        "first_name": "Test",
        "last_name": "Agent",
        "state": "PA",
        "brokerage": "Test Brokerage"
    }
    
    try:
        response = requests.post("http://localhost:8000/api/auth/register", json=user_data)
        if response.status_code == 409:  # User already exists
            print("Test user already exists, logging in...")
            login_data = {"username": user_data["email"], "password": user_data["password"]}
            response = requests.post("http://localhost:8000/api/auth/token", data=login_data)
        else:
            print(f"User registration response: {response.status_code}")
            if response.status_code == 200:
                print("User created successfully, logging in...")
                login_data = {"username": user_data["email"], "password": user_data["password"]}
                response = requests.post("http://localhost:8000/api/auth/token", data=login_data)
        
        if response.status_code == 200:
            token_data = response.json()
            return token_data.get("access_token")
        else:
            print(f"Login failed: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        print(f"Error creating/logging in user: {e}")
        return None

def test_open_house_creation(token):
    headers = {"Authorization": f"Bearer {token}"}
    
    # Create open house request
    open_house_data = {
        "property_data": test_property_data,
        "cover_image_url": "https://example.com/image.jpg"
    }
    
    try:
        print("\n🚀 Testing open house creation with nested address structure...")
        print(f"Property data being sent: {json.dumps(test_property_data, indent=2)}")
        
        response = requests.post(
            "http://localhost:8000/api/open-houses",
            json=open_house_data,
            headers=headers
        )
        
        print(f"\nAPI Response: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            print("✅ Open house created successfully!")
            print(f"   ID: {result.get('id')}")
            print(f"   Address: {result.get('address')}")
            print(f"   QR Code URL: {result.get('qr_code_url')}")
            print(f"   Form URL: {result.get('form_url')}")
            return True
        else:
            print(f"❌ Open house creation failed: {response.status_code}")
            print(f"   Error: {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Error testing open house creation: {e}")
        return False

if __name__ == "__main__":
    print("Testing Open House API with nested address structure...")
    
    # Get auth token
    token = create_test_user()
    if not token:
        print("❌ Failed to get authentication token")
        exit(1)
    
    print("✅ Authentication successful")
    
    # Test open house creation
    success = test_open_house_creation(token)
    
    if success:
        print("\n🎉 All tests passed! The address extraction fix is working correctly.")
    else:
        print("\n💥 Test failed. There may still be issues with the address extraction logic.")