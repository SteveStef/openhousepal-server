from pydantic import BaseModel
from typing import Optional, List, Literal, Any, Dict
from datetime import datetime

from app.schemas.forms import LinkedProperty

DesignStatus = Literal['DRAFT', 'READY', 'EXPORTED', 'PUBLISHED', 'ARCHIVED']
BrandingMode = Literal['personal', 'team', 'brokerage', 'none']

DesignBlockType = Literal[
    'hero_photo', 'photo_gallery', 'address', 'price', 'facts', 'open_house_details',
    'description', 'highlights', 'agent_info', 'brokerage_branding', 'qr_code', 'cta', 'headline',
]


class DesignBlockStyle(BaseModel):
    font: Optional[str] = None  # curated preset id - see Brand Kit heading/body font presets
    size: Optional[str] = None  # scale-multiplier preset, e.g. 'sm' | 'md' | 'lg' | 'xl'
    bold: Optional[bool] = None
    align: Optional[Literal['left', 'center', 'right']] = None
    color: Optional[str] = None  # brand token ('primary'|'secondary'|'accent'|'white'|'black') or a custom hex
    uppercase: Optional[bool] = None
    line_height: Optional[str] = None  # 'tight' | 'normal' | 'relaxed'


class DesignBlock(BaseModel):
    id: str
    type: DesignBlockType
    hidden: bool = False
    data: Dict[str, Any] = {}
    overrides: Dict[str, Any] = {}  # project-specific overrides of property-sourced fields
    style: Optional[DesignBlockStyle] = None  # per-block visual overrides (font/size/align/color/case)


class DesignProjectResponse(BaseModel):
    id: str
    design_type: str
    style_id: str
    template_id: Optional[str] = None
    situation: Optional[str] = None
    color_mode: Optional[str] = None
    name: str
    status: DesignStatus
    linked_property: Optional[LinkedProperty] = None
    blocks: List[DesignBlock] = []
    branding_mode: BrandingMode = 'personal'
    campaign_id: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class DesignProjectCreate(BaseModel):
    design_type: str
    style_id: str
    template_id: Optional[str] = None
    situation: Optional[str] = None
    color_mode: Optional[str] = None
    name: str
    linked_property: Optional[LinkedProperty] = None
    blocks: Optional[List[DesignBlock]] = None
    branding_mode: BrandingMode = 'personal'
    campaign_id: Optional[str] = None


class DesignProjectUpdate(BaseModel):
    name: Optional[str] = None
    linked_property: Optional[LinkedProperty] = None
    blocks: Optional[List[DesignBlock]] = None
    branding_mode: Optional[BrandingMode] = None
    style_id: Optional[str] = None  # switching visual style only - never implies a blocks/content change
    color_mode: Optional[str] = None  # switching color mode only - never implies a blocks/content change
    campaign_id: Optional[str] = None  # attaching/changing/clearing the tracked Form Campaign - also presentation-only, never implies a blocks/content change
    template_id: Optional[str] = None  # switching layout (composition) - presentation-only, content carries over


class DesignStatusUpdate(BaseModel):
    status: DesignStatus
