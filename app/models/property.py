from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

class HomeStatus(str, Enum):
    FOR_SALE = "forSale"
    FOR_RENT = "forRent"
    RECENTLY_SOLD = "recentlySold"

class HomeType(str, Enum):
    SINGLE_FAMILY = "SINGLE_FAMILY"
    TOWNHOUSE = "TOWNHOUSE"
    CONDO = "CONDO"
    MULTI_FAMILY = "MULTI_FAMILY"
    LOT = "LOT"

class ListingSubType(BaseModel):
    is_FSBA: Optional[bool] = None
    is_openHouse: Optional[bool] = None

class OpenHouseShowing(BaseModel):
    open_house_end: Optional[int] = None
    open_house_start: Optional[int] = None

class OpenHouseInfo(BaseModel):
    open_house_showing: Optional[List[OpenHouseShowing]] = None

class PropertyListing(BaseModel):
    bathrooms: Optional[float] = None
    bedrooms: Optional[int] = None
    city: Optional[str] = None
    country: Optional[str] = None
    currency: Optional[str] = None
    datePriceChanged: Optional[int] = None
    daysOnZillow: Optional[int] = None
    homeStatus: Optional[str] = None
    homeStatusForHDP: Optional[str] = None
    homeType: Optional[str] = None
    imgSrc: Optional[str] = None
    isFeatured: Optional[bool] = None
    isNonOwnerOccupied: Optional[bool] = None
    isPreforeclosureAuction: Optional[bool] = None
    isPremierBuilder: Optional[bool] = None
    isShowcaseListing: Optional[bool] = None
    isUnmappable: Optional[bool] = None
    isZillowOwned: Optional[bool] = None
    latitude: Optional[float] = None
    listing_sub_type: Optional[ListingSubType] = None
    livingArea: Optional[int] = None
    longitude: Optional[float] = None
    lotAreaUnit: Optional[str] = None
    lotAreaValue: Optional[float] = None
    openHouse: Optional[str] = None
    open_house_info: Optional[OpenHouseInfo] = None
    price: Optional[int] = None
    priceChange: Optional[int] = None
    priceForHDP: Optional[int] = None
    priceReduction: Optional[str] = None
    rentZestimate: Optional[int] = None
    shouldHighlight: Optional[bool] = None
    state: Optional[str] = None
    streetAddress: Optional[str] = None
    taxAssessedValue: Optional[int] = None
    timeOnZillow: Optional[int] = None
    unit: Optional[str] = None
    zestimate: Optional[int] = None
    zipcode: Optional[str] = None
    zpid: Optional[int] = None

class ZillowSearchResponse(BaseModel):
    results: List[PropertyListing]
    resultsPerPage: int
    totalPages: int
    totalResultCount: int

class PropertyAddress(BaseModel):
    city: Optional[str] = None
    community: Optional[str] = None
    neighborhood: Optional[str] = None
    state: Optional[str] = None
    streetAddress: Optional[str] = None
    subdivision: Optional[str] = None
    zipcode: Optional[str] = None

class ImageSource(BaseModel):
    url: Optional[str] = None
    width: Optional[int] = None

class MixedSources(BaseModel):
    jpeg: Optional[List[ImageSource]] = None
    webp: Optional[List[ImageSource]] = None

class OriginalPhoto(BaseModel):
    caption: Optional[str] = None
    mixedSources: Optional[MixedSources] = None

class AtAGlanceFact(BaseModel):
    factLabel: Optional[str] = None
    factValue: Optional[str] = None

class OtherFact(BaseModel):
    name: Optional[str] = None
    value: Optional[str] = None

class ResoFacts(BaseModel):
    aboveGradeFinishedArea: Optional[str] = None
    accessibilityFeatures: Optional[List[str]] = None
    additionalFeeInfo: Optional[str] = None
    additionalParcelsDescription: Optional[str] = None
    appliances: Optional[List[str]] = None
    architecturalStyle: Optional[str] = None
    associationAmenities: Optional[str] = None
    associationFee: Optional[str] = None
    associationFee2: Optional[str] = None
    associationFeeIncludes: Optional[List[str]] = None
    associationName: Optional[str] = None
    associationName2: Optional[str] = None
    associationPhone: Optional[str] = None
    associationPhone2: Optional[str] = None
    associations: Optional[List[Dict[str, Any]]] = None
    atAGlanceFacts: Optional[List[Any]] = None  # Keep as-is unless you provide AtAGlanceFact model
    attic: Optional[str] = None
    availabilityDate: Optional[str] = None
    basement: Optional[str] = None
    basementYN: Optional[bool] = None
    bathrooms: Optional[int] = None
    bathroomsFloat: Optional[float] = None
    bathroomsFull: Optional[int] = None
    bathroomsHalf: Optional[int] = None
    bathroomsOneQuarter: Optional[int] = None
    bathroomsPartial: Optional[int] = None
    bathroomsThreeQuarter: Optional[int] = None
    bedrooms: Optional[int] = None
    belowGradeFinishedArea: Optional[str] = None
    bodyType: Optional[str] = None
    builderModel: Optional[str] = None
    builderName: Optional[str] = None
    buildingArea: Optional[str] = None
    buildingAreaSource: Optional[str] = None
    buildingFeatures: Optional[str] = None
    buildingName: Optional[str] = None
    canRaiseHorses: Optional[bool] = None
    carportParkingCapacity: Optional[int] = None
    cityRegion: Optional[str] = None
    commonWalls: Optional[str] = None
    communityFeatures: Optional[List[str]] = None
    compensationBasedOn: Optional[str] = None
    constructionMaterials: Optional[List[str]] = None
    contingency: Optional[str] = None
    cooling: Optional[List[str]] = None
    coveredParkingCapacity: Optional[int] = None
    cropsIncludedYN: Optional[bool] = None
    cumulativeDaysOnMarket: Optional[int] = None
    developmentStatus: Optional[str] = None
    doorFeatures: Optional[str] = None
    electric: Optional[List[str]] = None
    elementarySchool: Optional[str] = None
    elementarySchoolDistrict: Optional[str] = None
    elevation: Optional[str] = None
    elevationUnits: Optional[str] = None
    entryLevel: Optional[str] = None
    entryLocation: Optional[str] = None
    exclusions: Optional[List[str]] = None
    exteriorFeatures: Optional[List[str]] = None
    feesAndDues: Optional[List[Dict[str, Any]]] = None
    fencing: Optional[str] = None
    fireplaceFeatures: Optional[List[str]] = None
    fireplaces: Optional[int] = None
    flooring: Optional[List[str]] = None
    foundationArea: Optional[str] = None
    foundationDetails: Optional[List[str]] = None
    frontageLength: Optional[str] = None
    frontageType: Optional[str] = None
    furnished: Optional[bool] = None
    garageParkingCapacity: Optional[int] = None
    gas: Optional[str] = None
    greenBuildingVerificationType: Optional[str] = None
    greenEnergyEfficient: Optional[str] = None
    greenEnergyGeneration: Optional[str] = None
    greenIndoorAirQuality: Optional[str] = None
    greenSustainability: Optional[str] = None
    greenWaterConservation: Optional[str] = None
    hasAdditionalParcels: Optional[bool] = None
    hasAssociation: Optional[bool] = None
    hasAttachedGarage: Optional[bool] = None
    hasAttachedProperty: Optional[bool] = None
    hasCarport: Optional[bool] = None
    hasCooling: Optional[bool] = None
    hasElectricOnProperty: Optional[bool] = None
    hasFireplace: Optional[bool] = None
    hasGarage: Optional[bool] = None
    hasHeating: Optional[bool] = None
    hasHomeWarranty: Optional[bool] = None
    hasLandLease: Optional[bool] = None
    hasOpenParking: Optional[bool] = None
    hasPetsAllowed: Optional[bool] = None
    hasPrivatePool: Optional[bool] = None
    hasRentControl: Optional[bool] = None
    hasSpa: Optional[bool] = None
    hasView: Optional[bool] = None
    hasWaterfrontView: Optional[bool] = None
    heating: Optional[List[str]] = None
    highSchool: Optional[str] = None
    highSchoolDistrict: Optional[str] = None
    hoaFee: Optional[str] = None
    hoaFeeTotal: Optional[str] = None
    homeType: Optional[str] = None
    horseAmenities: Optional[str] = None
    horseYN: Optional[bool] = None
    inclusions: Optional[List[str]] = None
    incomeIncludes: Optional[str] = None
    interiorFeatures: Optional[List[str]] = None
    irrigationWaterRightsAcres: Optional[float] = None
    irrigationWaterRightsYN: Optional[bool] = None
    isNewConstruction: Optional[bool] = None
    isSeniorCommunity: Optional[bool] = None
    landLeaseAmount: Optional[float] = None
    landLeaseExpirationDate: Optional[str] = None
    laundryFeatures: Optional[List[str]] = None
    leaseTerm: Optional[str] = None
    levels: Optional[str] = None
    listAOR: Optional[str] = None
    listingId: Optional[str] = None
    listingTerms: Optional[str] = None
    livingArea: Optional[str] = None
    livingAreaRange: Optional[str] = None
    livingAreaRangeUnits: Optional[str] = None
    livingQuarters: Optional[List[str]] = None
    lotFeatures: Optional[List[str]] = None
    lotSize: Optional[str] = None
    lotSizeDimensions: Optional[str] = None
    mainLevelBathrooms: Optional[int] = None
    mainLevelBedrooms: Optional[int] = None
    marketingType: Optional[str] = None
    media: Optional[List[str]] = None
    middleOrJuniorSchool: Optional[str] = None
    middleOrJuniorSchoolDistrict: Optional[str] = None
    municipality: Optional[str] = None
    numberOfUnitsInCommunity: Optional[int] = None
    numberOfUnitsVacant: Optional[int] = None
    offerReviewDate: Optional[str] = None
    onMarketDate: Optional[datetime] = None
    openParkingCapacity: Optional[int] = None
    otherEquipment: Optional[str] = None
    otherFacts: Optional[List[OtherFact]] = None
    otherParking: Optional[str] = None
    otherStructures: Optional[List[str]] = None
    ownership: Optional[str] = None
    ownershipType: Optional[str] = None
    parcelNumber: Optional[str] = None
    parkName: Optional[str] = None
    parkingCapacity: Optional[int] = None
    parkingFeatures: Optional[List[str]] = None
    patioAndPorchFeatures: Optional[List[str]] = None
    petsMaxWeight: Optional[int] = None
    poolFeatures: Optional[List[str]] = None
    pricePerSquareFoot: Optional[int] = None
    propertyCondition: Optional[str] = None
    propertySubType: Optional[List[str]] = None
    roadSurfaceType: Optional[str] = None
    roofType: Optional[str] = None
    roomTypes: Optional[str] = None
    rooms: Optional[List[Dict[str, Any]]] = None
    securityFeatures: Optional[List[str]] = None
    sewer: Optional[List[str]] = None
    spaFeatures: Optional[str] = None
    specialListingConditions: Optional[str] = None
    stories: Optional[int] = None
    storiesDecimal: Optional[float] = None
    storiesTotal: Optional[int] = None
    structureType: Optional[str] = None
    subdivisionName: Optional[str] = None
    taxAnnualAmount: Optional[float] = None
    taxAssessedValue: Optional[float] = None
    tenantPays: Optional[str] = None
    topography: Optional[str] = None
    totalActualRent: Optional[float] = None
    utilities: Optional[List[str]] = None
    vegetation: Optional[str] = None
    view: Optional[List[str]] = None
    virtualTour: Optional[str] = None
    waterBodyName: Optional[str] = None
    waterSource: Optional[List[str]] = None
    waterView: Optional[str] = None
    waterViewYN: Optional[bool] = None
    waterfrontFeatures: Optional[str] = None
    windowFeatures: Optional[str] = None
    woodedArea: Optional[str] = None
    yearBuilt: Optional[int] = None
    yearBuiltEffective: Optional[int] = None
    zoning: Optional[str] = None
    zoningDescription: Optional[str] = None

class PropertyDetailResponse(BaseModel):
    abbreviatedAddress: Optional[str] = None
    address: Optional[PropertyAddress] = None
    bathrooms: Optional[float] = None
    bedrooms: Optional[int] = None
    city: Optional[str] = None
    homeStatus: Optional[str] = None
    homeType: Optional[str] = None
    livingArea: Optional[int] = None
    lotSize: Optional[int] = None
    price: Optional[int] = None
    zestimate: Optional[int] = None
    yearBuilt: Optional[int] = None
    zpid: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    propertyTaxRate: Optional[float] = None
    taxHistory: Optional[List[Dict[str, Any]]] = None
    priceHistory: Optional[List[Dict[str, Any]]] = None
    originalPhotos: Optional[List[OriginalPhoto]] = None
    openHouseSchedule: Optional[List[Dict[str, Any]]] = None
    daysOnZillow: Optional[int] = None

class PropertySaveResponse(BaseModel):
    property_id: str
    zpid: Optional[int] = None
    abbreviatedAddress: Optional[str] = None
    price: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    livingArea: Optional[int] = None
    yearBuilt: Optional[int] = None
    homeType: Optional[str] = None
    lotSize: Optional[int] = None
    originalPhotos: Optional[List[OriginalPhoto]] = None

class PropertyLookupRequest(BaseModel):
    address: str
    newConstructionType: Optional[str] = None
    resoFacts: Optional[ResoFacts] = None


class TaxHistoryEntry(BaseModel):
    taxIncreaseRate: Optional[float] = None
    taxPaid: Optional[float] = None
    time: Optional[int] = None
    value: Optional[int] = None
    valueIncreaseRate: Optional[float] = None


class PriceHistoryEntry(BaseModel):
    buyerAgent: Optional[str] = None
    date: Optional[str] = None
    event: Optional[str] = None
    postingIsRental: Optional[bool] = None
    price: Optional[int] = None
    priceChangeRate: Optional[float] = None
    pricePerSquareFoot: Optional[int] = None
    sellerAgent: Optional[str] = None
    showCountyLink: Optional[bool] = None
    source: Optional[str] = None
    time: Optional[int] = None


class ZillowPropertyDetailResponse(BaseModel):
    """Comprehensive model matching the complete Zillow API response structure"""
    
    # Core property information
    abbreviatedAddress: Optional[str] = None
    address: Optional[PropertyAddress] = None
    bathrooms: Optional[float] = None
    bedrooms: Optional[int] = None
    city: Optional[str] = None
    homeStatus: Optional[str] = None
    homeType: Optional[str] = None
    livingArea: Optional[int] = None
    lotSize: Optional[int] = None
    price: Optional[int] = None
    zestimate: Optional[int] = None
    yearBuilt: Optional[int] = None
    zpid: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    description: Optional[str] = None
    
    # Financial and tax information
    propertyTaxRate: Optional[float] = None
    taxHistory: Optional[List[TaxHistoryEntry]] = None
    priceHistory: Optional[List[PriceHistoryEntry]] = None
    
    # Media and schedule information
    originalPhotos: Optional[List[OriginalPhoto]] = None
    openHouseSchedule: Optional[List[Dict[str, Any]]] = None
    
    # Construction and facts
    newConstructionType: Optional[str] = None
    resoFacts: Optional[ResoFacts] = None
