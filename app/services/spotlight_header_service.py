"""Spotlight "living photo" header: one AI clip of the agent's main photo, turned into a seamless
loop for the top of a Listing Spotlight page.

The clip itself is an ordinary AI Media video asset (studio_media_service.create_asset), so it
lands in the agent's AI Media library, is billed on the same monthly AI allowance and can be
reused by any other Spotlight site for the same home. This module only adds:

- the header-specific request: one photo through the exact same pipeline as any Media Lab video
  (Hailuo render + the ByteDance 2K finish), with the slowest forward push-in and an extra line
  asking for the light to turn from day to dusk while it moves,
- the cost estimate shown before the agent says yes, and
- make_loop(): the finished clip played forward then backward ("boomerang") so it repeats with
  no visible jump - a slow camera push reads naturally in reverse, a hard cut back to frame one
  never does. Built with the same bundled ffmpeg the video pipeline already uses.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import httpx

from app.config.logging import get_logger
from app.services import s3_service

logger = get_logger(__name__)

# One style. "glide" (camera move only) and a separate Runway "dusk" timelapse were dropped
# 2026-09-30: the Runway timelapse ignored "camera still" (gates swung, the house morphed) and
# billed ~$4.10 with its 4K upscale. Every header is now a normal Media Lab clip - slow push-in +
# the light turning to dusk - at the normal Media Lab price (~$0.41).
HEADER_STYLES = ("dusk",)

# PAUSED 2026-09-30 ("Coming soon" in the editor): the agent judged the Hailuo 1080p/2K clips not
# detailed enough for a luxury header, and the push-in too jumpy once looped. Everything below
# still works - flip this on to bring it back. Ideas for when it returns: a much slower move (the
# suffix below is already rewritten for that), playing the loop at reduced speed, and a detail
# pass (Topaz Starlight ~$0.12/s, i.e. ~$0.70 for one ~6s clip, vs Runway's 4K upscale at ~$2.90
# for 10s that was far too expensive for a full 30s reel).
LIVING_PHOTO_ENABLED = False

# Appended to the normal video prompt (StudioVideoVariantOptions.prompt_suffix, max 500 chars).
HEADER_PROMPT_SUFFIX = (
    "Looping website header. The camera is almost still: an extremely slow, smooth push-in, barely "
    "perceptible, never a zoom - no rotation, shake, speed changes or cuts. Over the clip the "
    "daylight gently turns to dusk: the sky warms to sunset then soft blue twilight, and windows "
    "already in the photo glow warm - never add lights. Only subtle natural motion: drifting "
    "clouds, softly moving leaves. No people or cars."
)

def header_suffix(note: str | None = None) -> str:
    """The header suffix, plus the agent's own fix-it note from a remake ("keep the gate still").
    Fits StudioVideoVariantOptions.prompt_suffix's 500-char cap."""
    note = " ".join((note or "").split())[:110].strip()
    if not note:
        return HEADER_PROMPT_SUFFIX
    return f"{HEADER_PROMPT_SUFFIX} Also: {note}"[:500]


_S3_PREFIX = "spotlight-headers"
# Every loop gets a brand-new key, so it never changes - let the CDN edge and each visitor's
# browser keep it for a year (repeat visits and later visitors are served from cache).
_CACHE = "public, max-age=31536000, immutable"
_CHUNK_SECONDS = 1.0  # reverse() buffers every frame it's given - reverse in 1s pieces


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(cmd: list[str], timeout: int = 180) -> None:
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "ignore")[-500:])


def _duration(path: str) -> float:
    r = subprocess.run([_ffmpeg(), "-hide_banner", "-i", path], capture_output=True, timeout=20)
    text = r.stderr.decode("utf-8", "ignore")
    import re
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not m:
        raise RuntimeError("Could not read the clip length.")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def build_boomerang(src: str, out: str, *, width: int = 1600, crf: int = 25) -> None:
    """src (any mp4) -> out: forward then reversed, one silent H.264 file, web-ready."""
    work = tempfile.mkdtemp(prefix="hdr_loop_")
    try:
        # Capped bitrate: a detailed aerial at plain CRF came out ~10MB for 12s - too heavy for a
        # page header on a phone. ~3Mbps keeps it sharp at 1600px and around 4MB.
        enc = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-maxrate", "3000k", "-bufsize", "6000k",
               "-pix_fmt", "yuv420p", "-r", "24"]
        scale = f"scale='min({width},iw)':-2"
        fwd = os.path.join(work, "fwd.mp4")
        _run([_ffmpeg(), "-y", "-i", src, "-vf", scale, *enc, fwd])
        total = _duration(fwd)
        # Reverse piece by piece (last second first) so memory stays small at 1080p.
        pieces: list[str] = []
        start = 0.0
        idx = 0
        while start < total - 0.05:
            piece = os.path.join(work, f"rev_{idx:03d}.mp4")
            _run([_ffmpeg(), "-y", "-ss", f"{start:.3f}", "-t", f"{_CHUNK_SECONDS:.3f}", "-i", fwd,
                  "-vf", "reverse", *enc, piece])
            pieces.append(piece)
            start += _CHUNK_SECONDS
            idx += 1
        listing = os.path.join(work, "list.txt")
        with open(listing, "w", encoding="utf-8") as f:
            f.write(f"file '{fwd}'\n")
            for piece in reversed(pieces):
                f.write(f"file '{piece}'\n")
        _run([_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", "-movflags", "+faststart", out])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def poster_frame(src: str, out: str) -> None:
    _run([_ffmpeg(), "-y", "-ss", "0.05", "-i", src, "-frames:v", "1", "-q:v", "3", out], timeout=30)


async def make_loop(clip_url: str) -> dict:
    """Download the finished AI clip, build the loop + a poster, upload both. Returns
    {"url", "poster"}. No AI cost - plain video processing."""
    work = tempfile.mkdtemp(prefix="hdr_src_")
    try:
        src = os.path.join(work, "clip.mp4")
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            r = await client.get(clip_url)
            r.raise_for_status()
            with open(src, "wb") as f:
                f.write(r.content)
        out = os.path.join(work, "loop.mp4")
        poster = os.path.join(work, "poster.jpg")
        import asyncio
        await asyncio.to_thread(build_boomerang, src, out)
        await asyncio.to_thread(poster_frame, out, poster)
        with open(out, "rb") as f:
            url = s3_service.upload_bytes(f.read(), _S3_PREFIX, ".mp4", "video/mp4", cache_control=_CACHE)
        with open(poster, "rb") as f:
            poster_url = s3_service.upload_bytes(f.read(), _S3_PREFIX, ".jpg", "image/jpeg", cache_control=_CACHE)
        return {"url": url, "poster": poster_url}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def estimate(config: dict, plan_tier: str) -> dict:
    """Up-front cost of the header clip, in dollars and as a share of the agent's monthly AI
    allowance (the only number agents ever see). Same projection as any 1-photo Media Lab video."""
    from app.services.studio_media_service import estimate_video_generation_cost_usd
    limit = config.get("agent_monthly_ai_budget_usd_by_tier", {}).get(plan_tier)
    out = {}
    for style in HEADER_STYLES:
        usd = float(estimate_video_generation_cost_usd(config, photo_count=1)["total_usd"])
        pct = None
        if limit:
            pct = max(1, int(-(-usd * 100 // float(limit))))  # round up - never under-promise
        out[style] = {"usd": round(usd, 2), "percent": pct}
    return {"styles": out, "limit_usd": limit}
