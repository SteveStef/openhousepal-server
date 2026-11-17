"""OpenAI gpt-image-2 image editing (the primary still-image model for Studio AI Media).

Uses the /v1/images/edits endpoint with the source listing photo as the reference image, so
the edit is anchored to the real property rather than generated from scratch. Same httpx +
OPENAI_API_KEY pattern as ai_tts_service.py.
"""
import base64
import os
from typing import Optional

import httpx

from app.config.logging import get_logger
from .base import ImageEditProvider, ImageEditRequest, ImageEditResult, ProviderError

logger = get_logger(__name__)

_EDITS_URL = "https://api.openai.com/v1/images/edits"
_GENERATIONS_URL = "https://api.openai.com/v1/images/generations"
_TIMEOUT = httpx.Timeout(180.0, connect=15.0)


class OpenAIImageProvider(ImageEditProvider):
    name = "openai"

    def __init__(self, model: str = "gpt-image-2"):
        self.model = model

    async def edit(self, req: ImageEditRequest) -> ImageEditResult:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ProviderError("OPENAI_API_KEY is not configured", not_configured=True)

        data = {
            "model": self.model,
            "prompt": req.prompt,
            "n": "1",
            # "auto" lets the model keep the source's own dimensions - avoids cropping or
            # rescaling the property when editing an existing photo.
            "size": req.size,
            "quality": req.quality,
        }
        # gpt-image-2 rejects `input_fidelity` outright (invalid_input_fidelity_model); only
        # gpt-image-1 accepts it. Preservation is carried by the prompt's PRESERVATION_CLAUSE
        # instead. If a future config swaps the model back to gpt-image-1, re-enable this.
        if self.model == "gpt-image-1":
            data["input_fidelity"] = "high"
        files = {"image": ("source.jpg", req.image_bytes, req.image_content_type)}
        if req.mask_bytes:
            files["mask"] = ("mask.png", req.mask_bytes, "image/png")

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    _EDITS_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    data=data,
                    files=files,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenAI image request failed: {exc}") from exc

        if resp.status_code >= 400:
            logger.warning("openai image edit non-2xx", extra={"status": resp.status_code, "body": resp.text[:500]})
            raise ProviderError(f"OpenAI image edit returned {resp.status_code}")

        payload = resp.json()
        try:
            b64 = payload["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("OpenAI image edit response had no image") from exc

        # usage: {input_tokens, input_tokens_details:{text_tokens,image_tokens}, output_tokens}
        u = payload.get("usage") or {}
        details = u.get("input_tokens_details") or {}
        usage = {
            "text_input": details.get("text_tokens", 0),
            "image_input": details.get("image_tokens", u.get("input_tokens", 0)),
            "image_output": u.get("output_tokens", 0),
        }

        return ImageEditResult(
            image_bytes=base64.b64decode(b64),
            content_type="image/png",
            provider_cost_usd=None,
            usage=usage,
        )

    async def edit_batch(self, image_url: str, prompt: str, size: str, quality: str, cap_seconds: float) -> Optional[ImageEditResult]:
        """The same edit through OpenAI's Batch API - half price, answered later (tested 11/16: a twilight edit with
        gpt-image-2 came back in 161 s). The photo is passed by its public link. None when it hasn't answered within
        cap_seconds (the batch is cancelled; the caller sends it the normal way) or the batch failed. The usage
        returned is HALVED, so a cost worked out from it is what the batch really cost."""
        import asyncio
        import json as _json
        import time as _time
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None
        h = {"Authorization": f"Bearer {api_key}"}
        body = {"model": self.model, "prompt": prompt, "n": 1, "size": size, "quality": quality, "images": [{"image_url": image_url}]}
        line = _json.dumps({"custom_id": "edit", "method": "POST", "url": "/v1/images/edits", "body": body}).encode()
        bid = None
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                f = await client.post("https://api.openai.com/v1/files", headers=h, data={"purpose": "batch"}, files={"file": ("edit.jsonl", line, "application/jsonl")})
                f.raise_for_status()
                b = await client.post("https://api.openai.com/v1/batches", headers=h, json={"input_file_id": f.json()["id"], "endpoint": "/v1/images/edits", "completion_window": "24h"})
                b.raise_for_status()
                bid = b.json()["id"]
                began = _time.monotonic()
                st: dict = {}
                while _time.monotonic() - began < cap_seconds:
                    await asyncio.sleep(10)
                    try:
                        st = (await client.get(f"https://api.openai.com/v1/batches/{bid}", headers=h)).json()
                    except httpx.HTTPError:
                        continue
                    if st.get("status") in ("completed", "failed", "expired", "cancelled"):
                        break
                if st.get("status") != "completed" or not st.get("output_file_id"):
                    if st.get("status") not in ("failed", "expired", "cancelled"):
                        try:
                            await client.post(f"https://api.openai.com/v1/batches/{bid}/cancel", headers=h)
                        except httpx.HTTPError:
                            pass
                    logger.info("openai batch edit not back in time - sending it the normal way", extra={"status": st.get("status")})
                    return None
                out = (await client.get(f"https://api.openai.com/v1/files/{st['output_file_id']}/content", headers=h)).text
                row = _json.loads(out.splitlines()[0])
                payload = ((row.get("response") or {}).get("body")) or {}
                b64 = payload["data"][0]["b64_json"]
        except Exception as exc:  # noqa: BLE001 - the caller sends it the normal way
            logger.warning("openai batch edit failed", extra={"error": str(exc)[:300], "batch": bid})
            return None
        u = payload.get("usage") or {}
        details = u.get("input_tokens_details") or {}
        usage = {"text_input": details.get("text_tokens", 0) / 2, "image_input": details.get("image_tokens", u.get("input_tokens", 0)) / 2,
                 "image_output": u.get("output_tokens", 0) / 2}
        return ImageEditResult(image_bytes=base64.b64decode(b64), content_type="image/png", provider_cost_usd=None, usage=usage)

    async def generate(self, prompt: str, size: str = "1536x1024", quality: str = "high") -> ImageEditResult:
        """A brand-new image from a prompt (blog cover photos) - /v1/images/generations."""
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ProviderError("OPENAI_API_KEY is not configured", not_configured=True)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    _GENERATIONS_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": self.model, "prompt": prompt, "n": 1, "size": size, "quality": quality},
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenAI image request failed: {exc}") from exc
        if resp.status_code >= 400:
            logger.warning("openai image generation non-2xx", extra={"status": resp.status_code, "body": resp.text[:500]})
            raise ProviderError(f"OpenAI image generation returned {resp.status_code}")
        payload = resp.json()
        try:
            b64 = payload["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("OpenAI image generation response had no image") from exc
        u = payload.get("usage") or {}
        details = u.get("input_tokens_details") or {}
        usage = {
            "text_input": details.get("text_tokens", u.get("input_tokens", 0)),
            "image_input": details.get("image_tokens", 0),
            "image_output": u.get("output_tokens", 0),
        }
        return ImageEditResult(image_bytes=base64.b64decode(b64), content_type="image/png", provider_cost_usd=None, usage=usage)
