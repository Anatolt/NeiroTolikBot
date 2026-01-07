import logging
import os
import tempfile
from typing import Optional, Tuple

from openai import AsyncOpenAI

from config import BOT_CONFIG
from services.memory import get_tts_voice

logger = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None


def _get_client() -> Optional[AsyncOpenAI]:
    global _client
    if _client is not None:
        return _client

    api_key = BOT_CONFIG.get("OPENAI_API_KEY")
    if not api_key:
        return None

    _client = AsyncOpenAI(api_key=api_key)
    return _client


async def synthesize_speech(text: str) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return None, "Empty text"

    client = _get_client()
    if client is None:
        logger.warning("OPENAI_API_KEY is not configured; skipping TTS.")
        return None, "OPENAI_API_KEY is not configured"

    model = BOT_CONFIG.get("TTS_MODEL", "gpt-4o-mini-tts")
    voice = get_tts_voice() or BOT_CONFIG.get("TTS_VOICE", "alloy")

    try:
        response = await client.audio.speech.create(
            model=model,
            voice=voice,
            input=text,
        )
        audio_bytes = None
        if hasattr(response, "content"):
            audio_bytes = response.content
        elif hasattr(response, "read"):
            audio_bytes = await response.read()
        if not audio_bytes:
            return None, "Empty TTS response"

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_file:
            tmp_file.write(audio_bytes)
            return tmp_file.name, None
    except Exception as exc:
        logger.warning("Failed to synthesize speech: %s", exc)
        return None, f"{exc}"
