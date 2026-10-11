"""MiniMax Hailuo (H3) image-to-video - an alternative to Runway for the photo-to-video render
step, being trialled for cost/quality against gen4_turbo (see studio_media_service._run_video).

Async task model like Runway's: submit() POSTs to the v2 create endpoint and returns a task_id
immediately; poll() is called later until MiniMax reports the task succeeded/failed. Unlike
Runway, MiniMax never reports a cost estimate at submit time - the real cost (from `usage.
total_seconds` once the job finishes) comes back through poll()'s `provider_cost_usd`, which
studio_media_service.poll_asset trues up onto the clip's stored cost.

Docs: https://platform.minimax.io/docs/api-reference/video-generation-v2-create
"""
import asyncio
import os
from typing import Optional

import httpx

from app.config.logging import get_logger
from .base import ProviderError, VideoPoll, VideoProvider, VideoRequest, VideoSubmit

logger = get_logger(__name__)

_BASE_URL = "https://api.minimax.io/v2"
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3

_TERMINAL_OK = {"succeeded"}
_TERMINAL_FAIL = {"failed", "cancelled"}

# $/second of OUTPUT video at "2K" (the only resolution this integration asks for) - MiniMax
# bills per second of finished video, not per API call. Confirmed against MiniMax's own published
# rate (~$1.30 for a 10s 2K H3 clip = $0.13/s) as of 2026-09. Used only as a fallback estimate if
# a response is ever missing `usage.total_seconds`; the real cost from `usage` is preferred.
_PRICE_PER_SECOND_2K_USD = 0.13


def _raise_for_minimax(resp: "httpx.Response", op: str) -> None:
    text = (resp.text or "").lower()
    if resp.status_code == 401 or resp.status_code == 403:
        raise ProviderError("MiniMax API key rejected", not_configured=True)
    if "insufficient" in text or "balance" in text or resp.status_code == 402:
        raise ProviderError("MiniMax account is out of credits", out_of_credits=True)
    if resp.status_code == 429:
        raise ProviderError("MiniMax is rate-limiting requests", rate_limited=True)
    raise ProviderError(f"MiniMax {op} returned {resp.status_code}: {resp.text[:200]}")


async def _request_with_retry(method: str, url: str, headers: dict, **kwargs) -> httpx.Response:
    delay = 1.0
    last: httpx.Response | None = None
    for attempt in range(_MAX_RETRIES + 1):
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
        if resp.status_code not in _RETRY_STATUSES:
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
        logger.warning("minimax retrying", extra={"status": resp.status_code, "attempt": attempt + 1, "wait_s": wait})
        await asyncio.sleep(wait)
        delay = min(delay * 2, 15.0)
    return last  # type: ignore[return-value]


class MiniMaxVideoProvider(VideoProvider):
    """`model` is expected to be "MiniMax-H3" (or "MiniMax-H3-Max", which drops 2K support -
    stick to H3 for this integration). Always requests 2K - the whole point of trialling this
    vendor is native 2K output with no separate upscale step."""

    name = "minimax"

    def __init__(self, model: str = "MiniMax-H3"):
        self.model = model

    def _headers(self) -> dict:
        api_key = os.environ.get("MINIMAX_API_KEY")
        if not api_key:
            raise ProviderError("MINIMAX_API_KEY is not configured", not_configured=True)
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def submit(self, req: VideoRequest, config: Optional[dict] = None) -> VideoSubmit:
        # `config` unused - MiniMax has no upfront cost estimate to price (see below), the real
        # cost comes from poll()'s usage report. Accepted for interface parity with fal.
        headers = self._headers()
        # H3 requires at least one non-empty text element - fall back to a generic instruction
        # if the caller's prompt is somehow blank rather than let the API reject the whole call.
        prompt_text = (req.prompt or "Smooth, subtle camera movement across the scene.")[:7000]
        body = {
            "model": self.model,
            "resolution": "2K",
            "duration": max(4, min(15, int(req.duration_seconds or 5))),
            "ratio": "16:9",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": req.image_url, "role": "first_frame"},
            ],
        }
        try:
            resp = await _request_with_retry("POST", f"{_BASE_URL}/video_generation", headers, json=body)
        except httpx.HTTPError as exc:
            raise ProviderError(f"MiniMax submit failed: {exc}") from exc

        if resp.status_code >= 400:
            logger.warning("minimax submit non-2xx", extra={"status": resp.status_code, "body": resp.text[:500]})
            _raise_for_minimax(resp, "submit")

        data = resp.json() or {}
        job_id = data.get("task_id")
        if not job_id:
            raise ProviderError("MiniMax submit response had no task_id")
        # No upfront cost estimate from this API - the caller (studio_media_service) reserves a
        # conservative flat estimate and this gets trued up via poll()'s provider_cost_usd once
        # the job finishes and reports real usage.
        return VideoSubmit(job_id=job_id, estimated_cost_usd=None)

    async def poll(self, job_id: str, config: Optional[dict] = None) -> VideoPoll:  # config unused
        headers = self._headers()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(f"{_BASE_URL}/query/video_generation/{job_id}", headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"MiniMax poll failed: {exc}") from exc

        if resp.status_code >= 400:
            logger.warning("minimax poll non-2xx", extra={"status": resp.status_code, "body": resp.text[:500]})
            raise ProviderError(f"MiniMax poll returned {resp.status_code}")

        payload = (resp.json() or {}).get("task") or {}
        state = (payload.get("status") or "").lower()

        if state in _TERMINAL_OK:
            url = ((payload.get("content") or {}).get("url"))
            if not url:
                return VideoPoll(status="failed", error="MiniMax reported success but returned no video URL")
            usage = payload.get("usage") or {}
            output_seconds = usage.get("output_seconds") or usage.get("total_seconds") or payload.get("duration")
            cost = round(float(output_seconds) * _PRICE_PER_SECOND_2K_USD, 4) if output_seconds else None
            return VideoPoll(status="succeeded", output_url=url, provider_cost_usd=cost)

        if state in _TERMINAL_FAIL:
            err = payload.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else None
            return VideoPoll(status="failed", error=str(msg or state or "MiniMax task failed"))

        # queued / running / anything unknown -> keep waiting.
        return VideoPoll(status="processing")

    # Not offered for this provider - MiniMax H3 already renders natively at 2K, so there's
    # nothing to upscale. studio_media_service gates the whole Magnific upscale pass on the
    # render provider being "runway" specifically so these never get called.
    async def submit_video_upscale(self, video_url: str, **kwargs) -> VideoSubmit:
        raise ProviderError("Upscaling isn't available for the minimax provider")

    async def submit_image_upscale(self, image_url: str, **kwargs) -> VideoSubmit:
        raise ProviderError("Upscaling isn't available for the minimax provider")

    async def submit_upscale(self, video_url: str) -> VideoSubmit:
        raise ProviderError("Upscaling isn't available for the minimax provider")
