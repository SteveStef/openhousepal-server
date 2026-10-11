"""Runway Gen-4.5 image-to-video (the primary photo-to-video model for Studio AI Media).

Async task model: submit() returns a job id immediately; the service stores it on the asset
row (status="processing") and poll() is called later (from GET /assets/{id}) until Runway
reports SUCCEEDED/FAILED. Same httpx + env-key pattern as the OpenAI provider.
"""
import asyncio
import os
from typing import Optional

import httpx

from app.config.logging import get_logger
from .base import ProviderError, VideoPoll, VideoProvider, VideoRequest, VideoSubmit

logger = get_logger(__name__)

_BASE_URL = "https://api.dev.runwayml.com/v1"
_API_VERSION = "2024-11-06"
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3


def _raise_for_runway(resp: "httpx.Response", op: str) -> None:
    """Map a Runway non-2xx to the right ProviderError flavour. Order matters: a 429 body of
    'Your daily task limit has been reached' is NOT a transient rate-limit - it won't clear for
    hours - so it's surfaced like out_of_credits ('temporarily unavailable', don't say 'retry
    in a minute')."""
    text = (resp.text or "").lower()
    if "credit" in text or resp.status_code == 402:
        raise ProviderError("Runway account is out of credits", out_of_credits=True)
    if resp.status_code == 429 and ("daily" in text or "task limit" in text or "quota" in text):
        raise ProviderError("Runway daily task limit reached for the account", out_of_credits=True)
    if resp.status_code == 429:
        raise ProviderError("Runway is rate-limiting requests", rate_limited=True)
    raise ProviderError(f"Runway {op} returned {resp.status_code}: {resp.text[:200]}")


async def _post_with_retry(url: str, headers: dict, body: dict) -> httpx.Response:
    """POST that retries transient Runway failures (429 rate-limit / 5xx) with exponential
    backoff, honouring a Retry-After header when present. Runway 429s easily when a video fires
    one submit per photo back-to-back, or when the account is at its concurrent-task ceiling."""
    delay = 1.0
    last: httpx.Response | None = None
    for attempt in range(_MAX_RETRIES + 1):
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
        if resp.status_code not in _RETRY_STATUSES:
            return resp
        # A "daily task limit" 429 won't clear on a retry - hours away - so stop immediately.
        if resp.status_code == 429 and any(k in (resp.text or "").lower() for k in ("daily", "task limit", "quota")):
            return resp
        last = resp
        if attempt == _MAX_RETRIES:
            break
        wait = delay
        ra = resp.headers.get("retry-after")
        if ra:
            try:
                wait = min(30.0, float(ra))
            except ValueError:
                pass
        logger.warning("runway retrying", extra={"status": resp.status_code, "attempt": attempt + 1, "wait_s": wait})
        await asyncio.sleep(wait)
        delay = min(delay * 2, 15.0)
    return last  # type: ignore[return-value]

# Runway task states -> our three-state model.
_TERMINAL_OK = {"SUCCEEDED"}
_TERMINAL_FAIL = {"FAILED", "CANCELLED"}


class RunwayVideoProvider(VideoProvider):
    name = "runway"

    def __init__(self, model: str = "gen4.5"):
        self.model = model

    def _headers(self) -> dict:
        api_key = os.environ.get("RUNWAY_API_KEY")
        if not api_key:
            raise ProviderError("RUNWAY_API_KEY is not configured", not_configured=True)
        return {
            "Authorization": f"Bearer {api_key}",
            "X-Runway-Version": _API_VERSION,
            "Content-Type": "application/json",
        }

    async def submit(self, req: VideoRequest, config: Optional[dict] = None) -> VideoSubmit:
        # `config` unused here - Runway reports its own real credit cost in the submit response
        # (see below), no config-driven price needed. Accepted for interface parity with fal.
        headers = self._headers()
        body = {
            "model": self.model,
            "promptImage": req.image_url,
            # Runway hard-rejects promptText over 1000 chars with a 400 - clamp defensively.
            "promptText": (req.prompt or "")[:1000],
            "ratio": req.ratio,
            "duration": req.duration_seconds,
        }
        try:
            resp = await _post_with_retry(f"{_BASE_URL}/image_to_video", headers, body)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Runway submit failed: {exc}") from exc

        if resp.status_code >= 400:
            logger.warning("runway submit non-2xx", extra={"status": resp.status_code, "body": resp.text[:500]})
            _raise_for_runway(resp, "submit")

        body = resp.json() or {}
        job_id = body.get("id")
        if not job_id:
            raise ProviderError("Runway submit response had no task id")
        credits = ((body.get("estimatedCost") or {}).get("credits"))
        cost = round(credits * 0.01, 4) if isinstance(credits, (int, float)) else None
        return VideoSubmit(job_id=job_id, estimated_cost_usd=cost)

    async def _submit_upscale(self, path: str, body: dict, op: str) -> VideoSubmit:
        headers = self._headers()
        try:
            resp = await _post_with_retry(f"{_BASE_URL}/{path}", headers, body)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Runway {op} submit failed: {exc}") from exc
        if resp.status_code >= 400:
            logger.warning("runway %s non-2xx" % op, extra={"status": resp.status_code, "body": resp.text[:500]})
            _raise_for_runway(resp, op)
        data = resp.json() or {}
        job_id = data.get("id")
        if not job_id:
            raise ProviderError(f"Runway {op} response had no task id")
        credits = ((data.get("estimatedCost") or {}).get("credits"))
        cost = round(credits * 0.01, 4) if isinstance(credits, (int, float)) else None
        return VideoSubmit(job_id=job_id, estimated_cost_usd=cost)

    async def submit_video_upscale(
        self, video_url: str, *, model: str = "magnific_video_upscaler_creative",
        resolution: str = "4k", creativity: int = 20, flavor: str = "natural",
        config: Optional[dict] = None,  # unused - Runway has no config-driven upscale params
        duration_seconds: Optional[float] = None,  # unused - Runway/Magnific reports its own real cost
    ) -> VideoSubmit:
        """Runway/Magnific video upscaler. Input <= 30s; resolution 720p|1k|2k|4k. `creativity`
        0-100 (low = faithful, 60-80 invents plausible detail); `flavor` vivid|natural."""
        body = {
            "model": model,
            "videoUri": video_url,
            "resolution": resolution,
        }
        if model.endswith("creative"):
            body["creativity"] = max(0, min(100, int(creativity)))
            normalized_flavor = (flavor or "").strip().lower()
            if normalized_flavor in ("vivid", "natural"):
                body["flavor"] = normalized_flavor
        return await self._submit_upscale("video_upscale", body, "video upscale")

    async def submit_image_upscale(
        self, image_url: str, *, model: str = "magnific_precision_upscaler_v2",
        scale_factor: int = 2, ultra_detail: int = 30, sharpen: int = 7,
        smart_grain: int = 7, flavor: str = "photo",
    ) -> VideoSubmit:
        """Runway/Magnific still-image upscaler (Precision v2). Fixed 2026-09-05: this previously
        sent `resolution`/`strength`, which don't exist on this endpoint - confirmed via a live
        probe against Runway's own API (400 "Unrecognized keys") since studio_media_image_upscale
        _enabled defaults to False and this method had never actually been exercised for real. The
        real fields are Magnific's own native ones, camelCased by Runway's wrapper: `scaleFactor`
        (an ENUM - only 2/4/8/16 are valid, confirmed by probing the real 400 error, not an
        arbitrary float), `ultraDetail` (0-100 - "how much fine detail the AI adds... textures,
        patterns, micro-details", the actual creativity/detail-injection dial), `sharpen` (0-100
        edge definition), `smartGrain` (0-100 texture enhancement), `flavor` (sublime|photo|
        photo_denoiser - "photo" is the real-estate-appropriate one). Async like every Runway
        task - poll with poll()."""
        body = {
            "model": model,
            "imageUri": image_url,
            "scaleFactor": scale_factor if scale_factor in (2, 4, 8, 16) else 2,
            "ultraDetail": max(0, min(100, int(ultra_detail))),
            "sharpen": max(0, min(100, int(sharpen))),
            "smartGrain": max(0, min(100, int(smart_grain))),
            "flavor": flavor,
        }
        return await self._submit_upscale("image_upscale", body, "image upscale")

    # Back-compat shim for the retired opt-in button path.
    async def submit_upscale(self, video_url: str) -> VideoSubmit:
        return await self.submit_video_upscale(video_url)

    async def poll(self, job_id: str, config: Optional[dict] = None) -> VideoPoll:  # config unused - Runway reports its own cost
        headers = self._headers()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(f"{_BASE_URL}/tasks/{job_id}", headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Runway poll failed: {exc}") from exc

        if resp.status_code >= 400:
            logger.warning("runway poll non-2xx", extra={"status": resp.status_code, "body": resp.text[:500]})
            raise ProviderError(f"Runway poll returned {resp.status_code}")

        payload = resp.json() or {}
        state = (payload.get("status") or "").upper()

        if state in _TERMINAL_OK:
            outputs = payload.get("output") or []
            url = outputs[0] if outputs else None
            if not url:
                return VideoPoll(status="failed", error="Runway reported success but returned no video")
            return VideoPoll(status="succeeded", output_url=url)

        if state in _TERMINAL_FAIL:
            return VideoPoll(status="failed", error=str(payload.get("failure") or payload.get("failureCode") or state))

        # PENDING / RUNNING / THROTTLED / anything unknown -> keep waiting.
        return VideoPoll(status="processing")
