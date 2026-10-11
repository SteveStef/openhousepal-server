"""Read Aloud: converts an already-generated Claude reply to speech via OpenAI TTS.

Deliberately does nothing else - no reasoning, no re-answering the question. The text handed
in here is exactly what Claude already produced (see ai_routes.py's /ai/speech), so this is a
pure synthesis step, never a second model in the loop.
"""
import os

import httpx

from app.config.logging import get_logger

logger = get_logger(__name__)

_SPEECH_URL = "https://api.openai.com/v1/audio/speech"


async def synthesize_speech(text: str, config: dict) -> bytes:
    """Returns raw audio bytes (mp3). Raises plainly on any failure - missing key, non-2xx,
    network - the caller (ai_routes.py) turns that into a generic 503, never leaking upstream
    details to the client."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            _SPEECH_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": config["tts_model"],
                "voice": config["tts_voice"],
                "input": text,
            },
        )
        response.raise_for_status()
        return response.content
