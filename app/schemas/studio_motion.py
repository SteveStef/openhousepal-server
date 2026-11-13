"""AI Media Cinematic Video requests (see app/services/studio_motion_service.py)."""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.forms import LinkedProperty

MotionFormat = Literal["16:9", "9:16", "1:1", "4:5"]


class MotionCreateRequest(BaseModel):
    # Every photo the video may use (all of a listing's, or the agent's uploads) - the agent picks
    # the length and the rooms in the editor, and the video's photos are chosen from these.
    photos: List[str] = Field(..., min_length=1, max_length=200)  # the service keeps the first 40
    source_kind: Literal["listing", "upload"] = "listing"
    linked_property: Optional[LinkedProperty] = None
    # The agent's own details for the badge / closing card (from their profile, editable later).
    agent: Optional[Dict[str, Any]] = None
    client_request_id: Optional[str] = Field(None, max_length=80)


class MotionUpdateRequest(BaseModel):
    """Autosave: the editor's whole settings object (photos, look, words, sections...)."""
    settings: Dict[str, Any]


class MotionAddPhotosRequest(BaseModel):
    photos: List[str] = Field(..., min_length=1, max_length=15)


class MotionRenderRequest(BaseModel):
    sharpen: Optional[bool] = None
    # Another shape of the same video (made from the same settings, kept beside the main one).
    format: Optional[MotionFormat] = None


class MotionEstimateResponse(BaseModel):
    photos: int
    analyze_usd: float
    sharpen_usd: float
    analyze_percent: Optional[float] = None
    sharpen_percent: Optional[float] = None
    month_percent: Optional[float] = None
    soft_capped: bool = False
    sharpen_paused: bool = False
