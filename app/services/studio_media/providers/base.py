"""Provider-agnostic request/result shapes and protocols for Studio AI Property Media.

The service (studio_media_service.py) only ever speaks in these types; each concrete provider
translates them to/from its own API. Keeps the service free of any one vendor's quirks and
makes the swap-a-model-later goal real.
"""
from dataclasses import dataclass
from typing import Optional, Protocol


class ProviderError(RuntimeError):
    """Any provider-side failure - missing API key, non-2xx, malformed response, timeout.
    The service catches this, refunds the cost reservation, and raises a generic 502/503 so
    upstream vendor details never reach the client."""

    def __init__(self, message: str, *, not_configured: bool = False, out_of_credits: bool = False,
                 rate_limited: bool = False):
        super().__init__(message)
        # True => the provider has no API key set. The service turns this into a 503
        # ("not configured yet") rather than a 502, mirroring cma_ai_service's ANTHROPIC guard.
        self.not_configured = not_configured
        # True => the provider account has run out of paid credits/balance. The service surfaces
        # a distinct "temporarily unavailable" message so the agent isn't told to "try again in
        # a moment" for something that won't fix itself until the account is topped up.
        self.out_of_credits = out_of_credits
        # True => the provider returned HTTP 429 (too many requests / too many concurrent tasks)
        # even after our own retry/backoff. The service surfaces "wait a minute and retry".
        self.rate_limited = rate_limited


# ---------------------------------------------------------------------------- image editing

@dataclass
class ImageEditRequest:
    image_bytes: bytes
    image_content_type: str  # e.g. "image/jpeg"
    prompt: str
    size: str = "auto"       # provider-specific; "auto" = infer from the source (no crop/scale)
    quality: str = "high"
    # PNG the same size as the image; fully transparent pixels = where the edit may happen.
    mask_bytes: Optional[bytes] = None


@dataclass
class ImageEditResult:
    image_bytes: bytes
    content_type: str
    # Real spend for this edit if the provider reports it directly; None => the caller derives
    # it from `usage` (below) or falls back to the configured reservation estimate.
    provider_cost_usd: Optional[float] = None
    # Raw token usage from the provider response, so the caller can bill the true cost:
    # {text_input, image_input, image_output} token counts.
    usage: Optional[dict] = None


class ImageEditProvider(Protocol):
    name: str
    model: str

    async def edit(self, req: ImageEditRequest) -> ImageEditResult: ...


# ------------------------------------------------------------------------- image-to-video

@dataclass
class VideoRequest:
    image_url: str           # publicly-fetchable source photo URL (S3 / MLS)
    prompt: str
    ratio: str = "1280:720"
    duration_seconds: int = 5


@dataclass
class VideoSubmit:
    job_id: str
    # Runway returns an exact credit cost at submit time (1 credit = $0.01). None if absent.
    estimated_cost_usd: Optional[float] = None


@dataclass
class VideoPoll:
    status: str              # "processing" | "succeeded" | "failed"
    output_url: Optional[str] = None
    provider_cost_usd: Optional[float] = None
    error: Optional[str] = None


class VideoProvider(Protocol):
    name: str
    model: str

    # `config` is the full ai_config_service config dict, passed through so a provider can price
    # itself off DB-tunable values (e.g. fal's flat per-clip fee) instead of a hardcoded constant -
    # optional/ignored by providers that don't need it (Runway/MiniMax report their own real cost).
    async def submit(self, req: VideoRequest, config: Optional[dict] = None) -> VideoSubmit: ...
    async def poll(self, job_id: str, config: Optional[dict] = None) -> VideoPoll: ...
    # ML upscale of an already-rendered video / still image. Returns a task id polled via poll(),
    # same as submit(). The URL must be publicly fetchable (stored S3 URL). Deliberately **kwargs -
    # each vendor's upscale params differ (Runway/Magnific: creativity/flavor/resolution; fal/
    # ByteDance: target_resolution/target_fps/enhancement_tier/fidelity/bit_depth) and forcing a
    # shared fixed signature would mean every new vendor fakes params it doesn't have.
    async def submit_video_upscale(self, video_url: str, **kwargs) -> VideoSubmit: ...
    async def submit_image_upscale(self, image_url: str, **kwargs) -> VideoSubmit: ...
    async def submit_upscale(self, video_url: str) -> VideoSubmit: ...
