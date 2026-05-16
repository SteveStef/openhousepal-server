import re
from typing import Tuple

# 1. Define the Master List of major brands to be grouped by prefix
MAJOR_BRANDS = [
    "KELLER WILLIAMS", "RE/MAX", "CENTURY 21", "COMPASS", 
    "COLDWELL BANKER", "LONG & FOSTER", "BERKSHIRE HATHAWAY", 
    "EXP REALTY", "REDFIN", "WEICHERT", "IRON VALLEY", 
    "NEXTHOME", "REALTY ONE", "HOWARD HANNA", "SAMSON PROPERTIES",
    "REAL BROKER", "SOTHEBY'S INTERNATIONAL REALTY", "TTR SOTHEBY'S",
    "MONUMENT SOTHEBY'S", "ERA ", "BETTER HOMES", "UNITED REAL ESTATE",
    "EXIT REALTY", "HOMESMART", "HOMESALE REALTY"
]

def normalize_township(name: str) -> str:
    """Standardizes township names and expands common abbreviations."""
    if not name:
        return ""
    
    val = name.upper().strip()
    
    # Expand known MLS abbreviations
    mappings = {
        'AA': 'ANNE ARUNDEL COUNTY',
        'AA COUNTY': 'ANNE ARUNDEL COUNTY',
        'BA': 'BALTIMORE CITY',
        'BA COUNTY': 'BALTIMORE CITY',
        'BC': 'BALTIMORE COUNTY',
        'BC COUNTY': 'BALTIMORE COUNTY'
    }
    
    return mappings.get(val, val)

def normalize_school_district(name: str) -> str:
    """Standardizes school district names."""
    if not name:
        return ""
    return name.upper().strip()

def normalize_brokerage(raw_name: str) -> Tuple[str, str]:
    """
    Standardizes the office name and determines its Parent Brand.
    Returns: (name, parent_name)
    - name: The key used to join with properties (matches raw MLS data).
    - parent_name: The brand name used for grouping/filtering.
    """
    if not raw_name:
        return "", ""

    # --- TIER 1: The 'Key' Name (Must match Property data) ---
    # We only perform basic cleanup (upper/trim) to ensure it joins correctly 
    # with the properties table.
    name = raw_name.strip().upper()

    # --- TIER 2: Determine Parent Brand (The Category) ---
    # We use a temporary cleaned version for detection logic only.
    
    # 1. Remove hyphens/slashes/dots for better matching
    clean_for_match = re.sub(r'[.,/\-]', ' ', name)
    # 2. Strip legal suffixes (LLC, INC)
    clean_for_match = re.sub(r'(,?\s*(LLC|INC|LTD)\.?)$', '', clean_for_match, flags=re.IGNORECASE)
    # 3. Collapse double spaces
    clean_for_match = re.sub(r'\s+', ' ', clean_for_match).strip()

    # A. Special Abbreviation Mappings
    if clean_for_match.startswith("KW ") or clean_for_match == "KW":
        return name, "KELLER WILLIAMS"
    if clean_for_match.startswith("RE MAX ") or clean_for_match.startswith("REMAX "):
        return name, "RE/MAX"
    if clean_for_match.startswith("BHHS "):
        return name, "BERKSHIRE HATHAWAY"
    if clean_for_match.startswith("C 21 "):
        return name, "CENTURY 21"

    # B. Master Array Prefix Match
    for brand in MAJOR_BRANDS:
        # Clean the brand name too (remove slashes/hyphens) to ensure a match
        clean_brand = re.sub(r'[.,/\-]', ' ', brand).strip().upper()
        # Collapse spaces
        clean_brand = re.sub(r'\s+', ' ', clean_brand)
        if clean_for_match.startswith(clean_brand):
            return name, brand

    # C. Smart Stemming Fallback (First 3 significant words minus fluff)
    fluff_pattern = r'\b(REALTY|PROPERTIES|REAL ESTATE|GROUP|SERVICES|ASSOCIATES|ASSOC|RE|LLC|INC|LTD|COMPANY|CO|CORP|CORPORATION|REALTORS|REALTOR)\b'
    
    # Extract first 3 chunks from the match-ready string
    chunks = clean_for_match.split()
    first_three_chunks = " ".join(chunks[:3])
    
    # Strip fluff words
    parent_name = re.sub(fluff_pattern, '', first_three_chunks, flags=re.IGNORECASE).strip()
    # Strip trailing symbols
    parent_name = re.sub(r'[& ]+$', '', parent_name).strip()
    
    if not parent_name:
        parent_name = clean_for_match

    return name, parent_name
