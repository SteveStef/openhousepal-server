
# Zillow API Documentation

## Base Endpoint
```
GET /api/search
```

## Authentication
This API uses RapidAPI key authentication handled internally by the server.

## Example Requests

### Basic Search
```bash
GET /api/search?location=Villanova PA, 19085
```

### Advanced Search with Filters
```bash
GET /api/search?location=New York, NY&status=forSale&price_min=500000&price_max=2000000&beds_min=2&baths_min=2&isSingleFamily=true&sortSelection=pricea
```

### Using cURL
```bash
curl "http://localhost:8080/api/search?location=Villanova%20PA%2C%2019085&status=forSale&price_max=1500000"
```

## Python Example
```python
import httpx

response = httpx.get("http://localhost:8080/api/search", params={
    "location": "Villanova PA, 19085",
    "status": "forSale",
    "price_min": 500000,
    "price_max": 2000000,
    "beds_min": 2,
    "isSingleFamily": True
})

data = response.json()
print(f"Found {data['totalResultCount']} properties")
```


## Query Parameters

### Required Parameters

| Parameter | Type | Description | Example |
|-----------|------|-------------|---------|
| `location` | string | Location can be an address, neighborhood, city, or ZIP code | `"Villanova PA, 19085"` |

### Optional Search Parameters

| Parameter | Type | Default | Description | Possible Values |
|-----------|------|---------|-------------|-----------------|
| `page` | number | `0` | Page number for pagination | Any integer |
| `output` | string | `"json"` | Output format | `"json"`, `"csv"`, `"xlsx"` |
| `status` | enum | `"forSale"` | Property status | `"forSale"`, `"forRent"`, `"recentlySold"` |
| `sortSelection` | string | `"priorityscore"` | Sorting criteria | See sorting options below |
| `listing_type` | string | `"by_agent"` | Listing type | `"by_agent"`, `"by_owner_other"` |
| `doz` | string | `"any"` | Days on Zillow filter | `"any"`, `"1"`, `"7"`, `"14"`, `"30"`, `"90"`, `"6m"`, `"12m"`, `"24m"`, `"36m"` |

### Price & Size Filters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `price_min` | number | `0` | Minimum price |
| `price_max` | number | `0` | Maximum price |
| `beds_min` | number | `0` | Minimum bedrooms |
| `beds_max` | number | `0` | Maximum bedrooms |
| `baths_min` | number | `0` | Minimum bathrooms |
| `baths_max` | number | `0` | Maximum bathrooms |
| `sqft_min` | number | `0` | Minimum square footage |
| `sqft_max` | number | `0` | Maximum square footage |

### Property Type Filters

| Parameter | Type | Description |
|-----------|------|-------------|
| `isSingleFamily` | boolean | Filter for single family homes |
| `isMultiFamily` | boolean | Filter for multi-family homes |
| `isApartment` | boolean | Filter for apartments |
| `isCondo` | boolean | Filter for condos |
| `isTownhouse` | boolean | Filter for townhouses |
| `isLotLand` | boolean | Filter for lots/land |

### Phase 1: High-Value Filters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `monthlyPayment_min` | number | `0` | Minimum monthly payment |
| `monthlyPayment_max` | number | `0` | Maximum monthly payment |
| `hoa_min` | number | `0` | Minimum HOA fee |
| `hoa_max` | number | `0` | Maximum HOA fee |
| `built_min` | number | `0` | Minimum year built |
| `built_max` | number | `0` | Maximum year built |
| `lotSize_min` | number | `0` | Minimum lot size in sqft |
| `lotSize_max` | number | `0` | Maximum lot size in sqft |
| `parkingSpots_min` | number | `0` | Minimum parking spots |
| `hasPool` | boolean | | Has swimming pool |
| `hasGarage` | boolean | | Has garage |
| `isNewConstruction` | boolean | | New construction only |
| `keywords` | string | | Search keywords |

### Phase 2: Specialized Filters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `greatSchoolsRating_min` | number | `0` | Minimum school rating |
| `isWaterfront` | boolean | | Waterfront property |
| `isMountainView` | boolean | | Mountain view |
| `isWaterView` | boolean | | Water view |
| `isForSaleByOwner` | boolean | | For sale by owner |
| `onlyPriceReduction` | boolean | | Price reduced properties only |
| `onlyWithPhotos` | boolean | | Properties with photos only |

### Sorting Options

The `sortSelection` parameter accepts these values:

- `"days"` - Newest (default)
- `"priced"` - Price (High to Low)
- `"pricea"` - Price (Low to High)
- `"beds"` - Bedrooms
- `"baths"` - Bathrooms
- `"size"` - Square Feet
- `"lot"` - Lot Size
- `"zest"` - Zestimate (High to Low)
- `"zesta"` - Zestimate (Low to High)
- `"priorityscore"` - Priority Score

## Response Format

The API returns a JSON object with the following structure:

```json
{
  "results": [
    {
      "bathrooms": 4,
      "bedrooms": 4,
      "city": "Villanova",
      "country": "USA",
      "currency": "USD",
      "price": 1375000,
      "streetAddress": "526 N Spring Mill Rd",
      "state": "PA",
      "zipcode": "19085",
      "latitude": 40.04451,
      "longitude": -75.32732,
      "imgSrc": "https://photos.zillowstatic.com/...",
      "homeType": "SINGLE_FAMILY",
      "homeStatus": "FOR_SALE",
      // ... additional property fields
    }
  ],
  "resultsPerPage": 41,
  "totalPages": 1,
  "totalResultCount": 15
}
```

## Error Responses

| Status Code | Description |
|-------------|-------------|
| `400` | Bad Request - Invalid parameters |
| `401` | Unauthorized - Invalid API key |
| `429` | Too Many Requests - Rate limit exceeded |
| `500` | Internal Server Error - API key not configured |
| `502` | Bad Gateway - External API error |
| `504` | Gateway Timeout - Request timeout |
