"""AI Media Cinematic Video requests (see app/services/studio_motion_service.py)."""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.forms import LinkedProperty

MotionFormat = Literal["16:9", "9:16", "1:1", "4:5"]


class MotionCreateRequest(BaseModel):
    # Every photo the video may use (all of a listing's, or the agent's uploads) - the agent picks
    # the length and the rooms in the editor, and the video's photos are chosen from these.
    photos: List[str] = Field(..., min_length=1, max_length=200)  # the service keeps the first 100 (POOL_MAX)
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
    # Overnight mode (11/14): made on our own video maker in the quiet hours - no AI for the render.
    # False on a video already scheduled = "make it now instead".
    overnight: Optional[bool] = None


class MotionEstimateResponse(BaseModel):
    photos: int
    analyze_usd: float
    sharpen_usd: float
    analyze_percent: Optional[float] = None
    sharpen_percent: Optional[float] = None
    month_percent: Optional[float] = None
    soft_capped: bool = False
    sharpen_paused: bool = False
    # making the video itself (AWS Lambda, owner 11/13) - 0 / False on the old self-hosted worker
    render_usd: float = 0.0
    render_percent: Optional[float] = None
    render_paid: bool = False
    render_paused: bool = False
    # the cheapest it can be (a discounted Amazon machine) - render_usd is the most (Lambda)
    render_usd_low: float = 0.0
    render_percent_low: Optional[float] = None
    # Fast Cut only: the AI editor's one look at the photos (once per set of photos)
    fast_edit_usd: float = 0.0
    fast_edit_percent: Optional[float] = None
    # Save usage / Make it now: the AI editor's look + making the video (photo preparation is the same either way)
    speed_save_usd: float = 0.0
    speed_save_usd_max: float = 0.0
    speed_now_usd: float = 0.0
    speed_save_percent: Optional[float] = None
    speed_save_percent_max: Optional[float] = None
    speed_now_percent: Optional[float] = None
    speed_save_cap_minutes: int = 12
    # when it would be ready if started now, the line ahead included (11/17)
    speed_save_ready_minutes: Optional[int] = None
    speed_now_ready_minutes: Optional[int] = None
    speed_save_ahead: int = 0
    speed_now_ahead: int = 0
    overnight_available: bool = False
    overnight_ready_at: Optional[str] = None
    overnight_held: bool = False
    overnight_saves_usd: float = 0.0
    overnight_saves_percent: Optional[float] = None
