from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime
import uuid

BcnStatus = str  # DRAFT | READY | SHARED
LoanType = Literal['conventional', 'jumbo', 'fha', 'va', 'cash', 'other']

DEFAULT_DISCLAIMER = (
    "All figures shown are estimates only. Actual loan terms, closing costs, prepaid items, and cash "
    "required at closing will be determined by the lender, title/settlement company, insurance "
    "provider, tax authorities, and the final terms of the transaction."
)


class BcnProperty(BaseModel):
    listing_key: Optional[str] = None
    source: str = 'mls'  # mls | manual
    photo_url: Optional[str] = None
    street_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    list_price: Optional[float] = None
    annual_taxes: Optional[float] = None
    hoa_fee: Optional[float] = None
    property_type: Optional[str] = None
    closing_date: Optional[str] = None


class BcnAmount(BaseModel):
    amount: Optional[float] = None


class BcnDownPaymentScenario(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    label: Optional[str] = None
    percent: Optional[float] = None


class BcnLineItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    label: str = ''
    amount: Optional[float] = None
    enabled: bool = True


class BcnCredits(BaseModel):
    seller_credit: Optional[float] = None
    lender_credit: Optional[float] = None


class BcnMonthlyContext(BaseModel):
    include: bool = False
    interest_rate: Optional[float] = None
    loan_term_years: int = 30
    homeowners_insurance_monthly: Optional[float] = None


class BuyerCashNeededResponse(BaseModel):
    id: str
    client_name: Optional[str] = None
    status: BcnStatus = 'DRAFT'
    property: BcnProperty = Field(default_factory=BcnProperty)
    purchase_price: BcnAmount = Field(default_factory=BcnAmount)
    down_payment_scenarios: List[BcnDownPaymentScenario] = []
    loan_type: LoanType = 'conventional'
    closing_costs: List[BcnLineItem] = []
    prepaid_expenses: List[BcnLineItem] = []
    inspections: List[BcnLineItem] = []
    earnest_money: BcnAmount = Field(default_factory=BcnAmount)
    credits: BcnCredits = Field(default_factory=BcnCredits)
    monthly_context: BcnMonthlyContext = Field(default_factory=BcnMonthlyContext)
    notes: Optional[str] = None
    disclaimer: Optional[str] = DEFAULT_DISCLAIMER
    share_slug: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class BuyerCashNeededListItem(BaseModel):
    id: str
    client_name: Optional[str] = None
    status: BcnStatus
    property_address: Optional[str] = None
    property_photo_url: Optional[str] = None
    purchase_price: Optional[float] = None
    share_slug: Optional[str] = None
    updated_at: Optional[datetime] = None
    created_at: datetime


class BuyerCashNeededCreate(BaseModel):
    client_name: Optional[str] = None
    property: Optional[BcnProperty] = None


class BuyerCashNeededUpdate(BaseModel):
    client_name: Optional[str] = None
    status: Optional[BcnStatus] = None
    property: Optional[BcnProperty] = None
    purchase_price: Optional[BcnAmount] = None
    down_payment_scenarios: Optional[List[BcnDownPaymentScenario]] = None
    loan_type: Optional[LoanType] = None
    closing_costs: Optional[List[BcnLineItem]] = None
    prepaid_expenses: Optional[List[BcnLineItem]] = None
    inspections: Optional[List[BcnLineItem]] = None
    earnest_money: Optional[BcnAmount] = None
    credits: Optional[BcnCredits] = None
    monthly_context: Optional[BcnMonthlyContext] = None
    notes: Optional[str] = None
    disclaimer: Optional[str] = None


class BcnShareResponse(BaseModel):
    share_slug: str
    status: BcnStatus
    public_path: str


class BcnPublicAgent(BaseModel):
    name: str
    headshot_url: Optional[str] = None
    brokerage: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    logo_url: Optional[str] = None
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    agent_hub_slug: Optional[str] = None


class BuyerCashNeededPublicResponse(BaseModel):
    client_name: Optional[str] = None
    property: BcnProperty
    purchase_price: BcnAmount
    down_payment_scenarios: List[BcnDownPaymentScenario]
    loan_type: LoanType
    closing_costs: List[BcnLineItem]
    prepaid_expenses: List[BcnLineItem]
    inspections: List[BcnLineItem]
    earnest_money: BcnAmount
    credits: BcnCredits
    monthly_context: BcnMonthlyContext
    notes: Optional[str] = None
    disclaimer: Optional[str] = None
    agent: BcnPublicAgent
    updated_at: Optional[datetime] = None
