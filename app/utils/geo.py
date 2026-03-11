import math
from typing import Tuple, List, Any, Optional

# Constants for geographic calculations
# Approximate miles per degree of latitude
MILES_PER_LAT_DEGREE = 69.1
EARTH_RADIUS_MILES = 3959.0

def get_lat_long_offsets(lat: float, diameter_miles: float) -> Tuple[float, float]:
    """
    Calculates latitude and longitude offsets for a bounding box 
    given a center latitude and a search distance in miles.
    """
    radius_miles = float(diameter_miles)
    lat_offset = radius_miles / MILES_PER_LAT_DEGREE
    cos_lat = math.cos(math.radians(float(lat)))
    
    if abs(cos_lat) > 0.0001:
        long_offset = radius_miles / (MILES_PER_LAT_DEGREE * cos_lat)
    else:
        long_offset = lat_offset
        
    return lat_offset, long_offset

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculates the great-circle distance between two points in miles.
    """
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    
    a = (math.sin(dlat / 2) ** 2 + 
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * 
         math.sin(dlon / 2) ** 2)
    
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_MILES * c

def is_within_distance(lat1: Optional[float], lon1: Optional[float], 
                       lat2: Optional[float], lon2: Optional[float], 
                       max_miles: float) -> bool:
    """
    Checks if two points are within a specified distance.
    Returns False if any coordinate is None.
    """
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return False
    
    try:
        return haversine_distance(float(lat1), float(lon1), float(lat2), float(lon2)) <= float(max_miles)
    except (ValueError, TypeError):
        return False

def filter_properties_by_radius(properties: List[Any], center_lat: float, 
                               center_lon: float, diameter_miles: float) -> List[Any]:
    """
    Trims the 'corners' off a list of properties from a bounding box search,
    returning only those within the exact circular radius.
    
    Works with both objects (p.latitude) and dictionaries (p['Latitude']).
    """
    radius_miles = float(diameter_miles)
    filtered = []
    
    for p in properties:
        # Try object attribute first, then dictionary key
        p_lat = getattr(p, 'latitude', None) or getattr(p, 'Latitude', None)
        p_lon = getattr(p, 'longitude', None) or getattr(p, 'Longitude', None)
        
        if p_lat is None or p_lon is None:
            if isinstance(p, dict):
                p_lat = p.get('latitude') or p.get('Latitude')
                p_lon = p.get('longitude') or p.get('Longitude')

        if is_within_distance(center_lat, center_lon, p_lat, p_lon, radius_miles):
            filtered.append(p)
        elif p_lat is None or p_lon is None:
            # If we can't verify location, keep it to be safe 
            # (though radius searches shouldn't return null-loc properties)
            filtered.append(p)
            
    return filtered
