from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.forms import LinkedProperty

ImageEffect = Literal["twilight", "lights_on", "virtual_staging", "enhance"]
# "stack" = a photo edit driven by `effects`; "animate" = the multi-photo AI video.
StudioMediaAction = Literal["twilight", "lights_on", "virtual_staging", "enhance", "stack", "animate"]
StudioMediaKind = Literal["image", "video", "upload"]
StudioMediaStatus = Literal["processing", "succeeded", "failed"]
StudioMediaSourceKind = Literal["listing", "upload"]

RoomType = Literal[
    "living_room", "family_room", "primary_bedroom", "bedroom", "nursery", "kitchen",
    "dining_room", "office", "basement", "bonus_room", "home_gym", "sunroom",
    "entryway", "patio",
]
StagingStyle = Literal["modern", "transitional", "luxury", "traditional"]
Enhancement = Literal["grass", "sky", "brighten_exterior", "lighting", "declutter", "polish", "yard_cleanup", "remove_furniture"]
StagingTagPosition = Literal["bottom-right", "bottom-middle", "off"]
VideoVariantModelTier = Literal["fast_pro", "pro"]
VideoVariantMotion = Literal["push_in", "slow_push_in", "pan_left", "pan_right"]
# "standard" = the existing camera-motion clip (push_in etc.). "day_to_twilight" = a still-camera
# clip where only the sky/light transitions from day to dusk - see build_video_timelapse_prompt.
VideoStyle = Literal["standard", "day_to_twilight"]


class StudioVideoVariantOptions(BaseModel):
    """Optional per-job test knobs for AI Video. Normal production jobs omit this and keep the
    configured default model/motion/prompt exactly as-is."""
    label: Optional[str] = None
    model_tier: Optional[VideoVariantModelTier] = None
    motion: Optional[VideoVariantMotion] = None
    prompt_suffix: Optional[str] = Field(None, max_length=500)
    max_cost_per_clip_usd: Optional[float] = Field(None, gt=0)


class StudioMediaOptions(BaseModel):
    """Loose options bag. Virtual Staging uses room_type + style; Enhance uses enhancements[].
    Twilight / Lights On take none. AI Video picks its camera moves at random (video_style
    "standard", the default) unless video_style is "day_to_twilight", or video_variant is
    supplied for controlled model/motion/prompt tests."""
    room_type: Optional[RoomType] = None
    style: Optional[StagingStyle] = None
    enhancements: Optional[List[Enhancement]] = None
    video_style: Optional[VideoStyle] = None
    video_variant: Optional[StudioVideoVariantOptions] = None
    # "Virtually Staged" export tag, offered under the Virtual Stage effect - only ever burned
    # into the downloaded copy (see apply_staging_tag), never the stored master. Off by default;
    # the agent opts in per-project and picks where the pill sits.
    staging_tag: Optional[StagingTagPosition] = None
    # "Circle what to remove" (Enhance -> Remove cars / Remove furniture): each shape is a list
    # of [x, y] points as fractions of the photo's width/height (0-1). When set, removal edits
    # ONLY inside these shapes and the original photo is pasted back everywhere else (see
    # studio_media_service._masked_removal_pass).
    remove_areas: Optional[List[List[List[float]]]] = None

    @field_validator("remove_areas")
    @classmethod
    def _clean_areas(cls, v):
        if not v:
            return None
        shapes = []
        for shape in v[:12]:
            pts = []
            for pt in (shape or [])[:300]:
                if isinstance(pt, (list, tuple)) and len(pt) == 2:
                    try:
                        pts.append([min(1.0, max(0.0, float(pt[0]))), min(1.0, max(0.0, float(pt[1])))])
                    except (TypeError, ValueError):
                        continue
            if len(pts) >= 3:
                shapes.append(pts)
        return shapes or None


class StudioMediaGenerateRequest(BaseModel):
    action: StudioMediaAction
    source_photo_url: str = Field(..., min_length=1)
    source_kind: StudioMediaSourceKind
    linked_property: Optional[LinkedProperty] = None
    options: StudioMediaOptions = StudioMediaOptions()
    # Photo edit: one or more effects, applied as a SINGLE stacked gpt-image-2 call.
    effects: Optional[List[ImageEffect]] = None
    # AI Video: up to 8 source photos, each becomes a 5s clip; concatenated server-side.
    photos: Optional[List[str]] = None


class StudioMediaRegenerateRequest(BaseModel):
    options: Optional[StudioMediaOptions] = None  # omitted => reuse the parent's options
    effects: Optional[List[ImageEffect]] = None  # omitted => reuse the parent's own effect(s)


class StudioMediaRenameRequest(BaseModel):
    name: str = Field("", max_length=120)


class StudioMediaStagingTagRequest(BaseModel):
    position: StagingTagPosition


TitleCardPreset = Literal["just_listed", "open_house", "coming_soon", "price_improved", "under_contract", "just_sold", "back_on_market", "new_construction", "custom"]
TitleCardPosition = Literal["top", "middle", "bottom"]
BrandingPosition = Literal["top-left", "bottom-left", "top-middle"]


class TitleCardManualFields(BaseModel):
    """Free-typed property fields for a title card on an upload-sourced video (no linked_property
    to pull real listing data from) - or to override a linked listing's real values."""
    address: Optional[str] = Field(None, max_length=200)
    location: Optional[str] = Field(None, max_length=120)
    price: Optional[str] = Field(None, max_length=40)
    beds: Optional[str] = Field(None, max_length=10)
    baths: Optional[str] = Field(None, max_length=10)
    sqft: Optional[str] = Field(None, max_length=20)


class LayerTiming(BaseModel):
    """One video layer's time on screen. end None = to the end of the video; exit = animate away
    or cut instantly (an edit-style cut, usually on a clip change)."""
    start: float = Field(0, ge=0, le=900)
    end: Optional[float] = Field(None, ge=0, le=900)
    exit: Literal["animate", "cut"] = "animate"


class OutroTiming(BaseModel):
    start: float = Field(0, ge=0, le=900)


class OverlayTiming(BaseModel):
    title: Optional[LayerTiming] = None
    tag: Optional[LayerTiming] = None
    brand: Optional[LayerTiming] = None
    outro: Optional[OutroTiming] = None


class TitleCardOptions(BaseModel):
    """The 0-5s intro title card overlay - preset + editable title + which property stats show.
    Burned in (Pillow + ffmpeg, see studio_media_service.export_video_bytes) only at export time;
    never touches the stored master render."""
    enabled: bool = False
    preset: TitleCardPreset = "just_listed"
    title: str = Field("Just Listed", max_length=60)
    show_address: bool = True
    show_location: bool = True
    show_price: bool = False
    show_beds: bool = True
    show_baths: bool = True
    show_sqft: bool = True
    manual: TitleCardManualFields = TitleCardManualFields()
    position: TitleCardPosition = "bottom"
    # Animated designs, drawn + exported in the browser (frontend lib/studio/videoMotion.ts).
    style: Literal["default", "luxe"] = "default"
    kicker: Optional[str] = Field(None, max_length=60)
    event_line: Optional[str] = Field(None, max_length=60)
    corner_tag: bool = False
    end_card: bool = False
    end_cta: Optional[str] = Field(None, max_length=60)
    # Signature: the agent's own color; Luxe: gold / silver / bronze.
    accent: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    metal: Literal["gold", "silver", "bronze"] = "gold"
    # When each layer shows (seconds into the video); unset = the frontend's defaults.
    timing: Optional[OverlayTiming] = None


class BrandingOptions(BaseModel):
    """Agent branding corner badge - shown for the whole clip, independent of the title card.
    Only a toggle + a placement: the logo/name/brokerage are always pulled live from the agent's
    current StudioBrandKit + User row at export time, never snapshotted here. `position` is its
    own field, fully independent of TitleCardOptions.position - the two used to be coupled (the
    badge auto-moved to avoid the title card), which meant picking a title-card position silently
    moved the badge too. Fixed 2026-09-06."""
    enabled: bool = False
    position: BrandingPosition = "top-left"


class VideoOverlayUpdateRequest(BaseModel):
    """PATCH body for /assets/{id}/overlay - merges into asset.options, autosaved from the editor
    as the agent edits it so it survives a reload without a full regenerate."""
    title_card: Optional[TitleCardOptions] = None
    branding: Optional[BrandingOptions] = None


class VideoExportRequest(BaseModel):
    """POST body for /assets/{id}/export. `ratio` omitted = the 16:9 master. Both overlays
    optional/independent; omitting both just delegates to the same export as plain /download."""
    ratio: Optional[str] = None
    title_card: Optional[TitleCardOptions] = None
    branding: Optional[BrandingOptions] = None


class StudioMediaAssetResponse(BaseModel):
    id: str
    group_id: str
    parent_asset_id: Optional[str] = None
    kind: StudioMediaKind
    action: str  # a single effect, a "+"-joined stack ("virtual_staging+enhance"), or "animate"
    status: StudioMediaStatus
    source_photo_url: str
    source_kind: StudioMediaSourceKind
    linked_property: Optional[LinkedProperty] = None
    options: Optional[Dict[str, Any]] = None
    provider: Optional[str] = None
    provider_model: Optional[str] = None
    result_url: Optional[str] = None
    error_message: Optional[str] = None
    cost_usd: Optional[float] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class StudioMediaAssetListResponse(BaseModel):
    assets: List[StudioMediaAssetResponse]


class StudioVideoTestGridRequest(BaseModel):
    source_photo_url: str = Field(..., min_length=1)
    source_kind: StudioMediaSourceKind = "listing"
    linked_property: Optional[LinkedProperty] = None
    model_tiers: List[VideoVariantModelTier] = Field(default_factory=lambda: ["fast_pro", "pro"])
    motions: List[VideoVariantMotion] = Field(default_factory=lambda: ["push_in", "slow_push_in", "pan_left"])
    prompt_suffixes: List[str] = Field(default_factory=lambda: ["", "Keep foliage, leaves, branches, grass, roof texture, brick, stone, siding and other fine real-estate textures crisp, sharp, stable and highly detailed throughout the clip. Avoid soft focus, smeared leaves, mushy trees, motion blur, painterly texture, or temporal detail loss."])
    max_cost_per_clip_usd: float = Field(0.65, gt=0)
    include_over_budget: bool = False


class StudioVideoTestGridVariant(BaseModel):
    label: str
    model_tier: VideoVariantModelTier
    motion: VideoVariantMotion
    prompt_suffix: str
    projected_cost_usd: float
    projected_over_budget: bool
    skipped: bool = False
    asset: Optional[StudioMediaAssetResponse] = None


class StudioVideoTestGridResponse(BaseModel):
    group_id: str
    max_cost_per_clip_usd: float
    variants: List[StudioVideoTestGridVariant]


class StudioMediaUsageResponse(BaseModel):
    """This agent's own AI Media spend, for the in-page "you're testing all day" cost readout.
    Costs are the reconciled estimated_cost_usd on successful generations only.

    The `*_budget_*` / `*_est_usd` fields power the "this will use ~N% of your monthly AI
    usage" note shown before a generation. `month_ai_used_usd` is spend across EVERY AI tool
    combined (not just Media), matching the Monthly AI Usage meter. All are null for a tier
    with no configured monthly budget."""
    today_cost_usd: float
    month_cost_usd: float
    today_count: int
    month_count: int
    month_ai_used_usd: float | None = None
    month_ai_budget_usd: float | None = None
    month_ai_percent: int | None = None
    video_est_usd: float | None = None
    image_est_usd: float | None = None
    # Real, provider-aware per-unit prices (never hardcoded on the frontend) so the picker can
    # compute "this video will use ~N% of your monthly allowance" live as the agent adds/removes
    # photos, before ever clicking Generate. video_clip_price_usd covers one rendered clip;
    # video_upscale_price_per_second_usd covers the one 4K upscale pass on the finished video
    # (0 for a provider whose render is already native 4K/2K with no separate upscale step).
    video_clip_price_usd: float | None = None
    video_upscale_price_per_second_usd: float | None = None
    video_seconds_per_clip: int | None = None
    # Per-photo light-enhancement pass (Magnific Precision v2) run before Hailuo ever renders it -
    # 0 when studio_media_video_photo_enhance_enabled is off, so a live estimate always reflects
    # whether this step will actually run.
    video_photo_enhance_price_usd: float | None = None
    # True once this agent is at/over the soft cap where the expensive AI Studio tools stop but
    # cheaper tools (CMA, cheat sheets, chat) keep working.
    studio_soft_capped: bool = False
    # Per-agent AI Studio Media storage: bytes stored vs the hard cap. At/over cap, new
    # generations are blocked until the agent deletes some assets.
    storage_used_bytes: int = 0
    storage_cap_bytes: int = 0
