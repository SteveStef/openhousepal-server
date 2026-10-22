import re

from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional
from datetime import datetime


class BrandKitResponse(BaseModel):
    primary_color: str
    secondary_color: str
    accent_color: str
    heading_font: str
    body_font: str
    personal_logo_url: Optional[str] = None
    team_logo_url: Optional[str] = None
    brokerage_logo_url: Optional[str] = None
    disclaimer_text: Optional[str] = None
    updated_at: Optional[datetime] = None
    # The brand LOOK (services/brand_look_service): {theme, shown_theme, metal, dark, accent,
    # primary, saved}. Every feature on "My Brand Kit" wears it.
    look: Optional[dict] = None

    class Config:
        from_attributes = True


class BrandLookUpdate(BaseModel):
    theme: Optional[Literal['classic', 'luxe']] = None
    metal: Optional[Literal['gold', 'silver', 'bronze']] = None
    dark: Optional[bool] = None


class BrandKitUpdate(BaseModel):
    primary_color: Optional[str] = None
    secondary_color: Optional[str] = None
    accent_color: Optional[str] = None
    heading_font: Optional[str] = None
    body_font: Optional[str] = None
    disclaimer_text: Optional[str] = Field(None, max_length=2000)
    look: Optional[BrandLookUpdate] = None

    # Brand colors go straight into designs, Spotlight pages and email-signature HTML - only a
    # real #RGB / #RRGGBB color is accepted (a typo like "zzz" used to be saved and broke them).
    @field_validator('primary_color', 'secondary_color', 'accent_color')
    @classmethod
    def _hex_color(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not re.fullmatch(r'#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})', v):
            raise ValueError('Use a color like #1F3A5F.')
        return v


class MediaUploadResponse(BaseModel):
    url: str
