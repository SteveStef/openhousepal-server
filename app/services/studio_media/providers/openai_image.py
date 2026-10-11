"""OpenAI gpt-image-2 image editing (the primary still-image model for Studio AI Media).

Uses the /v1/images/edits endpoint with the source listing photo as the reference image, so
the edit is anchored to the real property rather than generated from scratch. Same httpx +
OPENAI_API_KEY pattern as ai_tts_service.py.
"""
import base64
import os

import httpx

from app.config.logging import get_logger
from .base import ImageEditProvider, ImageEditRequest, ImageEditResult, ProviderError

logger = get_logger(__name__)

_EDITS_URL = "https://api.openai.com/v1/images/edits"
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
