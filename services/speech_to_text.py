import logging
from typing import Optional, Tuple

from openai import AsyncOpenAI

from config import BOT_CONFIG

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


async def transcribe_audio(file_path: str) -> Tuple[Optional[str], Optional[str]]:
    client = _get_client()
    if client is None:
        logger.warning("OPENAI_API_KEY is not configured; skipping transcription.")
        return None, "OPENAI_API_KEY is not configured"

    try:
        with open(file_path, "rb") as file_handle:
            result = await client.audio.transcriptions.create(
                model="whisper-1",
                file=file_handle,
            )
        text = result.text.strip() if result and getattr(result, "text", None) else None
        return text, None
    except Exception as exc:
        logger.warning("Failed to transcribe audio: %s", exc)
        return None, f"{exc}"
