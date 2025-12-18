Normal Search with these: 
`
import http.client

conn = http.client.HTTPSConnection("zllw-working-api.p.rapidapi.com")

headers = {
    'x-rapidapi-key': "dad8d3d505msh100304f2c4511e8p1e068fjsn5a71e755c970",
    'x-rapidapi-host': "zllw-working-api.p.rapidapi.com"
}

conn.request("GET", "/search/byaddress?location=New%20York%2C%20NY&page=1&sortOrder=Homes_for_you&listingStatus=For_Sale&bed_min=No_Min&bed_max=No_Max&bathrooms=Any&homeType=Houses%2C%20Townhomes%2C%20Multi-family%2C%20Condos%2FCo-ops%2C%20Lots-Land%2C%20Apartments%2C%20Manufactured&maxHOA=Any&listingType=By_Agent&listingTypeOptions=Agent%20listed%2CNew%20Construction%2CFore-closures%2CAuctions&parkingSpots=Any&mustHaveBasement=No&daysOnZillow=Any&soldInLast=Any", headers=headers)

res = conn.getresponse()
data = res.read()

print(data.decode("utf-8"))
`
{
  "location": {
    "required": true,
    "type": "string",
    "example": "New York, NY",
    "notes": "Up to 5 inputs separated by semicolon."
  },
  "page": {
    "required": false,
    "type": "number",
    "default": 1,
    "notes": "Max 5 pages (200 results each)."
  },
  "sortOrder": {
    "required": false,
    "type": "enum",
    "default": {
      "For_Sale": "Homes_for_you",
      "For_Rent": "Rental_Priority_Score",
      "Sold": "Newest"
    }
  },
  "listingStatus": {
    "required": true,
    "type": "enum",
    "possible": ["For_Sale", "For_Rent", "Sold"]
  },
  "listPriceRange": {
    "required": false,
    "type": "string",
    "examples": ["min:5000", "max:500000", "min:5000, max:500000"]
  },
  "monthlyPayment": {
    "required": false,
    "type": "string",
    "usedWhen": "listingStatus = For_Sale",
    "examples": ["min:5000", "max:50000", "min:5000, max:50000"]
  },
  "downPayment": {
    "required": false,
    "type": "number",
    "usedWhen": "listingStatus = For_Sale",
    "example": 15000
  },
  "bed_min": {
    "required": false,
    "type": "enum",
    "default": "No_Min"
  },
  "bed_max": {
    "required": false,
    "type": "enum",
    "default": "No_Max"
  },
  "bathrooms": {
    "required": false,
    "type": "enum",
    "default": "Any",
    "mapping": {
      "Any": "Any",
      "OnePlus": "1+",
      "OneHalfPlus": "1.5+",
      "TwoPlus": "2+",
      "ThreePlus": "3+",
      "FourPlus": "4+"
    }
  },
  "homeType": {
    "required": false,
    "type": "string",
    "multiple": true,
    "possibleForRent": ["Houses", "Apartments/Condos/Co-ops", "Townhomes"],
    "possibleForSaleOrSold": [
      "Houses", "Townhomes", "Multi-family", "Condos/Co-ops",
      "Lots-Land", "Apartments", "Manufactured"
    ]
  },
  "space": {
    "required": false,
    "type": "string",
    "multiple": true,
    "usedWhen": "listingStatus = For_Rent",
    "possible": ["Entire Place", "Room"]
  },
  "maxHOA": {
    "required": false,
    "type": "enum",
    "default": "Any",
    "usedWhen": "listingStatus = For_Sale or Sold"
  },
  "incIncompleteHOA": {
    "required": false,
    "type": "boolean",
    "internal": true,
    "usedWhen": "listingStatus = For_Sale or Sold"
  },
  "listingType": {
    "required": false,
    "type": "enum",
    "default": "By_Agent",
    "usedWhen": "listingStatus = For_Sale"
  },
  "listingTypeOptions": {
    "required": false,
    "type": "string",
    "multiple": true,
    "usedWhen": "listingStatus = For_Sale",
    "possibleByAgent": [
      "Agent listed", "New Construction", "Fore-closures",
      "Auctions", "Foreclosed", "Pre-foreclosures"
    ],
    "possibleByOwnerAndOther": [
      "Owner Posted", "Agent listed", "New Construction",
      "Fore-closures", "Auctions", "Foreclosed", "Pre-foreclosures"
    ]
  },
  "propertyStatus": {
    "required": false,
    "type": "string",
    "multiple": true,
    "usedWhen": "listingStatus = For_Sale",
    "possible": [
      "Coming soon",
      "Accepting backup offers",
      "Pending & under contract"
    ]
  },
  "tours": {
    "required": false,
    "type": "string",
    "multiple": true",
    "possibleForRent": ["Must have 3D Tour"],
    "possibleForSale": ["Must have open house", "Must have 3D Tour"]
  },
  "parkingSpots": {
    "required": false,
    "type": "enum",
    "default": "Any",
    "usedWhen": "listingStatus = For_Sale or Sold",
    "mapping": {
      "Any": "Any",
      "OnePlus": "1+",
      "TwoPlus": "2+",
      "ThreePlus": "3+",
      "FourPlus": "4+"
    }
  },
  "haveGarage": {
    "required": false,
    "type": "boolean",
    "internal": true,
    "usedWhen": "listingStatus = For_Sale or Sold"
  },
  "move_in_date": {
    "required": false,
    "type": "date",
    "format": "yyyy-mm-dd",
    "usedWhen": "listingStatus = For_Rent"
  },
  "hideNoDateListings": {
    "required": false,
    "type": "boolean",
    "internal": true,
    "usedWhen": "listingStatus = For_Rent"
  },
  "squareFeetRange": {
    "required": false,
    "type": "string",
    "examples": ["min:500", "max:5000", "min:500, max:5000"]
  },
  "lotSizeRange": {
    "required": false,
    "type": "string",
    "notes": "Values in sqft. 1 acre = 43560 sqft",
    "examples": ["min:1000", "max:7500", "min:1000, max:7500"]
  },
  "yearBuiltRange": {
    "required": false,
    "type": "string",
    "examples": ["min:2011", "max:2024", "min:2011, max:2024"]
  },
  "mustHaveBasement": {
    "required": false,
    "type": "enum",
    "default": "No"
  },
  "singleStoryOnly": {
    "required": false,
    "type": "boolean",
    "internal": true
  },
  "hide55plusComm": {
    "required": false,
    "type": "boolean",
    "internal": true,
    "usedWhen": "listingStatus = For_Sale or Sold"
  },
  "pets": {
    "required": false,
    "type": "string",
    "multiple": true,
    "usedWhen": "listingStatus = For_Rent",
    "possible": ["Allow large dogs", "Allow small dogs", "Allow cats"]
  },
  "otherAmenities": {
    "required": false,
    "type": "string",
    "multiple": true,
    "possibleForSaleOrSold": ["Must have A/C", "Must have pool", "Waterfront"],
    "possibleForRent": [
      "Must have A/C", "Must have pool", "Waterfront",
      "On-site Parking", "In-unit Laundry", "Accepts Zillow Applications",
      "Income Restricted", "Apartment Community"
    ]
  },
  "view": {
    "required": false,
    "type": "string",
    "multiple": true,
    "possible": ["City", "Mountain", "Park", "Water"]
  },
  "daysOnZillow": {
    "required": false,
    "type": "enum",
    "default": "Any",
    "usedWhen": "listingStatus = For_Sale or For_Rent"
  },
  "soldInLast": {
    "required": false,
    "type": "enum",
    "default": "Any",
    "usedWhen": "listingStatus = Sold"
  },
  "keywords": {
    "required": false,
    "type": "string",
    "examples": ["MLS #", "yard", "fireplace", "horses"]
  }
}

After making the request here is the output example:
```
{
  "message": "200",
  "source": "9vrc_ws_cch",
  "resultsCount": {
    "totalMatchingCount": 21327,
    "ungroupedResultCount": 0,
    "scrapeable_count": 1000
  },
  "pagesInfo": {
    "totalPages": 5,
    "currentPage": 1,
    "resultsPerPage": 200
  },
  "searchResults": [
    {
      "property": {
        "zpid": 2068952873,
        "location": {
          "latitude": 40.766228,
          "longitude": -73.980965
        },
        "address": {
          "streetAddress": "217 W 57th St #127/128",
          "zipcode": "10019",
          "city": "New York",
          "state": "NY"
        },
        "buildingId": 2196725862,
        "media": {
          "propertyPhotoLinks": {
            "mediumSizeLink": "https://photos.zillowstatic.com/fp/e790ef4aa31deebf080cf9083ad1ed00-p_c.jpg",
            "highResolutionLink": "https://photos.zillowstatic.com/fp/e790ef4aa31deebf080cf9083ad1ed00-p_f.jpg"
          },
          "thirdPartyPhotoLinks": {},
          "hasVRModel": false,
          "hasVideos": false,
          "hasIMX": false,
          "hasApprovedThirdPartyVirtualTour": false,
          "allPropertyPhotos": {
            "medium": [],
            "highResolution": []
          }
        },
        "title": "Central Park Tower",
        "isFeatured": false,
        "isShowcaseListing": false,
        "rental": {
          "areApplicationsAccepted": false,
          "hasVirtualTour": false,
          "currency": "usd",
          "country": "usa"
        },
        "listingDateTimeOnZillow": 1762732800000,
        "bestGuessTimeZone": "America/New_York",
        "isUnmappable": false,
        "listCardRecommendation": {
          "flexFieldRecommendations": [
            {
              "displayString": "Price cut: $22,000,000 (11/10)",
              "flexFieldType": "priceCut",
              "contentType": "priceCut"
            },
            {
              "displayString": "Floor-to-ceiling crystalline windows",
              "flexFieldType": "homeInsight",
              "contentType": "homeInsight"
            },
            {
              "displayString": "20 days on Zillow",
              "flexFieldType": "daysOnZillow",
              "contentType": "daysOnZillow"
            }
          ]
        },
        "bathrooms": 10,
        "bedrooms": 8,
        "livingArea": 11535,
        "yearBuilt": 2020,
        "propertyType": "condo",
        "listing": {
          "listingStatus": "forSale",
          "marketingStatus": "active",
          "palsId": "6955001_S1799078",
          "listingSubType": {
            "isFSBA": true
          },
          "daysOnZillow": 20,
          "isPreforeclosureAuction": false
        },
        "price": {
          "value": 128000000,
          "changedDate": 1762750800000,
          "priceChange": -22000000,
          "pricePerSquareFoot": 11097
        },
        "estimates": {
          "rentZestimate": 7106
        },
        "zillowOwnedProperty": {
          "isZillowOwned": false
        },
        "hdpView": {
          "listingStatus": "forSale",
          "price": 128000000,
          "hdpUrl": "/homedetail/MobileAppHDPShopperPlatformServicePage.htm?fromApp=true&p=ipad&variant=FOR_SALE#zpid=2068952873&homeDetailsVariant=FOR_SALE&webviewLayout=doubleScroll&showFactsAndFeatures=true&fromApp=true&gmaps=false&streetview=false"
        },
        "region": {},
        "personalizedResult": {
          "isViewed": false
        },
        "userRecommendation": {
          "isRecommendedForYou": false
        },
        "propertyDisplayRules": {
          "canShowAddress": true,
          "canShowOnMap": true
        },
        "agent": {
          "mls": {
            "brokerName": "Listing by: Compass",
            "mustDisplayBrokerName": true
          }
        },
        "builder": {},
        "soldByOffice": {},
        "listingCategory": "category1",
        "ssid": 6955,
        "hasFloorPlan": false,
        "resultType": "property"
      }
    }
  ]
}
```














coordinate search:
{
  "latitude": {
    "required": true,
    "type": "string",
    "example": "40.599283",
    "notes": "Used with longitude and radius to create a circular search area."
  },
  "longitude": {
    "required": true,
    "type": "string",
    "example": "-74.129194",
    "notes": "Used with latitude and radius to create a circular search area."
  },
  "radius": {
    "required": true,
    "type": "string",
    "example": "0.5",
    "notes": "Radius in miles."
  },
  "page": {
    "required": false,
    "type": "number",
    "default": 1,
    "notes": "Max 5 pages (200 results per call)."
  },
  "sortOrder": {
    "required": false,
    "type": "enum",
    "default": {
      "For_Sale": "Homes_for_you",
      "For_Rent": "Rental_Priority_Score",
      "Sold": "Newest"
    }
  },
  "listingStatus": {
    "required": true,
    "type": "enum",
    "possible": ["For_Sale", "For_Rent", "Sold"]
  },
  "listPriceRange": {
    "required": false,
    "type": "string",
    "examples": ["min:5000", "max:500000", "min:5000, max:500000"]
  },
  "monthlyPayment": {
    "required": false,
    "type": "string",
    "usedWhen": "listingStatus = For_Sale",
    "examples": ["min:5000", "max:50000", "min:5000, max:50000"]
  },
  "downPayment": {
    "required": false,
    "type": "number",
    "usedWhen": "listingStatus = For_Sale",
    "example": 15000
  },
  "bed_min": {
    "required": false,
    "type": "enum",
    "default": "No_Min"
  },
  "bed_max": {
    "required": false,
    "type": "enum",
    "default": "No_Max"
  },
  "bathrooms": {
    "required": false,
    "type": "enum",
    "default": "Any",
    "mapping": {
      "Any": "Any",
      "OnePlus": "1+",
      "OneHalfPlus": "1.5+",
      "TwoPlus": "2+",
      "ThreePlus": "3+",
      "FourPlus": "4+"
    }
  },
  "homeType": {
    "required": false,
    "type": "string",
    "multiple": true,
    "possibleForRent": ["Houses", "Apartments/Condos/Co-ops", "Townhomes"],
    "possibleForSaleOrSold": [
      "Houses", "Townhomes", "Multi-family", "Condos/Co-ops",
      "Lots-Land", "Apartments", "Manufactured"
    ]
  },
  "space": {
    "required": false,
    "type": "string",
    "multiple": true,
    "usedWhen": "listingStatus = For_Rent",
    "possible": ["Entire Place", "Room"]
  },
  "maxHOA": {
    "required": false,
    "type": "enum",
    "default": "Any",
    "usedWhen": "listingStatus = For_Sale or Sold"
  },
  "incIncompleteHOA": {
    "required": false,
    "type": "boolean",
    "internal": true,
    "usedWhen": "listingStatus = For_Sale or Sold"
  },
  "listingType": {
    "required": false,
    "type": "enum",
    "default": "By_Agent",
    "usedWhen": "listingStatus = For_Sale"
  },
  "listingTypeOptions": {
    "required": false,
    "type": "string",
    "multiple": true,
    "usedWhen": "listingStatus = For_Sale",
    "possibleByAgent": [
      "Agent listed", "New Construction", "Fore-closures",
      "Auctions", "Foreclosed", "Pre-foreclosures"
    ],
    "possibleByOwnerAndOther": [
      "Owner Posted", "Agent listed", "New Construction",
      "Fore-closures", "Auctions", "Foreclosed", "Pre-foreclosures"
    ]
  },
  "propertyStatus": {
    "required": false,
    "type": "string",
    "multiple": true,
    "usedWhen": "listingStatus = For_Sale",
    "possible": [
      "Coming soon",
      "Accepting backup offers",
      "Pending & under contract"
    ]
  },
  "tours": {
    "required": false,
    "type": "string",
    "multiple": true,
    "possibleForRent": ["Must have 3D Tour"],
    "possibleForSale": ["Must have open house", "Must have 3D Tour"]
  },
  "parkingSpots": {
    "required": false,
    "type": "enum",
    "default": "Any",
    "usedWhen": "listingStatus = For_Sale or Sold",
    "mapping": {
      "Any": "Any",
      "OnePlus": "1+",
      "TwoPlus": "2+",
      "ThreePlus": "3+",
      "FourPlus": "4+"
    }
  },
  "haveGarage": {
    "required": false,
    "type": "boolean",
    "internal": true,
    "usedWhen": "listingStatus = For_Sale or Sold"
  },
  "move_in_date": {
    "required": false,
    "type": "date",
    "format": "yyyy-mm-dd",
    "usedWhen": "listingStatus = For_Rent"
  },
  "hideNoDateListings": {
    "required": false,
    "type": "boolean",
    "internal": true,
    "usedWhen": "listingStatus = For_Rent"
  },
  "squareFeetRange": {
    "required": false,
    "type": "string",
    "examples": ["min:500", "max:5000", "min:500, max:5000"]
  },
  "lotSizeRange": {
    "required": false,
    "type": "string",
    "notes": "Values in sqft. 1 acre = 43560 sqft",
    "examples": ["min:1000", "max:7500", "min:1000, max:7500"]
  },
  "yearBuiltRange": {
    "required": false,
    "type": "string",
    "examples": ["min:2011", "max:2024", "min:2011, max:2024"]
  },
  "mustHaveBasement": {
    "required": false,
    "type": "enum",
    "default": "No"
  },
  "singleStoryOnly": {
    "required": false,
    "type": "boolean",
    "internal": true
  },
  "hide55plusComm": {
    "required": false,
    "type": "boolean",
    "internal": true,
    "usedWhen": "listingStatus = For_Sale or Sold"
  },
  "pets": {
    "required": false,
    "type": "string",
    "multiple": true,
    "usedWhen": "listingStatus = For_Rent",
    "possible": ["Allow large dogs", "Allow small dogs", "Allow cats"]
  },
  "otherAmenities": {
    "required": false,
    "type": "string",
    "multiple": true,
    "possibleForSaleOrSold": ["Must have A/C", "Must have pool", "Waterfront"],
    "possibleForRent": [
      "Must have A/C", "Must have pool", "Waterfront",
      "On-site Parking", "In-unit Laundry", "Accepts Zillow Applications",
      "Income Restricted", "Apartment Community"
    ]
  },
  "view": {
    "required": false,
    "type": "string",
    "multiple": true,
    "possible": ["City", "Mountain", "Park", "Water"]
  },
  "daysOnZillow": {
    "required": false,
    "type": "enum",
    "default": "Any",
    "usedWhen": "listingStatus = For_Sale or For_Rent"
  },
  "soldInLast": {
    "required": false,
    "type": "enum",
    "default": "Any",
    "usedWhen": "listingStatus = Sold"
  },
  "keywords": {
    "required": false,
    "type": "string",
    "examples": ["MLS #", "yard", "fireplace", "horses"]
  }
}

example request with these parameters:

import http.client

conn = http.client.HTTPSConnection("zllw-working-api.p.rapidapi.com")

headers = {
    'x-rapidapi-key': "dad8d3d505msh100304f2c4511e8p1e068fjsn5a71e755c970",
    'x-rapidapi-host': "zllw-working-api.p.rapidapi.com"
}

conn.request("GET", "/search/bycoordinates?latitude=40.599283&longitude=-74.129194&radius=0.5&page=1&sortOrder=Homes_for_you&listingStatus=For_Sale&bed_min=No_Min&bed_max=No_Max&bathrooms=Any&homeType=Houses%2C%20Townhomes%2C%20Multi-family%2C%20Condos%2FCo-ops%2C%20Lots-Land%2C%20Apartments%2C%20Manufactured&maxHOA=Any&listingType=By_Agent&listingTypeOptions=Agent%20listed%2CNew%20Construction%2CFore-closures%2CAuctions&parkingSpots=Any&mustHaveBasement=No&daysOnZillow=Any&soldInLast=Any", headers=headers)

res = conn.getresponse()
data = res.read()

print(data.decode("utf-8"))


output of this request:
```
{
  "message": "200",
  "source": "9vrc_ws_cch",
  "resultsCount": {
    "totalMatchingCount": 12,
    "ungroupedResultCount": 0,
    "scrapeable_count": 12
  },
  "pagesInfo": {
    "totalPages": 1,
    "currentPage": 1,
    "resultsPerPage": 200
  },
  "searchResults": [
    {
      "property": {
        "zpid": 457822962,
        "location": {
          "latitude": 40.601616,
          "longitude": -74.1329
        },
        "address": {
          "streetAddress": "343 Harold St #3B",
          "zipcode": "10314",
          "city": "Staten Island",
          "state": "NY"
        },
        "buildingId": 2008856724,
        "media": {
          "propertyPhotoLinks": {
            "mediumSizeLink": "https://photos.zillowstatic.com/fp/a80d2fbd4a09b96fd468e292434f54ec-p_c.jpg",
            "highResolutionLink": "https://photos.zillowstatic.com/fp/a80d2fbd4a09b96fd468e292434f54ec-p_f.jpg"
          },
          "thirdPartyPhotoLinks": {},
          "hasVRModel": false,
          "hasVideos": false,
          "hasIMX": false,
          "hasApprovedThirdPartyVirtualTour": false,
          "allPropertyPhotos": {
            "highResolution": [],
            "medium": []
          }
        },
        "isFeatured": false,
        "isShowcaseListing": false,
        "rental": {
          "areApplicationsAccepted": false,
          "hasVirtualTour": true,
          "currency": "usd",
          "country": "usa"
        },
        "listingDateTimeOnZillow": 1761747267358,
        "bestGuessTimeZone": "America/New_York",
        "isUnmappable": false,
        "listCardRecommendation": {
          "flexFieldRecommendations": [
            {
              "displayString": "Corner unit",
              "flexFieldType": "homeInsight",
              "contentType": "homeInsight"
            },
            {
              "displayString": "31 days on Zillow",
              "flexFieldType": "daysOnZillow",
              "contentType": "daysOnZillow"
            }
          ]
        },
        "bathrooms": 1,
        "bedrooms": 2,
        "livingArea": 906,
        "yearBuilt": 1986,
        "lotSizeWithUnit": {
          "lotSize": 871.2,
          "lotSizeUnit": "squareFeet"
        },
        "propertyType": "condo",
        "listing": {
          "listingStatus": "forSale",
          "marketingStatus": "active",
          "palsId": "820001_2506362",
          "listingSubType": {
            "isFSBA": true
          },
          "daysOnZillow": 31,
          "isPreforeclosureAuction": false
        },
        "price": {
          "value": 399000,
          "pricePerSquareFoot": 440
        },
        "estimates": {
          "zestimate": 402300
        },
        "zillowOwnedProperty": {
          "isZillowOwned": false
        },
        "hdpView": {
          "listingStatus": "forSale",
          "price": 399000,
          "hdpUrl": "/homedetail/MobileAppHDPShopperPlatformServicePage.htm?fromApp=true&p=ipad&variant=FOR_SALE#zpid=457822962&homeDetailsVariant=FOR_SALE&webviewLayout=doubleScroll&showFactsAndFeatures=true&fromApp=true&gmaps=false&streetview=false"
        },
        "region": {},
        "personalizedResult": {
          "isViewed": false
        },
        "userRecommendation": {
          "isRecommendedForYou": false
        },
        "propertyDisplayRules": {
          "canShowAddress": true,
          "canShowOnMap": true
        },
        "agent": {
          "mls": {
            "brokerName": "Listing by: Prereal Prendamano Real Estate",
            "mustDisplayBrokerName": true
          }
        },
        "builder": {},
        "soldByOffice": {},
        "listingCategory": "category1",
        "ssid": 820,
        "hasFloorPlan": false,
        "resultType": "property"
      }
    }
  ]
}

```
