from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Union
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
    aboveGradeFinishedArea: Optional[Union[List[str], str]] = None
    accessibilityFeatures: Optional[List[str]] = None
    additionalFeeInfo: Optional[Union[List[str], str]] = None
    additionalParcelsDescription: Optional[Union[List[str], str]] = None
    appliances: Optional[List[str]] = None
    architecturalStyle: Optional[Union[List[str], str]] = None
    associationAmenities: Optional[Union[str, List[str]]] = None
    associationFee: Optional[Union[List[str], str]] = None
    associationFee2: Optional[Union[List[str], str]] = None
    associationFeeIncludes: Optional[List[str]] = None
    associationName: Optional[Union[List[str], str]] = None
    associationName2: Optional[Union[List[str], str]] = None
    associationPhone: Optional[Union[List[str], str]] = None
    associationPhone2: Optional[Union[List[str], str]] = None
    associations: Optional[List[Dict[str, Any]]] = None
    atAGlanceFacts: Optional[List[Any]] = None  # Keep as-is unless you provide AtAGlanceFact model
    attic: Optional[Union[List[str], str]] = None
    availabilityDate: Optional[Union[List[str], str, int]] = None
    basement: Optional[Union[List[str], str]] = None
    basementYN: Optional[bool] = None
    bathrooms: Optional[int] = None
    bathroomsFloat: Optional[float] = None
    bathroomsFull: Optional[int] = None
    bathroomsHalf: Optional[int] = None
    bathroomsOneQuarter: Optional[int] = None
    bathroomsPartial: Optional[int] = None
    bathroomsThreeQuarter: Optional[int] = None
    bedrooms: Optional[int] = None
    belowGradeFinishedArea: Optional[Union[List[str], str]] = None
    bodyType: Optional[Union[List[str], str]] = None
    builderModel: Optional[Union[List[str], str]] = None
    builderName: Optional[Union[List[str], str]] = None
    buildingArea: Optional[Union[List[str], str]] = None
    buildingAreaSource: Optional[Union[List[str], str]] = None
    buildingFeatures: Optional[Union[List[str], str]] = None
    buildingName: Optional[Union[List[str], str]] = None
    canRaiseHorses: Optional[bool] = None
    carportParkingCapacity: Optional[int] = None
    cityRegion: Optional[Union[List[str], str]] = None
    commonWalls: Optional[Union[List[str], str]] = None
    communityFeatures: Optional[List[str]] = None
    compensationBasedOn: Optional[Union[List[str], str]] = None
    constructionMaterials: Optional[List[str]] = None
    contingency: Optional[Union[List[str], str]] = None
    cooling: Optional[List[str]] = None
    coveredParkingCapacity: Optional[int] = None
    cropsIncludedYN: Optional[bool] = None
    cumulativeDaysOnMarket: Optional[Union[str, List[str]]] = None
    developmentStatus: Optional[Union[List[str], str]] = None
    doorFeatures: Optional[List[str]] = None
    electric: Optional[List[str]] = None
    elementarySchool: Optional[Union[List[str], str]] = None
    elementarySchoolDistrict: Optional[Union[List[str], str]] = None
    elevation: Optional[Union[List[str], str]] = None
    elevationUnits: Optional[Union[List[str], str]] = None
    entryLevel: Optional[Union[List[str], str]] = None
    entryLocation: Optional[Union[List[str], str]] = None
    exclusions: Optional[List[str]] = None
    exteriorFeatures: Optional[List[str]] = None
    feesAndDues: Optional[List[Dict[str, Any]]] = None
    fencing: Optional[Union[List[str], str]] = None
    fireplaceFeatures: Optional[List[str]] = None
    fireplaces: Optional[int] = None
    flooring: Optional[List[str]] = None
    foundationArea: Optional[Union[List[str], str]] = None
    foundationDetails: Optional[List[str]] = None
    frontageLength: Optional[Union[List[str], str]] = None
    frontageType: Optional[Union[List[str], str]] = None
    furnished: Optional[bool] = None
    garageParkingCapacity: Optional[int] = None
    gas: Optional[Union[List[str], str]] = None
    greenBuildingVerificationType: Optional[Union[List[str], str]] = None
    greenEnergyEfficient: Optional[Union[List[str], str]] = None
    greenEnergyGeneration: Optional[Union[List[str], str]] = None
    greenIndoorAirQuality: Optional[Union[List[str], str]] = None
    greenSustainability: Optional[Union[List[str], str]] = None
    greenWaterConservation: Optional[Union[List[str], str]] = None
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
    highSchool: Optional[Union[List[str], str]] = None
    highSchoolDistrict: Optional[Union[List[str], str]] = None
    hoaFee: Optional[Union[List[str], str]] = None
    hoaFeeTotal: Optional[Union[List[str], str]] = None
    homeType: Optional[Union[List[str], str]] = None
    horseAmenities: Optional[Union[str, List[str]]] = None
    horseYN: Optional[bool] = None
    inclusions: Optional[List[str]] = None
    incomeIncludes: Optional[Union[List[str], str]] = None
    interiorFeatures: Optional[List[str]] = None
    irrigationWaterRightsAcres: Optional[float] = None
    irrigationWaterRightsYN: Optional[bool] = None
    isNewConstruction: Optional[bool] = None
    isSeniorCommunity: Optional[bool] = None
    landLeaseAmount: Optional[float] = None
    landLeaseExpirationDate: Optional[Union[List[str], str]] = None
    laundryFeatures: Optional[List[str]] = None
    leaseTerm: Optional[Union[List[str], str]] = None
    levels: Optional[Union[List[str], str]] = None
    listAOR: Optional[Union[List[str], str]] = None
    listingId: Optional[Union[List[str], str]] = None
    listingTerms: Optional[Union[List[str], str]] = None
    livingArea: Optional[Union[List[str], str]] = None
    livingAreaRange: Optional[Union[List[str], str]] = None
    livingAreaRangeUnits: Optional[Union[List[str], str]] = None
    livingQuarters: Optional[List[str]] = None
    lotFeatures: Optional[List[str]] = None
    lotSize: Optional[Union[List[str], str]] = None
    lotSizeDimensions: Optional[Union[List[str], str]] = None
    mainLevelBathrooms: Optional[int] = None
    mainLevelBedrooms: Optional[int] = None
    marketingType: Optional[Union[List[str], str]] = None
    media: Optional[List[Dict[str, Any]]] = None
    middleOrJuniorSchool: Optional[Union[List[str], str]] = None
    middleOrJuniorSchoolDistrict: Optional[Union[List[str], str]] = None
    municipality: Optional[Union[List[str], str]] = None
    numberOfUnitsInCommunity: Optional[int] = None
    numberOfUnitsVacant: Optional[int] = None
    offerReviewDate: Optional[Union[List[str], str]] = None
    onMarketDate: Optional[datetime] = None
    openParkingCapacity: Optional[int] = None
    otherEquipment: Optional[Union[str, List[str]]] = None
    otherFacts: Optional[List[OtherFact]] = None
    otherParking: Optional[list[str]] = None
    otherStructures: Optional[List[str]] = None
    ownership: Optional[Union[List[str], str]] = None
    ownershipType: Optional[Union[List[str], str]] = None
    parcelNumber: Optional[Union[List[str], str]] = None
    parkName: Optional[Union[List[str], str]] = None
    parkingCapacity: Optional[int] = None
    parkingFeatures: Optional[List[str]] = None
    patioAndPorchFeatures: Optional[List[str]] = None
    petsMaxWeight: Optional[int] = None
    poolFeatures: Optional[List[str]] = None
    pricePerSquareFoot: Optional[int] = None
    propertyCondition: Optional[Union[List[str], str]] = None
    propertySubType: Optional[List[str]] = None
    roadSurfaceType: Optional[List[str]] = None
    roofType: Optional[Union[List[str], str]] = None
    roomTypes: Optional[List[str]] = None
    rooms: Optional[List[Dict[str, Any]]] = None
    securityFeatures: Optional[List[str]] = None
    sewer: Optional[List[str]] = None
    spaFeatures: Optional[Union[str, List[str]]] = None
    specialListingConditions: Optional[Union[List[str], str]] = None
    stories: Optional[int] = None
    storiesDecimal: Optional[float] = None
    storiesTotal: Optional[int] = None
    structureType: Optional[Union[List[str], str]] = None
    subdivisionName: Optional[Union[List[str], str]] = None
    taxAnnualAmount: Optional[float] = None
    taxAssessedValue: Optional[float] = None
    tenantPays: Optional[Union[List[str], str]] = None
    topography: Optional[Union[List[str], str]] = None
    totalActualRent: Optional[float] = None
    utilities: Optional[List[str]] = None
    vegetation: Optional[Union[List[str], str]] = None
    view: Optional[List[str]] = None
    virtualTour: Optional[Union[List[str], str]] = None
    waterBodyName: Optional[Union[List[str], str]] = None
    waterSource: Optional[List[str]] = None
    waterView: Optional[Union[List[str], str]] = None
    waterViewYN: Optional[bool] = None
    waterfrontFeatures: Optional[Union[str, List[str]]] = None
    windowFeatures: Optional[List[str]] = None
    woodedArea: Optional[Union[List[str], str]] = None
    yearBuilt: Optional[int] = None
    yearBuiltEffective: Optional[int] = None
    zoning: Optional[Union[List[str], str]] = None
    zoningDescription: Optional[Union[List[str], str]] = None

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
    buyerAgent: Optional[Dict[str, Any]] = None
    date: Optional[str] = None
    event: Optional[str] = None
    postingIsRental: Optional[bool] = None
    price: Optional[int] = None
    priceChangeRate: Optional[float] = None
    pricePerSquareFoot: Optional[int] = None
    sellerAgent: Optional[Dict[str, Any]] = None
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
