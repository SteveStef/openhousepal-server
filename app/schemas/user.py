from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Optional
from datetime import datetime

class UserBase(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str
    state: str
    brokerage: str
    mls_id: Optional[str] = None  # None = agent outside Bright MLS
    license_number: Optional[str] = None
    broker_authorized: bool = False
    photo_url: Optional[str] = None
    professional_title: Optional[str] = None
    market: Optional[str] = None

def _clean_email(v):
    # Emails match case-insensitively everywhere: phones capitalize the first letter, and
    # "John@x.com" / "john@x.com" must be the same account, never two.
    return v.strip().lower() if isinstance(v, str) else v


class UserCreate(UserBase):
    password: str = Field(min_length=8)
    # False = an agent outside Bright MLS: no MLS ID, a license number instead, No-MLS plans only.
    is_bright: bool = True
    # An admin test-account code (from the sign-up link): lets a Bright sign-up take the MLS ID of
    # the agent the code copies. Ignored outside Bright.
    signup_code: Optional[str] = None

    _email = field_validator("email", mode="before")(_clean_email)

    @model_validator(mode="after")
    def _id_for_audience(self):
        self.first_name, self.last_name = self.first_name.strip(), self.last_name.strip()
        self.brokerage = self.brokerage.strip()
        if not (self.first_name and self.last_name and self.brokerage):
            raise ValueError("Name and brokerage are required")
        if self.is_bright:
            self.mls_id = (self.mls_id or "").strip()
            if not self.mls_id:
                raise ValueError("MLS ID is required")
            self.license_number = None
        else:
            self.license_number = (self.license_number or "").strip()
            if not self.license_number or len(self.license_number) > 40:
                raise ValueError("License number is required")
            self.mls_id = None  # never stored in the MLS ID slot - it could match a real Bright ID
            self.signup_code = None
        self.signup_code = (self.signup_code or "").strip() or None
        return self

class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    state: Optional[str] = None
    brokerage: Optional[str] = None
    mls_id: Optional[str] = None
    password: Optional[str] = None

class User(UserBase):
    id: str
    is_admin: bool = False
    created_at: datetime
    updated_at: Optional[datetime] = None

    # PayPal subscription fields
    subscription_id: Optional[str] = None
    subscription_status: Optional[str] = None
    broker_authorized: bool = False
    plan_id: Optional[str] = None
    plan_tier: Optional[str] = None  # BASIC or PREMIUM
    trial_ends_at: Optional[datetime] = None
    idx_setup_settled_at: Optional[datetime] = None
    subscription_started_at: Optional[datetime] = None
    last_billing_date: Optional[datetime] = None
    next_billing_date: Optional[datetime] = None  # For grace period after cancellation
    restrict_host_requests_to_team: bool = False
    idx_domain_migration_agreed_at: Optional[datetime] = None  # null = IDX agent hasn't seen/agreed to the domain-migration prompt yet
    billing_group_id: Optional[str] = None  # paid for by a brokerage group (admin)
    test_copy_of_user_id: Optional[str] = None  # an admin-made test copy of this agent (shares their MLS ID)
    last_seen_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class UserLogin(BaseModel):
    email: EmailStr
    password: str

    _email = field_validator("email", mode="before")(_clean_email)

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    email: Optional[str] = None

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

    _email = field_validator("email", mode="before")(_clean_email)

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


class ChangePasswordRequest(BaseModel):
    """Authenticated in-app password change - the user proves it's them by re-entering the
    current password (there is no email round-trip and no 2FA in the system)."""
    current_password: str
    new_password: str = Field(min_length=8)
