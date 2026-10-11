from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
import uuid

SnsStatus = str  # DRAFT | READY | SHARED

DEFAULT_DISCLAIMER = (
    "This Seller Net Sheet provides an estimate only. Final proceeds depend on the actual mortgage "
    "payoff amount, settlement charges, taxes, commissions/compensation, prorations, concessions, and "
    "other closing costs determined at settlement. It is not a guaranteed settlement statement."
)


class SnsProperty(BaseModel):
    listing_key: Optional[str] = None
    source: str = 'mls'  # mls | manual
    photo_url: Optional[str] = None
    street_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    list_price: Optional[float] = None
    previous_sale_price: Optional[float] = None
    annual_taxes: Optional[float] = None
    property_type: Optional[str] = None
    closing_date: Optional[str] = None


class SnsScenario(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    label: Optional[str] = None
    sale_price: Optional[float] = None


class SnsLineItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    label: str = ''
    amount: Optional[float] = None


class SnsCompensationSide(BaseModel):
    percent: Optional[float] = None
    flat_amount: Optional[float] = None


class SnsCompensation(BaseModel):
    listing_side: SnsCompensationSide = Field(default_factory=SnsCompensationSide)
    buyer_side: SnsCompensationSide = Field(default_factory=SnsCompensationSide)


class SnsTaxProration(BaseModel):
    amount: Optional[float] = None


class SellerNetSheetResponse(BaseModel):
    id: str
    client_name: Optional[str] = None
    status: SnsStatus = 'DRAFT'
    property: SnsProperty = Field(default_factory=SnsProperty)
    scenarios: List[SnsScenario] = []
    mortgage_payoffs: List[SnsLineItem] = []
    compensation: SnsCompensation = Field(default_factory=SnsCompensation)
    closing_costs: List[SnsLineItem] = []
    tax_proration: SnsTaxProration = Field(default_factory=SnsTaxProration)
    repairs: List[SnsLineItem] = []
    notes: Optional[str] = None
    disclaimer: Optional[str] = DEFAULT_DISCLAIMER
    share_slug: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SellerNetSheetListItem(BaseModel):
    id: str
    client_name: Optional[str] = None
    status: SnsStatus
    property_address: Optional[str] = None
    property_photo_url: Optional[str] = None
    scenario_count: int = 0
    top_sale_price: Optional[float] = None
    share_slug: Optional[str] = None
    updated_at: Optional[datetime] = None
    created_at: datetime


class SellerNetSheetCreate(BaseModel):
    client_name: Optional[str] = None
    property: Optional[SnsProperty] = None


class SellerNetSheetUpdate(BaseModel):
    client_name: Optional[str] = None
    status: Optional[SnsStatus] = None
    property: Optional[SnsProperty] = None
    scenarios: Optional[List[SnsScenario]] = None
    mortgage_payoffs: Optional[List[SnsLineItem]] = None
    compensation: Optional[SnsCompensation] = None
    closing_costs: Optional[List[SnsLineItem]] = None
    tax_proration: Optional[SnsTaxProration] = None
    repairs: Optional[List[SnsLineItem]] = None
    notes: Optional[str] = None
    disclaimer: Optional[str] = None


class SnsShareResponse(BaseModel):
    share_slug: str
    status: SnsStatus
    public_path: str


class SnsPublicAgent(BaseModel):
    name: str
    headshot_url: Optional[str] = None
    brokerage: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    agent_hub_slug: Optional[str] = None


class SellerNetSheetPublicResponse(BaseModel):
    client_name: Optional[str] = None
    property: SnsProperty
    scenarios: List[SnsScenario]
    mortgage_payoffs: List[SnsLineItem]
    compensation: SnsCompensation
    closing_costs: List[SnsLineItem]
    tax_proration: SnsTaxProration
    repairs: List[SnsLineItem]
    notes: Optional[str] = None
    disclaimer: Optional[str] = None
    agent: SnsPublicAgent
    updated_at: Optional[datetime] = None
