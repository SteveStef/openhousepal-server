from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime

class UserBase(BaseModel):
    email: EmailStr
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    state: Optional[str] = None
    brokerage: Optional[str] = None

class UserCreate(UserBase):
    password: str

class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    state: Optional[str] = None
    brokerage: Optional[str] = None
    password: Optional[str] = None

class User(UserBase):
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    # PayPal subscription fields
    subscription_id: Optional[str] = None
    subscription_status: Optional[str] = None
    plan_id: Optional[str] = None
    plan_tier: Optional[str] = None  # BASIC or PREMIUM
    trial_ends_at: Optional[datetime] = None
    subscription_started_at: Optional[datetime] = None
    last_billing_date: Optional[datetime] = None

    class Config:
        from_attributes = True

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    email: Optional[str] = None
