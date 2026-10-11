"""Resolve an image / video provider by the name configured in ai_config_service DEFAULT_CONFIG.

Image: only "openai" exists today. Video: "fal" (Hailuo 2.3 Pro + ByteDance upscale, the default
as of 2026-09 - see fal_video.py), "runway" (gen4_turbo + optional Magnific) and "minimax"
(Hailuo H3, native 2K) both still work as available-but-not-default alternatives. To trial another
model later (e.g. Google Veo), add its module under providers/ and one entry here - the service
and routes don't change.
"""
from .base import ImageEditProvider, VideoProvider
from .fal_video import FalVideoProvider
from .minimax_video import MiniMaxVideoProvider
from .openai_image import OpenAIImageProvider
from .runway_video import RunwayVideoProvider


def image_provider(name: str, model: str) -> ImageEditProvider:
    if name == "openai":
        return OpenAIImageProvider(model=model)
    raise ValueError(f"Unknown image provider: {name!r}")


def video_provider(name: str, model: str) -> VideoProvider:
    if name == "fal":
        return FalVideoProvider(model=model)
    if name == "runway":
        return RunwayVideoProvider(model=model)
    if name == "minimax":
        return MiniMaxVideoProvider(model=model)
    raise ValueError(f"Unknown video provider: {name!r}")
