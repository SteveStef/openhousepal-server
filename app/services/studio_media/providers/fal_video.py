"""fal.ai: Hailuo 2.3 Pro image-to-video (native 1080p, flat fee/clip) + a choice of TWO upscale
vendors, both hosted on fal.ai, selected by studio_media_video_upscale_vendor:
  - "bytedance": video_url -> 1080p/2K/4K, real cost from the response's own `duration` field x
    the per-second rate for whichever resolution was actually requested. "high"/"medium" fidelity
    is deliberately faithful/mild - doesn't invent detail not in the source.
  - "topaz" (Starlight models, "generative" endpoint): explicitly REBUILDS/invents detail not in
    the source - the closer equivalent to the old Runway/Magnific "creative" upscaler's look, at
    a fraction of Magnific's cost. Takes `upscale_factor` (not a resolution enum) - 1.0 run at our
    own 1080p compact base is a same-size creative-detail enhancement pass, not a resolution
    change. Real cost isn't reported back in Topaz's response (no duration field), so it's
    computed deterministically from our own known input duration x the configured per-second
    rate at SUBMIT time (passed in via the `duration_seconds` kwarg), not trued up in poll().
Both are the default Studio AI Media video pipeline as of 2026-09, replacing the Runway
gen4_turbo+Magnific and MiniMax-H3 paths (still available, just no longer the default - see
registry.py). Nothing in this file hardcodes a price or a quality setting that has a config key
for it (see ai_config_service.py's studio_media_video_* keys) - `config` is threaded through
submit()/submit_video_upscale()/poll() so an admin can retune price/model/vendor without a
code deploy.

Docs: https://fal.ai/models/fal-ai/minimax/hailuo-2.3/pro/image-to-video/api,
      https://fal.ai/models/fal-ai/bytedance-upscaler/upscale/video/api,
      https://fal.ai/models/topaz/upscale/video/generative/api
"""
import asyncio
import os
from typing import Optional

import httpx

from app.config.logging import get_logger
from .base import ProviderError, VideoPoll, VideoProvider, VideoRequest, VideoSubmit

logger = get_logger(__name__)

_QUEUE_BASE = "https://queue.fal.run"
_UPSCALE_ENDPOINT_BYTEDANCE = "fal-ai/bytedance-upscaler/upscale/video"
# NOTE: Topaz's fal namespace is "topaz/...", NOT "fal-ai/topaz/..." - confirmed from fal's own
# model page URL (fal.ai/models/topaz/upscale/video/generative), unlike every other model in this
# file which lives under "fal-ai/".
_UPSCALE_ENDPOINT_TOPAZ = "topaz/upscale/video/generative"
_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3

# Fallbacks only - the real values always come from `config` (ai_config_service.py). These exist
# so the provider still works (at a sane, currently-real default) if ever called without a config
# dict, not as a second source of truth to keep in sync with it.
_DEFAULT_MODEL = "fal-ai/minimax/hailuo-2.3/pro/image-to-video"
_DEFAULT_HAILUO_PRICE_USD = 0.49
_DEFAULT_UPSCALE_VENDOR = "bytedance"
_DEFAULT_UPSCALE_RESOLUTION = "2k"
_DEFAULT_UPSCALE_FIDELITY = "high"
_DEFAULT_UPSCALE_PRESET = "aigc"
_DEFAULT_BYTEDANCE_PRICE_PER_SECOND_USD = {"1080p": 0.0072, "2k": 0.0144, "4k": 0.0288}
_DEFAULT_TOPAZ_MODEL = "Starlight HQ"
_DEFAULT_TOPAZ_FACTOR = 1.0
_DEFAULT_TOPAZ_PRICE_PER_SECOND_USD = 0.12  # Starlight HQ, <=1080p, <=30fps (fal's pricing page)

_TERMINAL_FAIL = {"ERROR"}
# job_id prefixes - poll() needs to know which fal endpoint a request_id belongs to (fal's
# status/result URLs are endpoint-namespaced, unlike Runway's single unified /v1/tasks/{id}), so
# submit()/submit_video_upscale() each tag their own request_id and poll() dispatches on it. Kept
# entirely internal to this provider - callers only ever see the combined job_id string.
_GEN_PREFIX = "generate:"
_UPSCALE_PREFIX = "upscale:"


def _exc_text(exc: BaseException) -> str:
    """httpx timeout/connect/transport errors frequently stringify to an EMPTY string (a known
    httpx quirk - the exception type carries the meaning, not the message), which made every log
    line built from `str(exc)` for one of these unreadable ("fal poll failed: "). Falls back to
    repr() (which always includes the exception's class name) when str() has nothing to say."""
    return str(exc) or repr(exc)


def _raise_for_fal(resp: "httpx.Response", op: str) -> None:
    if resp.status_code in (401, 403):
        raise ProviderError("fal.ai API key rejected", not_configured=True)
    text = (resp.text or "").lower()
    if resp.status_code == 402 or "insufficient" in text or "balance" in text:
        raise ProviderError("fal.ai account is out of credits", out_of_credits=True)
    if resp.status_code == 429:
        raise ProviderError("fal.ai is rate-limiting requests", rate_limited=True)
    raise ProviderError(f"fal.ai {op} returned {resp.status_code}: {resp.text[:200]}")


async def _request_with_retry(method: str, url: str, headers: dict, **kwargs) -> httpx.Response:
    """Retries on both a retryable STATUS CODE and a connection-level exception (timeout,
    connection reset, DNS blip) - a real generation observed on 2026-09-04 lost its whole
    ByteDance upscale step (and fell back to the lower-quality local ffmpeg pass) to what the
    logs show as a bare httpx timeout/connect error with an empty message - previously that class
    of failure got ZERO retries (this loop only retried on resp.status_code, and an exception
    raised before any response exists skips that check entirely), unlike a slow-but-responding
    5xx. A transient network blip should not cost an agent their already-paid-for render clips."""
    delay = 1.0
    last: httpx.Response | None = None
    last_exc: Optional[httpx.HTTPError] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt == _MAX_RETRIES:
                raise
            wait = min(delay, 15.0)
            logger.warning("fal retrying after transport error", extra={
                "error": str(exc) or repr(exc), "attempt": attempt + 1, "wait_s": wait,
            })
            await asyncio.sleep(wait)
            delay = min(delay * 2, 15.0)
            continue
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
        logger.warning("fal retrying", extra={"status": resp.status_code, "attempt": attempt + 1, "wait_s": wait})
        await asyncio.sleep(wait)
        delay = min(delay * 2, 15.0)
    if last is None and last_exc is not None:
        raise last_exc
    return last  # type: ignore[return-value]


class FalVideoProvider(VideoProvider):
    """`model` IS the fal endpoint path for generation (fal identifies the model by URL, not a
    request body field) - registry.py constructs this with config["studio_media_video_model"],
    so switching Hailuo variants (e.g. Fast Pro <-> Pro) is a config change, not a code change."""

    name = "fal"

    def __init__(self, model: str = _DEFAULT_MODEL):
        self.model = model

    def _headers(self) -> dict:
        api_key = os.environ.get("FAL_KEY")
        if not api_key:
            raise ProviderError("FAL_KEY is not configured", not_configured=True)
        return {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}

    async def _submit_queue(self, endpoint: str, body: dict, op: str) -> dict:
        headers = self._headers()
        try:
            resp = await _request_with_retry("POST", f"{_QUEUE_BASE}/{endpoint}", headers, json=body)
        except httpx.HTTPError as exc:
            raise ProviderError(f"fal {op} submit failed: {_exc_text(exc)}") from exc
        if resp.status_code >= 400:
            logger.warning("fal submit non-2xx", extra={"op": op, "status": resp.status_code, "body": resp.text[:500]})
            _raise_for_fal(resp, op)
        data = resp.json() or {}
        if not data.get("request_id"):
            raise ProviderError(f"fal {op} submit response had no request_id")
        return data

    async def submit(self, req: VideoRequest, config: Optional[dict] = None) -> VideoSubmit:
        config = config or {}
        body = {
            "image_url": req.image_url,
            "prompt": (req.prompt or "Smooth, subtle camera movement across the scene.")[:2000],
            # Fal defaults this to True (auto-rewrites the submitted prompt) - our motion prompts
            # are deliberately hand-tuned (exact degree caps, explicit "never a zoom/pan/sweep"),
            # so it's forced off unless the config explicitly opts back in - see
            # studio_media_video_prompt_optimizer in ai_config_service.py.
            "prompt_optimizer": bool(config.get("studio_media_video_prompt_optimizer", False)),
        }
        # self.model (not a separate hardcoded endpoint constant) is the actual URL submitted to -
        # this is what makes swapping Hailuo variants (Fast Pro <-> Pro) a config change: a prior
        # version of this method always posted to a hardcoded Fast Pro endpoint string regardless
        # of what studio_media_video_model said, so the config value silently had no effect.
        data = await self._submit_queue(self.model, body, "generate")
        # fal's queue request/status/result URLs are rooted at the request's own base app (e.g.
        # "fal-ai/minimax" for this model) - NOT the full multi-segment model path used to submit
        # ("fal-ai/minimax/hailuo-2.3/pro/image-to-video"). Reconstructing the poll URL from the
        # submit endpoint string (the old approach) silently 405'd on every single poll for this
        # model, leaving every video stuck at "processing" forever even though fal had already
        # finished the render within ~2-3 minutes. Store fal's own returned response_url (the one
        # true base for this request) and use it verbatim - never guess it again.
        response_url = data.get("response_url") or f"{_QUEUE_BASE}/{self.model}/requests/{data['request_id']}"
        # Flat, known price - no per-call estimate needed, unlike Runway/MiniMax. Config-driven so
        # a price change (or a Fast Pro <-> Pro swap) doesn't need a code deploy.
        price = float(config.get("studio_media_video_hailuo_price_per_clip_usd", _DEFAULT_HAILUO_PRICE_USD))
        return VideoSubmit(job_id=f"{_GEN_PREFIX}{response_url}", estimated_cost_usd=price)

    async def submit_video_upscale(self, video_url: str, **kwargs) -> VideoSubmit:
        """Upscale via fal.ai - vendor picked by studio_media_video_upscale_vendor ("bytedance" |
        "topaz"). Pass the ai_config_service config dict as the `config` kwarg (preferred) so
        every setting comes from one place rather than several individually-named kwargs (a prior
        version passed `resolution=` under a name this method never actually read, so the config
        value silently had no effect - config= as a single object avoids that whole class of
        kwarg-name mismatch). Individual keyword overrides still work and win over `config`."""
        cfg = kwargs.get("config") or {}
        vendor = kwargs.get("vendor", cfg.get("studio_media_video_upscale_vendor", _DEFAULT_UPSCALE_VENDOR))
        if vendor == "topaz":
            return await self._submit_topaz_upscale(video_url, cfg, kwargs)
        return await self._submit_bytedance_upscale(video_url, cfg, kwargs)

    async def _submit_bytedance_upscale(self, video_url: str, cfg: dict, kwargs: dict) -> VideoSubmit:
        """Resolution: a full 4x pixel jump straight to 4K from Hailuo's native 1080p source is
        too aggressive for the "standard" tier's adaptive algorithm and comes back over-sharpened/
        washed out - 2K (the default) is a gentler upscale that also matches what a phone/social-
        feed screen actually shows. Fidelity: "high" applies mild enhancement and preserves source
        texture (fal's own docs); "medium" alters more toward a punchier look. Enhancement preset:
        "aigc" is fal's preset built specifically for AI-generated source video (ours always is,
        since it's Hailuo's own output) rather than "general" (tuned for arbitrary/real camera
        footage). `enhancement_tier` MUST stay "standard" (fal's "pro" tier is 10x the cost) unless
        a caller explicitly asks for it. Real total cost isn't known until the job completes (see
        poll()) - no upfront estimate. The requested resolution is encoded into the job_id (not
        just the request body) so poll() bills the rate that actually applies, not always 4K."""
        resolution = kwargs.get("target_resolution", cfg.get("studio_media_video_upscale_resolution", _DEFAULT_UPSCALE_RESOLUTION))
        fidelity = kwargs.get("fidelity", cfg.get("studio_media_video_upscale_fidelity", _DEFAULT_UPSCALE_FIDELITY))
        preset = kwargs.get("enhancement_preset", cfg.get("studio_media_video_upscale_enhancement_preset", _DEFAULT_UPSCALE_PRESET))
        body = {
            "video_url": video_url,
            "target_resolution": resolution,
            "target_fps": kwargs.get("target_fps", 30),
            "enhancement_tier": kwargs.get("enhancement_tier", "standard"),
            "enhancement_preset": preset,
            "fidelity": fidelity,
            "bit_depth": kwargs.get("bit_depth", 8),
        }
        data = await self._submit_queue(_UPSCALE_ENDPOINT_BYTEDANCE, body, "upscale")
        response_url = data.get("response_url") or f"{_QUEUE_BASE}/{_UPSCALE_ENDPOINT_BYTEDANCE}/requests/{data['request_id']}"
        # "|" separates the vendor/resolution tag from the URL - a URL contains ":" (https://...)
        # so that can't be the delimiter. Old in-flight job_ids (pre-fix, no tag) still parse fine
        # in poll() below; they just fall back to the 4K rate, which is what they were actually billed.
        return VideoSubmit(job_id=f"{_UPSCALE_PREFIX}{resolution}|{response_url}", estimated_cost_usd=None)

    async def _submit_topaz_upscale(self, video_url: str, cfg: dict, kwargs: dict) -> VideoSubmit:
        """Topaz's Starlight models explicitly REBUILD/invent detail not in the source - the
        closer equivalent to the old Runway/Magnific "creative" upscaler's look (vs. ByteDance's
        deliberately faithful approach above), at a fraction of Magnific's per-frame cost.
        "Starlight Fast 2" is the cheapest Starlight tier. `upscale_factor` (fal's own param name -
        there's no target-resolution enum here) of 1.0 run on our own already-1080p compact base
        is a same-size creative-detail enhancement pass, not a resolution change - matches "no
        upscaling at all, just Starlight at 1080p". `H264_output` is forced True: Topaz defaults
        to H265, which has materially worse browser/device playback support than H264, and every
        later ffmpeg step in this pipeline (reframe, download) assumes H264 - leaving this False
        risks a video that generates fine but won't play everywhere. `target_fps` is deliberately
        left unset (no kwarg for it here) - Topaz only interpolates frames when told a fps that
        differs from source, and we don't want to introduce new motion-interpolation artifacts on
        top of an already-fine source. Real cost isn't in Topaz's response (no duration field), so
        it's computed here at submit time from OUR OWN known input duration (`duration_seconds`
        kwarg, passed by the caller) x the configured per-second rate - deterministic, not an
        estimate to true up later."""
        model = kwargs.get("topaz_model", cfg.get("studio_media_video_upscale_topaz_model", _DEFAULT_TOPAZ_MODEL))
        factor = kwargs.get("upscale_factor", cfg.get("studio_media_video_upscale_topaz_factor", _DEFAULT_TOPAZ_FACTOR))
        body = {
            "video_url": video_url,
            "model": model,
            "upscale_factor": float(factor),
            "H264_output": True,
        }
        data = await self._submit_queue(_UPSCALE_ENDPOINT_TOPAZ, body, "topaz-upscale")
        response_url = data.get("response_url") or f"{_QUEUE_BASE}/{_UPSCALE_ENDPOINT_TOPAZ}/requests/{data['request_id']}"
        duration = kwargs.get("duration_seconds")
        rate = float(cfg.get("studio_media_video_upscale_topaz_price_per_second_usd", _DEFAULT_TOPAZ_PRICE_PER_SECOND_USD))
        cost = round(float(duration) * rate, 4) if duration else None
        # "topaz" literal tag (not a resolution) - poll() uses it to skip the ByteDance
        # resolution-rate cost lookup, since cost is already fixed above, not re-derived on poll.
        return VideoSubmit(job_id=f"{_UPSCALE_PREFIX}topaz|{response_url}", estimated_cost_usd=cost)

    async def poll(self, job_id: str, config: Optional[dict] = None) -> VideoPoll:
        upscale_resolution = None
        is_topaz = False
        if job_id.startswith(_UPSCALE_PREFIX):
            rest, is_upscale = job_id[len(_UPSCALE_PREFIX):], True
            tag, sep, url_part = rest.partition("|")
            response_url = url_part if sep else rest  # no tag (pre-fix job_id) -> whole rest is the URL
            if tag == "topaz":
                is_topaz = True
            else:
                upscale_resolution = tag
        elif job_id.startswith(_GEN_PREFIX):
            response_url, is_upscale = job_id[len(_GEN_PREFIX):], False
        else:
            raise ProviderError(f"fal poll: unrecognized job_id {job_id!r}")

        headers = self._headers()
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                status_resp = await client.get(f"{response_url}/status", headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"fal poll failed: {_exc_text(exc)}") from exc
        if status_resp.status_code >= 400:
            logger.warning("fal poll non-2xx", extra={"status": status_resp.status_code, "body": status_resp.text[:500]})
            raise ProviderError(f"fal poll returned {status_resp.status_code}")

        state = ((status_resp.json() or {}).get("status") or "").upper()
        if state in _TERMINAL_FAIL:
            return VideoPoll(status="failed", error="fal.ai task failed")
        if state != "COMPLETED":
            return VideoPoll(status="processing")  # IN_QUEUE / IN_PROGRESS

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                result_resp = await client.get(response_url, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"fal result fetch failed: {_exc_text(exc)}") from exc
        if result_resp.status_code >= 400:
            logger.warning("fal result non-2xx", extra={"status": result_resp.status_code, "body": result_resp.text[:500]})
            raise ProviderError(f"fal result fetch returned {result_resp.status_code}")

        payload = result_resp.json() or {}
        video = payload.get("video") or {}
        url = video.get("url")
        if not url:
            return VideoPoll(status="failed", error="fal.ai reported success but returned no video URL")
        cost = None
        # Topaz: cost was already fixed deterministically at submit time (see
        # _submit_topaz_upscale - Topaz's response has no duration field to true up against
        # anyway), so leave provider_cost_usd None here - the caller keeps its submit-time figure.
        if is_upscale and not is_topaz:
            duration = payload.get("duration")
            if duration:
                price_table = (config or {}).get(
                    "studio_media_video_upscale_price_per_second_usd_by_resolution",
                    _DEFAULT_BYTEDANCE_PRICE_PER_SECOND_USD,
                )
                rate = price_table.get((upscale_resolution or "4k").lower(), price_table.get("4k", 0.0288))
                cost = round(float(duration) * rate, 4)
        return VideoPoll(status="succeeded", output_url=url, provider_cost_usd=cost)

    # Not offered for this provider - image edits go through the OpenAI provider, not video.
    async def submit_image_upscale(self, image_url: str, **kwargs) -> VideoSubmit:
        raise ProviderError("Image upscaling isn't available via the fal video provider")

    async def submit_upscale(self, video_url: str) -> VideoSubmit:
        return await self.submit_video_upscale(video_url)
