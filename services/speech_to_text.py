import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple

import aiohttp
from openai import AsyncOpenAI

from config import BOT_CONFIG
from services.memory import get_voice_model

logger = logging.getLogger(__name__)


_client: Optional[AsyncOpenAI] = None


def trim_silence(file_path: str) -> Tuple[str, bool]:
    """Пытается вырезать тишину через ffmpeg; возвращает путь и признак обрезки."""
    if not shutil.which("ffmpeg"):
        return file_path, False

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
            trimmed_path = tmp_file.name

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            file_path,
            "-af",
            "silenceremove=stop_periods=1:stop_duration=0.4:stop_threshold=-35dB",
            "-c:a",
            "libopus",
            trimmed_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logger.warning("ffmpeg silence trim failed: %s", result.stderr.strip())
            try:
                os.unlink(trimmed_path)
            except OSError:
                logger.warning("Failed to remove temp file %s", trimmed_path)
            return file_path, False

        return trimmed_path, True
    except Exception as exc:
        logger.warning("Failed to trim silence: %s", exc)
        return file_path, False


def estimate_transcription_cost(
    duration_seconds: float | None, size_bytes: int | None
) -> Optional[float]:
    """Грубая оценка стоимости транскрибации по длительности или размеру."""
    cost_per_min = BOT_CONFIG.get("VOICE_TRANSCRIBE_COST_PER_MIN")
    if cost_per_min is None:
        return None

    minutes = None
    if duration_seconds and duration_seconds > 0:
        minutes = duration_seconds / 60.0
    elif size_bytes and size_bytes > 0:
        size_mb = size_bytes / (1024 * 1024)
        minutes = size_mb  # оценка: ~1MB на минуту

    if minutes is None:
        return None

    try:
        return float(cost_per_min) * minutes
    except (TypeError, ValueError):
        return None


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
    try:
        size_bytes = os.path.getsize(file_path)
    except OSError:
        size_bytes = None
    hard_max_mb = BOT_CONFIG.get("VOICE_TRANSCRIBE_HARD_MAX_MB", 25)
    if size_bytes is not None:
        hard_max_bytes = int(hard_max_mb * 1024 * 1024)
        if size_bytes > hard_max_bytes:
            return None, f"Audio file exceeds {hard_max_mb}MB limit"

    model_name = get_voice_model() or BOT_CONFIG.get("VOICE_MODEL") or "whisper-1"
    if model_name == "local-whisper":
        return await _transcribe_local_whisper(file_path)

    client = _get_client()
    if client is None:
        logger.warning("OPENAI_API_KEY is not configured; skipping transcription.")
        return None, "OPENAI_API_KEY is not configured"

    try:
        with open(file_path, "rb") as file_handle:
            result = await client.audio.transcriptions.create(
                model=model_name,
                file=file_handle,
            )
        text = result.text.strip() if result and getattr(result, "text", None) else None
        return text, None
    except Exception as exc:
        logger.warning("Failed to transcribe audio: %s", exc)
        return None, f"{exc}"


async def _transcribe_local_whisper(file_path: str) -> Tuple[Optional[str], Optional[str]]:
    url = BOT_CONFIG.get("VOICE_LOCAL_WHISPER_URL")
    if not url:
        return None, "VOICE_LOCAL_WHISPER_URL is not configured"

    try:
        form = aiohttp.FormData()
        with open(file_path, "rb") as file_handle:
            form.add_field(
                "file",
                file_handle,
                filename=os.path.basename(file_path),
                content_type="application/octet-stream",
            )
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=form) as response:
                    raw_text = await response.text()
                    if response.status >= 400:
                        return None, f"Local whisper error {response.status}: {raw_text}"
                    try:
                        payload = await response.json(content_type=None)
                    except Exception:
                        payload = None
                    if isinstance(payload, dict):
                        text = payload.get("text") or payload.get("transcript") or payload.get("result")
                        if text:
                            return text.strip(), None
                    raw_text = raw_text.strip()
                    if raw_text:
                        return raw_text, None
                    return None, "Local whisper returned empty response"
    except Exception as exc:
        logger.warning("Failed to transcribe audio via local whisper: %s", exc)
        return None, f"{exc}"
