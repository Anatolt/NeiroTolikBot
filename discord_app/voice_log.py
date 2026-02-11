import asyncio
from collections import deque
import audioop
import logging
import os
import shutil
import subprocess
import re
import tempfile
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path
import wave

import discord

from config import BOT_CONFIG
from discord_app.notifications import send_telegram_notification
from discord_app.runtime import get_bot, get_telegram_bot
from discord_app.utils import count_humans_in_voice, pick_announcement_channel
from services.generation import generate_text
from services.analytics import log_stt_usage
from services.memory import (
    add_voice_log,
    add_voice_summary,
    get_last_voice_summary_date,
    get_preferred_model,
    get_recent_voice_logs,
    get_voice_logs_for_range,
    get_all_admins,
    get_voice_log_debug,
    get_voice_log_model,
    get_voice_model,
    get_voice_summary_enabled,
    get_voice_transcribe_mode,
    get_voice_transcripts_enabled,
    set_last_voice_channel,
)
from services.speech_to_text import transcribe_audio
from services.tts import synthesize_speech

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
RECENT_CHUNKS_DIR = BASE_DIR / "data" / "voice_chunks"

_voice_log_tasks: dict[int, asyncio.Task] = {}
_voice_log_processing_semaphore = asyncio.Semaphore(1)
_wake_last_trigger: dict[str, float] = {}


class RollingWaveSink(discord.sinks.Sink):
    def __init__(self, interval_seconds: float, *, filters=None):
        super().__init__(filters=filters)
        self.interval_seconds = float(interval_seconds)
        self._states: dict[object, dict[str, object]] = {}
        self._chunks: deque[dict[str, object]] = deque()
        self._lock = threading.Lock()
        self._bytes_per_second: int | None = None
        self._target_bytes: int | None = None

    def init(self, vc):
        self.vc = vc
        super().init(vc)
        self._bytes_per_second = int(vc.decoder.SAMPLING_RATE * vc.decoder.SAMPLE_SIZE)
        self._target_bytes = int(self.interval_seconds * self._bytes_per_second)

    def _open_state(self, user) -> dict[str, object]:
        safe_prefix = _sanitize_tmp_prefix(str(user))
        tmp_file = tempfile.NamedTemporaryFile(
            delete=False, suffix=".wav", prefix=f"{safe_prefix}+chunk_"
        )
        wav_handle = wave.open(tmp_file, "wb")
        wav_handle.setnchannels(self.vc.decoder.CHANNELS)
        wav_handle.setsampwidth(self.vc.decoder.SAMPLE_SIZE // self.vc.decoder.CHANNELS)
        wav_handle.setframerate(self.vc.decoder.SAMPLING_RATE)
        state = {
            "user": user,
            "file": tmp_file,
            "wave": wav_handle,
            "path": tmp_file.name,
            "bytes": 0,
        }
        self._states[user] = state
        return state

    def _finalize_state(self, user) -> dict[str, object] | None:
        state = self._states.pop(user, None)
        if not state:
            return None
        wav_handle = state.get("wave")
        file_handle = state.get("file")
        if wav_handle:
            wav_handle.close()
        if file_handle:
            file_handle.close()
        bytes_written = int(state.get("bytes") or 0)
        if bytes_written <= 0 or not self._bytes_per_second:
            return None
        duration = bytes_written / self._bytes_per_second
        entry = {
            "user_key": user,
            "tmp_path": state.get("path"),
            "duration": duration,
            "expected_seconds": float(self.interval_seconds),
        }
        return entry

    def pop_chunks(self, finalize: bool = False) -> list[dict[str, object]]:
        if finalize:
            for user in list(self._states.keys()):
                entry = self._finalize_state(user)
                if entry:
                    self._chunks.append(entry)
        with self._lock:
            items = list(self._chunks)
            self._chunks.clear()
        return items

    @discord.sinks.Filters.container
    def write(self, data, user):
        if not self.vc or not self._target_bytes or not self._bytes_per_second:
            return
        offset = 0
        data_len = len(data)
        while offset < data_len:
            state = self._states.get(user)
            if not state:
                state = self._open_state(user)
            remaining = self._target_bytes - int(state.get("bytes") or 0)
            if remaining <= 0:
                entry = self._finalize_state(user)
                if entry:
                    with self._lock:
                        self._chunks.append(entry)
                continue
            chunk = data[offset : offset + remaining]
            state["wave"].writeframesraw(chunk)
            state["bytes"] = int(state.get("bytes") or 0) + len(chunk)
            offset += len(chunk)
            if int(state.get("bytes") or 0) >= self._target_bytes:
                entry = self._finalize_state(user)
                if entry:
                    with self._lock:
                        self._chunks.append(entry)

    def cleanup(self):
        self.finished = True
        for user in list(self._states.keys()):
            entry = self._finalize_state(user)
            if entry:
                with self._lock:
                    self._chunks.append(entry)


async def _send_admin_voice_log(
    text: str,
    audio_files: list[dict[str, str]] | None = None,
) -> None:
    telegram_bot = get_telegram_bot()
    if not telegram_bot:
        return
    admins = get_all_admins()
    if not admins:
        return
    audio_files = audio_files or []
    for admin in admins:
        chat_id = admin.get("chat_id")
        if not chat_id:
            continue
        try:
            await telegram_bot.send_message(chat_id=int(chat_id), text=text)
            for audio in audio_files:
                path = audio.get("path")
                if not path or not os.path.exists(path):
                    continue
                caption = audio.get("caption")
                try:
                    with open(path, "rb") as file_handle:
                        await telegram_bot.send_document(
                            chat_id=int(chat_id),
                            document=file_handle,
                            caption=caption,
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to send voice audio to admin %s: %s", chat_id, exc
                    )
        except Exception as exc:
            logger.warning("Failed to send voice log to admin %s: %s", chat_id, exc)


def _format_voice_log_lines(
    channel: discord.abc.GuildChannel | None,
    items: list[tuple[str, str]],
) -> str:
    channel_title = getattr(channel, "name", "unknown")
    header = f"🎧 Голосовой лог Discord: {channel_title}"
    lines = [header]
    for username, text in items:
        lines.append(f"{username}: {text}")
    return "\n".join(lines)


def _split_message(text: str, limit: int = 1800) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        line = line.rstrip()
        if not line:
            line = " "
        if len(current) + len(line) + 1 > limit:
            if current:
                parts.append(current.rstrip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        parts.append(current.rstrip())
    return parts


def _format_discord_transcript(
    channel: discord.abc.GuildChannel | None,
    items: list[tuple[str, str]],
) -> str:
    channel_title = getattr(channel, "name", "unknown")
    lines = [f"🎧 Транскрипция голоса: {channel_title}"]
    for username, text in items:
        lines.append(f"{username}: {text}")
    lines.append("Отключить отправку: !transcripts_off")
    return "\n".join(lines)


def _pick_transcript_channel(
    guild: discord.Guild,
    voice_channel: discord.abc.GuildChannel | None,
) -> discord.TextChannel | None:
    target_name = getattr(voice_channel, "name", None)
    if target_name:
        for text_channel in guild.text_channels:
            if text_channel.name.lower() == str(target_name).lower():
                if text_channel.permissions_for(guild.me).send_messages:  # type: ignore[arg-type]
                    return text_channel
    channel = pick_announcement_channel(guild)
    if channel and channel.permissions_for(guild.me).send_messages:  # type: ignore[arg-type]
        return channel
    return None


def _get_ffmpeg_path() -> str | None:
    for candidate in (shutil.which("ffmpeg"), "/usr/bin/ffmpeg", "/bin/ffmpeg"):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _extract_wake_request(
    text: str,
    wake_words: list[str],
) -> tuple[str, str] | None:
    if not text or not wake_words:
        return None
    escaped = [re.escape(word) for word in wake_words if word]
    if not escaped:
        return None
    pattern = re.compile(rf"\b({'|'.join(escaped)})\b", re.IGNORECASE | re.UNICODE)
    match = pattern.search(text)
    if not match:
        return None
    tail = text[match.end() :].lstrip(" ,.:;—-!?\t\n")
    if not tail.strip():
        return None
    return match.group(1), tail.strip()


def _sanitize_voice_response(text: str) -> str:
    cleaned = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    cleaned = cleaned.replace("`", "").replace("*", "").replace("_", "")
    cleaned = cleaned.replace("#", "").replace("> ", "")
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()


async def _play_wake_response(
    voice_client: discord.VoiceClient,
    text: str,
    channel_id: str,
    user_id: str,
) -> None:
    if not voice_client or not voice_client.is_connected():
        return
    ffmpeg_path = _get_ffmpeg_path()
    if not ffmpeg_path:
        logger.warning("Wake word response skipped: ffmpeg not found")
        return
    audio_path, error = await synthesize_speech(
        text,
        platform="discord",
        chat_id=channel_id,
        user_id=user_id,
    )
    if error or not audio_path:
        logger.warning("Wake word TTS error: %s", error)
        return

    done = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _after_playback(err: Exception | None) -> None:
        if err:
            logger.warning("Wake word playback error: %s", err)
        loop.call_soon_threadsafe(done.set)

    try:
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        source = discord.FFmpegPCMAudio(audio_path, executable=ffmpeg_path)
        voice_client.play(source, after=_after_playback)
        await done.wait()
    finally:
        if os.path.exists(audio_path):
            try:
                os.unlink(audio_path)
            except OSError:
                logger.warning("Failed to remove temp file %s", audio_path)


async def _maybe_handle_wake_word(
    voice_client: discord.VoiceClient,
    channel: discord.abc.GuildChannel | None,
    user_id: int | None,
    username: str | None,
    transcript: str,
    timeline_entries: list[dict[str, object]],
    debug: callable,
) -> None:
    wake_words = BOT_CONFIG.get("VOICE_WAKE_WORDS") or [BOT_CONFIG.get("VOICE_WAKE_WORD")]
    wake_words = [word.strip() for word in wake_words if isinstance(word, str) and word.strip()]
    if not wake_words or not channel:
        return
    extracted = _extract_wake_request(transcript, wake_words)
    if not extracted:
        return
    wake_word, request_text = extracted

    channel_id = str(getattr(channel, "id", "unknown"))
    cooldown = int(BOT_CONFIG.get("VOICE_WAKE_COOLDOWN_SECONDS", 8) or 8)
    now = time.monotonic()
    last_hit = _wake_last_trigger.get(channel_id, 0.0)
    if now - last_hit < cooldown:
        debug(f"wake_word=skip cooldown={cooldown}")
        return
    _wake_last_trigger[channel_id] = now

    context_minutes = int(BOT_CONFIG.get("VOICE_WAKE_CONTEXT_MINUTES", 10) or 10)
    max_logs = int(BOT_CONFIG.get("VOICE_WAKE_MAX_LOGS", 12) or 12)
    recent_logs = get_recent_voice_logs(
        "discord",
        channel_id,
        context_minutes,
        limit=max_logs,
    )
    context_lines = []
    for row in recent_logs:
        speaker = row.get("username") or row.get("user_id") or "user"
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        context_lines.append(f"{speaker}: {text}")
    context_block = "\n".join(context_lines[-max_logs:]) or "—"

    system_prompt = (
        "Ты отвечаешь в голосовом чате Discord. "
        "Ответ должен быть коротким, 1-2 предложения. "
        "Без кода, без Markdown, без списков. "
        "Если вопрос неясен — задай короткий уточняющий вопрос."
    )
    user_prompt = (
        "Контекст последних реплик (голосовой чат):\n"
        f"{context_block}\n\n"
        f"После ключевого слова «{wake_word}» пользователь {username or 'пользователь'} сказал: {request_text}\n\n"
        "Ответь кратко."
    )

    preferred = None
    if user_id is not None:
        preferred = get_preferred_model(channel_id, str(user_id))
    model = preferred or BOT_CONFIG.get("DEFAULT_MODEL") or "gpt-4o-mini"

    debug(f"wake_word=hit model={model}")
    response, _model_used, _guard = await generate_text(
        request_text,
        model=model,
        chat_id=channel_id,
        user_id=str(user_id or ""),
        prepared_messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        use_context=False,
        platform="discord",
    )
    cleaned = _sanitize_voice_response(response or "")
    max_chars = int(BOT_CONFIG.get("VOICE_WAKE_MAX_RESPONSE_CHARS", 300) or 300)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    if not cleaned:
        return
    await _play_wake_response(
        voice_client,
        cleaned,
        channel_id=channel_id,
        user_id=str(user_id or ""),
    )
    bot_name = getattr(getattr(voice_client, "user", None), "name", None)
    bot_label = bot_name or "NeiroTolikBot"
    timeline_entries.append(
        {
            "start": 0.0,
            "end": 0.0,
            "username": bot_label,
            "text": cleaned,
        }
    )
    add_voice_log(
        platform="discord",
        guild_id=str(getattr(getattr(channel, "guild", None), "id", "")),
        channel_id=channel_id,
        user_id="bot",
        username=bot_label,
        text=cleaned,
    )


def _sanitize_tmp_prefix(raw: str | None) -> str:
    if not raw:
        return "user"
    cleaned = re.sub(r"[^\w+-]+", "_", str(raw), flags=re.UNICODE).strip("_")
    if not cleaned:
        return "user"
    return cleaned[:32]




def _store_recent_chunk(src_path: str, prefix: str | None = None) -> None:
    try:
        RECENT_CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
        safe_prefix = _sanitize_tmp_prefix(prefix) if prefix else "chunk"
        suffix = Path(src_path).suffix or ".mp3"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dst_name = f"{safe_prefix}_{timestamp}{suffix}"
        dst_path = RECENT_CHUNKS_DIR / dst_name
        shutil.copy2(src_path, dst_path)

        files = sorted(RECENT_CHUNKS_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for extra in files[3:]:
            try:
                extra.unlink()
            except OSError:
                logger.warning("Failed to remove old chunk %s", extra)
    except Exception as exc:
        logger.warning("Failed to store recent chunk %s: %s", src_path, exc)


def _stage_voice_log_audio(src_path: str, prefix: str | None = None) -> str | None:
    try:
        suffix = Path(src_path).suffix or ".wav"
        safe_prefix = _sanitize_tmp_prefix(prefix)
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix, prefix=f"{safe_prefix}+tmp_"
        ) as tmp_file:
            staged_path = tmp_file.name
        shutil.copy2(src_path, staged_path)
        _store_recent_chunk(staged_path, prefix=prefix)
        return staged_path
    except Exception as exc:
        logger.warning("Failed to stage voice log audio %s: %s", src_path, exc)
        return None


async def _convert_voice_log_audio(src_path: str) -> tuple[str, bool, str | None]:
    ffmpeg_path = _get_ffmpeg_path()
    if not ffmpeg_path:
        return src_path, False, "ffmpeg_missing"

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_file:
            dst_path = tmp_file.name

        cmd = [
            ffmpeg_path,
            "-y",
            "-i",
            src_path,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "64k",
            dst_path,
        ]
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning("ffmpeg voice log convert failed: %s", result.stderr.strip())
            try:
                os.unlink(dst_path)
            except OSError:
                logger.warning("Failed to remove temp file %s", dst_path)
            return src_path, False, result.stderr.strip() or "convert_failed"
        if not os.path.exists(dst_path) or os.path.getsize(dst_path) == 0:
            try:
                os.unlink(dst_path)
            except OSError:
                logger.warning("Failed to remove temp file %s", dst_path)
            return src_path, False, "convert_empty"

        return dst_path, True, None
    except Exception as exc:
        logger.warning("Failed to convert voice log audio: %s", exc)
        return src_path, False, str(exc)


async def _split_voice_log_audio(
    src_path: str, segment_seconds: int = 20
) -> tuple[list[str], str | None, str | None]:
    ffmpeg_path = _get_ffmpeg_path()
    if not ffmpeg_path:
        return [src_path], None, "ffmpeg_missing"

    try:
        tmp_dir = tempfile.mkdtemp(prefix="voice_log_")
        pattern = os.path.join(tmp_dir, "segment_%03d.mp3")
        cmd = [
            ffmpeg_path,
            "-y",
            "-i",
            src_path,
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-reset_timestamps",
            "1",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "64k",
            pattern,
        ]
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning("ffmpeg voice log split failed: %s", result.stderr.strip())
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except OSError:
                logger.warning("Failed to remove temp dir %s", tmp_dir)
            return [src_path], None, result.stderr.strip() or "split_failed"

        segments = sorted(
            os.path.join(tmp_dir, name)
            for name in os.listdir(tmp_dir)
            if name.endswith(".mp3")
        )
        if not segments:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except OSError:
                logger.warning("Failed to remove temp dir %s", tmp_dir)
            return [src_path], None, "split_no_segments"

        return segments, tmp_dir, None
    except Exception as exc:
        logger.warning("Failed to split voice log audio: %s", exc)
        return [src_path], None, str(exc)


async def _concat_voice_log_audio(src_paths: list[str]) -> tuple[str | None, str | None]:
    ffmpeg_path = _get_ffmpeg_path()
    if not ffmpeg_path:
        return None, "ffmpeg_missing"
    if not src_paths:
        return None, "no_sources"
    if len(src_paths) == 1:
        return src_paths[0], None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
            dst_path = tmp_file.name

        cmd = [ffmpeg_path, "-y"]
        for path in src_paths:
            cmd.extend(["-i", path])
        cmd.extend(
            [
                "-filter_complex",
                f"concat=n={len(src_paths)}:v=0:a=1",
                "-ac",
                "1",
                "-ar",
                "16000",
                dst_path,
            ]
        )
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning("ffmpeg voice log concat failed: %s", result.stderr.strip())
            try:
                os.unlink(dst_path)
            except OSError:
                logger.warning("Failed to remove temp file %s", dst_path)
            return None, result.stderr.strip() or "concat_failed"
        if not os.path.exists(dst_path) or os.path.getsize(dst_path) == 0:
            try:
                os.unlink(dst_path)
            except OSError:
                logger.warning("Failed to remove temp file %s", dst_path)
            return None, "concat_empty"
        return dst_path, None
    except Exception as exc:
        logger.warning("Failed to concat voice log audio: %s", exc)
        return None, str(exc)


def _detect_speech_segments(
    wav_path: str,
    frame_ms: int = 30,
    min_speech_ms: int = 300,
    min_silence_ms: int = 400,
) -> tuple[list[tuple[float, float]], dict[str, int | float]]:
    stats: dict[str, int | float] = {}
    try:
        with wave.open(wav_path, "rb") as wav_file:
            rate = wav_file.getframerate()
            width = wav_file.getsampwidth()
            total_frames = wav_file.getnframes()
            channels = wav_file.getnchannels()
            stats.update(
                {
                    "rate": rate,
                    "width": width,
                    "channels": channels,
                    "frames": total_frames,
                }
            )

            if width != 2:
                return [(0.0, total_frames / rate)], stats

            frames_per_window = max(1, int(rate * frame_ms / 1000))
            rms_values: list[int] = []
            while True:
                data = wav_file.readframes(frames_per_window)
                if not data:
                    break
                rms_values.append(audioop.rms(data, width))

            if not rms_values and total_frames == 0:
                ffmpeg_path = _get_ffmpeg_path()
                if not ffmpeg_path:
                    return [], stats
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".raw") as tmp_file:
                        raw_path = tmp_file.name
                    cmd = [
                        ffmpeg_path,
                        "-y",
                        "-i",
                        wav_path,
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-f",
                        "s16le",
                        raw_path,
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
                    if result.returncode != 0:
                        logger.warning("ffmpeg decode failed: %s", result.stderr.strip())
                        try:
                            os.unlink(raw_path)
                        except OSError:
                            logger.warning("Failed to remove temp file %s", raw_path)
                        return [], stats

                    with open(raw_path, "rb") as raw_handle:
                        raw_data = raw_handle.read()
                    try:
                        os.unlink(raw_path)
                    except OSError:
                        logger.warning("Failed to remove temp file %s", raw_path)

                    raw_rate = 16000
                    raw_width = 2
                    frames_per_window = max(1, int(raw_rate * frame_ms / 1000))
                    bytes_per_window = frames_per_window * raw_width
                    rms_values = []
                    for offset in range(0, len(raw_data), bytes_per_window):
                        chunk = raw_data[offset : offset + bytes_per_window]
                        if not chunk:
                            break
                        rms_values.append(audioop.rms(chunk, raw_width))
                    total_frames = len(raw_data) // raw_width
                    rate = raw_rate
                    stats.update(
                        {
                            "rate": rate,
                            "width": raw_width,
                            "channels": 1,
                            "frames": total_frames,
                            "ffmpeg_decode": 1,
                        }
                    )
                except Exception as exc:
                    logger.warning("Failed to decode wav for VAD: %s", exc)
                    return [], stats

            if not rms_values:
                return [], stats

            sorted_vals = sorted(rms_values)
            noise_floor = sorted_vals[int(len(sorted_vals) * 0.6)]
            threshold = max(50, int(noise_floor * 1.2))
            total_windows = len(rms_values)
            silence_windows = sum(1 for v in rms_values if v < threshold)
            zero_windows = sum(1 for v in rms_values if v == 0)
            max_silence_run = 0
            current_silence = 0
            for value in rms_values:
                if value < threshold:
                    current_silence += 1
                    if current_silence > max_silence_run:
                        max_silence_run = current_silence
                else:
                    current_silence = 0
            stats.update(
                {
                    "noise_floor": noise_floor,
                    "threshold": threshold,
                    "rms_max": max(rms_values),
                    "rms_min": min(rms_values),
                    "rms_avg": sum(rms_values) / total_windows if total_windows else 0,
                    "window_ms": frame_ms,
                    "windows": total_windows,
                    "silence_windows": silence_windows,
                    "zero_windows": zero_windows,
                    "max_silence_windows": max_silence_run,
                }
            )

            min_speech_frames = max(1, int(min_speech_ms / frame_ms))
            min_silence_frames = max(1, int(min_silence_ms / frame_ms))

            segments: list[tuple[int, int]] = []
            in_speech = False
            start_idx = 0
            silence_count = 0

            for idx, rms in enumerate(rms_values):
                if rms >= threshold:
                    if not in_speech:
                        in_speech = True
                        start_idx = idx
                    silence_count = 0
                else:
                    if in_speech:
                        silence_count += 1
                        if silence_count >= min_silence_frames:
                            end_idx = idx - silence_count + 1
                            if end_idx - start_idx >= min_speech_frames:
                                segments.append((start_idx, end_idx))
                            in_speech = False
                            silence_count = 0

            if in_speech:
                end_idx = len(rms_values)
                if end_idx - start_idx >= min_speech_frames:
                    segments.append((start_idx, end_idx))

            return (
                [
                    (start * frame_ms / 1000.0, end * frame_ms / 1000.0)
                    for start, end in segments
                ],
                stats,
            )
    except Exception as exc:
        logger.warning("Failed to detect speech segments: %s", exc)
        return [], stats


def _split_long_segments(
    segments: list[tuple[float, float]], max_duration: float = 3.0
) -> list[tuple[float, float]]:
    if not segments:
        return []
    if max_duration <= 0:
        return segments
    result: list[tuple[float, float]] = []
    for start, end in segments:
        if end <= start:
            continue
        if end - start <= max_duration:
            result.append((start, end))
            continue
        cursor = start
        while cursor < end:
            chunk_end = min(end, cursor + max_duration)
            result.append((cursor, chunk_end))
            cursor = chunk_end
    return result


def _build_pause_chunks(
    segments: list[tuple[float, float]],
    seg_stats: dict[str, int | float],
    max_bytes: int,
) -> list[tuple[float, float]]:
    if not segments:
        return []

    rate = float(seg_stats.get("rate", 0) or 0)
    width = float(seg_stats.get("width", 0) or 0)
    channels = float(seg_stats.get("channels", 0) or 0)
    bytes_per_sec = rate * width * channels
    if bytes_per_sec <= 0 or max_bytes <= 0:
        return segments

    max_seconds = max_bytes / bytes_per_sec
    if max_seconds <= 0:
        return segments

    chunks: list[tuple[float, float]] = []
    cur_start: float | None = None
    cur_end: float | None = None

    for start, end in segments:
        if end <= start:
            continue

        if (end - start) > max_seconds:
            if cur_start is not None and cur_end is not None:
                chunks.append((cur_start, cur_end))
                cur_start = None
                cur_end = None
            chunks.extend(_split_long_segments([(start, end)], max_duration=max_seconds))
            continue

        if cur_start is None:
            cur_start, cur_end = start, end
            continue

        if (end - cur_start) > max_seconds:
            chunks.append((cur_start, cur_end if cur_end is not None else end))
            cur_start, cur_end = start, end
        else:
            cur_end = end

    if cur_start is not None and cur_end is not None:
        chunks.append((cur_start, cur_end))

    return chunks or segments


def _extract_wav_segment(src_path: str, start_sec: float, end_sec: float) -> str | None:
    try:
        ffmpeg_path = _get_ffmpeg_path()
        if not ffmpeg_path:
            return None

        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
            segment_path = tmp_file.name

        cmd = [
            ffmpeg_path,
            "-y",
            "-ss",
            f"{start_sec}",
            "-to",
            f"{end_sec}",
            "-i",
            src_path,
            "-ac",
            "1",
            "-ar",
            "16000",
            segment_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logger.warning("ffmpeg segment extract failed: %s", result.stderr.strip())
            try:
                os.unlink(segment_path)
            except OSError:
                logger.warning("Failed to remove temp file %s", segment_path)
            return None

        return segment_path
    except Exception as exc:
        logger.warning("Failed to extract wav segment: %s", exc)
        return None


async def _voice_log_capture_callback(
    sink: discord.sinks.Sink,
    voice_client: discord.VoiceClient,
    done_event: asyncio.Event,
) -> None:
    done_event.set()


async def _send_discord_transcript(
    voice_channel: discord.abc.GuildChannel | None,
    items: list[tuple[str, str]],
) -> None:
    if not voice_channel or not items:
        return
    if not get_voice_transcripts_enabled(str(voice_channel.id)):
        return

    guild = getattr(voice_channel, "guild", None)
    if not guild:
        return
    text_channel = _pick_transcript_channel(guild, voice_channel)
    if not text_channel:
        return

    message_text = _format_discord_transcript(voice_channel, items)
    for part in _split_message(message_text):
        try:
            logger.info(
                "Sending transcript to text channel %s in guild %s",
                text_channel.id,
                guild.id,
            )
            await text_channel.send(part)
        except Exception as exc:
            logger.warning("Failed to send transcript to Discord: %s", exc)
            break


def _count_voice_sessions(
    rows: list[dict[str, object]],
    gap_minutes: int,
) -> tuple[int, list[str]]:
    if not rows:
        return 0, []
    gap = timedelta(minutes=gap_minutes)
    boundaries: list[str] = []
    sessions = 1
    prev_ts: datetime | None = None
    for row in rows:
        ts_raw = row.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except ValueError:
            continue
        if prev_ts and ts - prev_ts > gap:
            sessions += 1
            boundaries.append(ts.isoformat())
        prev_ts = ts
    return sessions, boundaries


async def _maybe_send_daily_summary(
    voice_channel: discord.abc.GuildChannel | None,
) -> None:
    if not voice_channel:
        return
    if not get_voice_summary_enabled(str(voice_channel.id)):
        return

    today = datetime.now().date()
    summary_date = today - timedelta(days=1)
    summary_date_str = summary_date.isoformat()

    last_summary_date = get_last_voice_summary_date("discord", str(voice_channel.id))
    if last_summary_date == summary_date_str:
        return

    start_dt = datetime.combine(summary_date, datetime.min.time())
    end_dt = start_dt + timedelta(days=1)
    summary_message = await generate_voice_summary_for_range(
        voice_channel,
        start_dt,
        end_dt,
        f"Саммари {summary_date_str}",
    )
    if not summary_message:
        add_voice_summary(
            "discord",
            str(voice_channel.id),
            summary_date_str,
            "Нет разговоров.",
            guild_id=str(getattr(voice_channel.guild, "id", "")),
        )
        return

    try:
        await send_telegram_notification(
            summary_message,
            discord_channel_id=str(voice_channel.id),
        )
    except Exception as exc:
        logger.warning("Failed to send daily voice summary: %s", exc)

    add_voice_summary(
        "discord",
        str(voice_channel.id),
        summary_date_str,
        summary_message,
        guild_id=str(getattr(voice_channel.guild, "id", "")),
    )


async def generate_voice_summary_for_range(
    voice_channel: discord.abc.GuildChannel,
    start_dt: datetime,
    end_dt: datetime,
    title: str,
) -> str | None:
    rows = get_voice_logs_for_range(
        "discord",
        str(voice_channel.id),
        start_dt.isoformat(),
        end_dt.isoformat(),
    )
    if not rows:
        return None

    gap_minutes = int(BOT_CONFIG.get("VOICE_SUMMARY_GAP_MINUTES", 30) or 30)
    sessions, _boundaries = _count_voice_sessions(rows, gap_minutes)

    max_chars = int(BOT_CONFIG.get("VOICE_SUMMARY_MAX_CHARS", 40000) or 40000)
    lines: list[str] = []
    used = 0
    truncated = False
    for row in rows:
        username = row.get("username") or row.get("user_id") or "user"
        text = row.get("text") or ""
        ts = row.get("timestamp") or ""
        line = f"{ts} {username}: {text}"
        if used + len(line) + 1 > max_chars:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1

    transcript_block = "\n".join(lines)
    summary_prompt = (
        "Сделай краткое саммари разговора из голосового чата. "
        "Укажи основные темы и события. "
        f"Если было несколько отдельных диалогов с паузами больше {gap_minutes} минут, "
        "обязательно отметь это в саммари и укажи количество сессий. "
        "Пиши без Markdown-форматирования и уложись в одно сообщение Telegram."
    )
    if truncated:
        summary_prompt += " Источник урезан по длине, укажи это, если заметно."

    prepared_messages = [
        {"role": "system", "content": summary_prompt},
        {"role": "user", "content": f"Транскрипция:\n{transcript_block}"},
    ]
    summary_model = BOT_CONFIG.get("VOICE_SUMMARY_MODEL") or BOT_CONFIG.get("DEFAULT_MODEL")
    summary_text, _used_model, _info = await generate_text(
        prompt="",
        model=summary_model,
        prepared_messages=prepared_messages,
        use_context=False,
        platform="discord",
        chat_id=str(getattr(voice_channel, "id", "")),
        user_id="voice_summary",
    )

    header = f"{title} — {getattr(voice_channel, 'name', 'voice')}"
    if sessions > 1:
        header += f" (сессий: {sessions})"
    summary_text = _sanitize_summary_text(summary_text)
    combined = f"{header}\n{summary_text}" if summary_text else header
    max_chars = int(BOT_CONFIG.get("VOICE_SUMMARY_TELEGRAM_MAX_CHARS", 3800) or 3800)
    if len(combined) > max_chars:
        combined = combined[: max_chars - 1].rstrip() + "…"
    return combined


def _sanitize_summary_text(text: str) -> str:
    cleaned = (text or "").replace("\r", "\n").strip()
    for token in ("**", "__", "*", "_", "`"):
        cleaned = cleaned.replace(token, "")
    for token in ("###", "##", "#", ">"):
        cleaned = cleaned.replace(token, "")
    lines = [line.strip() for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line)


def _collect_interval_entries(
    sink: discord.sinks.Sink,
    tail_seconds: float,
    expected_seconds: float,
) -> tuple[list[dict[str, object]], bool]:
    entries: list[dict[str, object]] = []
    extend = False

    for user_key, audio in sink.audio_data.items():
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
                tmp_path = tmp_file.name
                audio.file.seek(0)
                tmp_file.write(audio.file.read())

            with open(tmp_path, "rb") as verify_handle:
                header = verify_handle.read(12)
            if not (header.startswith(b"RIFF") and b"WAVE" in header):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.warning("Failed to remove temp file %s", tmp_path)
                continue

            segments, seg_stats = _detect_speech_segments(tmp_path)
            rate = float(seg_stats.get("rate", 0) or 0)
            frames = float(seg_stats.get("frames", 0) or 0)
            duration = frames / rate if rate > 0 and frames > 0 else 0.0
            last_end = segments[-1][1] if segments else 0.0

            if segments and duration > 0 and (duration - last_end) <= tail_seconds:
                extend = True

            expected = float(expected_seconds or 0)
            gap = expected - duration
            gap_pct = (gap / expected * 100.0) if expected > 0 else 0.0
            entries.append(
                {
                    "user_key": user_key,
                    "tmp_path": tmp_path,
                    "duration": duration,
                    "expected_seconds": expected,
                    "gap_seconds": gap,
                    "gap_pct": gap_pct,
                }
            )
        except Exception as exc:
            logger.warning("Failed to collect voice log audio: %s", exc)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.warning("Failed to remove temp file %s", tmp_path)

    return entries, extend


async def _process_voice_log_entries(
    entries: list[dict[str, object]],
    voice_client: discord.VoiceClient,
) -> None:
    if not entries:
        return

    grouped: dict[object, list[str]] = {}
    user_keys: dict[object, object] = {}
    capture_stats: dict[object, dict[str, float]] = {}
    for entry in entries:
        user_key = entry["user_key"]
        tmp_path = entry["tmp_path"]
        if not tmp_path:
            continue
        user_id = getattr(user_key, "id", user_key)
        if user_id not in grouped:
            grouped[user_id] = []
            user_keys[user_id] = user_key
            capture_stats[user_id] = {"expected": 0.0, "duration": 0.0}
        grouped[user_id].append(tmp_path)
        stats = capture_stats[user_id]
        stats["expected"] += float(entry.get("expected_seconds") or 0)
        stats["duration"] += float(entry.get("duration") or 0)

    merged_paths: list[str] = []
    audio_data: dict[object, object] = {}
    open_handles: list[object] = []
    try:
        for user_id, paths in grouped.items():
            merged_path = None
            if len(paths) == 1:
                merged_path = paths[0]
            else:
                merged_path, concat_error = await _concat_voice_log_audio(paths)
                if concat_error:
                    logger.warning("Failed to concat audio for user %s: %s", user_id, concat_error)
                    merged_path = None
                else:
                    merged_paths.append(merged_path)
            if not merged_path:
                continue
            user_key = user_keys[user_id]
            handle = open(merged_path, "rb")
            open_handles.append(handle)
            audio_data[user_key] = type("Audio", (), {"file": handle})()

        if not audio_data:
            return

        sink = type("MergedSink", (), {"audio_data": audio_data, "capture_stats": capture_stats})()
        await process_voice_log_sink(sink, voice_client)
    finally:
        for handle in open_handles:
            try:
                handle.close()
            except Exception:
                pass
        for entry in entries:
            tmp_path = entry.get("tmp_path")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.warning("Failed to remove temp file %s", tmp_path)
        for path in merged_paths:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    logger.warning("Failed to remove temp file %s", path)


async def process_voice_log_sink(
    sink: discord.sinks.Sink, voice_client: discord.VoiceClient
) -> None:
    channel = getattr(voice_client, "channel", None)
    guild = getattr(channel, "guild", None)

    timeline_entries: list[dict[str, object]] = []
    debug_enabled = get_voice_log_debug()
    debug_lines: list[str] = []
    capture_stats = getattr(sink, "capture_stats", None) or {}
    stt_errors: list[str] = []
    pending_audio_files: list[dict[str, str]] = []

    def _debug(line: str) -> None:
        if debug_enabled:
            debug_lines.append(line)
    if not sink.audio_data:
        logger.info("Voice log sink empty for channel %s", getattr(channel, "id", "unknown"))
    else:
        user_keys = []
        for user_key in sink.audio_data.keys():
            user_keys.append(getattr(user_key, "id", user_key))
        logger.info(
            "Voice log sink users for channel %s: %s",
            getattr(channel, "id", "unknown"),
            ",".join(str(key) for key in user_keys),
        )
    bot = get_bot()
    source_entries: list[dict[str, object]] = []
    for user_key, audio in sink.audio_data.items():
        member = None
        user_id = None
        username = None
        if hasattr(user_key, "id"):
            user_id = int(user_key.id)
            member = guild.get_member(user_id) if guild else None
            username = getattr(user_key, "display_name", None) or getattr(user_key, "name", None)
        else:
            try:
                user_id = int(user_key)
            except (TypeError, ValueError):
                user_id = None
            if user_id and guild:
                member = guild.get_member(user_id)
            if member:
                username = member.display_name
            elif user_id:
                try:
                    fetched = await bot.fetch_user(user_id)
                    username = getattr(fetched, "name", None)
                except Exception:
                    username = None
        if member and member.bot:
            continue

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
                tmp_path = tmp_file.name
                audio.file.seek(0)
                tmp_file.write(audio.file.read())

            with open(tmp_path, "rb") as verify_handle:
                header = verify_handle.read(12)
            if not (header.startswith(b"RIFF") and b"WAVE" in header):
                logger.warning("Skipping non-wav audio for user %s", user_id)
                debug_prefix = f"user={username or user_id or user_key}"
                _debug(f"{debug_prefix} skip=non_wav")
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.warning("Failed to remove temp file %s", tmp_path)
                continue
        except Exception as exc:
            logger.warning("Failed to prepare voice log audio for user %s: %s", user_id, exc)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.warning("Failed to remove temp file %s", tmp_path)
            continue

        source_entries.append(
            {
                "user_key": user_key,
                "user_id": user_id,
                "username": username,
                "member": member,
                "tmp_path": tmp_path,
            }
        )

    cleanup_paths = {entry["tmp_path"] for entry in source_entries}

    for entry in source_entries:
        user_key = entry["user_key"]
        user_id = entry["user_id"]
        username = entry["username"]
        member = entry["member"]
        tmp_path = entry["tmp_path"]
        debug_prefix = f"user={username or user_id or user_key}"
        if debug_enabled and user_id in capture_stats:
            stats = capture_stats[user_id]
            expected = float(stats.get("expected") or 0)
            actual = float(stats.get("duration") or 0)
            gap = expected - actual
            gap_pct = (gap / expected * 100.0) if expected > 0 else 0.0
            _debug(
                f"{debug_prefix} capture expected={expected:.2f}s actual={actual:.2f}s "
                f"gap={gap:.2f}s gap_pct={gap_pct:.1f}"
            )

        voice_model = (
            get_voice_log_model()
            or get_voice_model()
            or BOT_CONFIG.get("VOICE_MODEL")
            or "whisper-1"
        )
        stored_mode = (get_voice_transcribe_mode() or "").lower()
        if stored_mode not in {"raw", "segmented"}:
            send_mode = "raw" if voice_model == "local-whisper" else "segmented"
        else:
            send_mode = stored_mode

        segments, seg_stats = _detect_speech_segments(tmp_path)
        if debug_enabled and seg_stats:
            rate = float(seg_stats.get("rate", 0) or 0)
            frames = float(seg_stats.get("frames", 0) or 0)
            duration = frames / rate if rate > 0 and frames > 0 else 0.0
            window_ms = int(seg_stats.get("window_ms", 0) or 0)
            windows = int(seg_stats.get("windows", 0) or 0)
            silence_windows = int(seg_stats.get("silence_windows", 0) or 0)
            zero_windows = int(seg_stats.get("zero_windows", 0) or 0)
            max_silence_windows = int(seg_stats.get("max_silence_windows", 0) or 0)
            silence_pct = (silence_windows / windows * 100.0) if windows else 0.0
            zero_pct = (zero_windows / windows * 100.0) if windows else 0.0
            max_silence_ms = max_silence_windows * window_ms
            rms_min = seg_stats.get("rms_min")
            rms_avg = seg_stats.get("rms_avg")
            rms_max = seg_stats.get("rms_max")
            noise = seg_stats.get("noise_floor")
            threshold = seg_stats.get("threshold")
            rms_avg_text = f"{rms_avg:.1f}" if isinstance(rms_avg, (int, float)) else str(rms_avg)
            _debug(
                f"{debug_prefix} wav_stats rate={rate:.0f} frames={frames:.0f} dur={duration:.2f}s "
                f"win_ms={window_ms} windows={windows} silence_pct={silence_pct:.1f} "
                f"zero_pct={zero_pct:.1f} max_silence_ms={max_silence_ms}"
            )
            _debug(
                f"{debug_prefix} rms min={rms_min} avg={rms_avg_text} max={rms_max} "
                f"noise={noise} thr={threshold}"
            )
        if send_mode == "raw":
            rate = float(seg_stats.get("rate", 0) or 0)
            frames = float(seg_stats.get("frames", 0) or 0)
            duration = frames / rate if rate > 0 and frames > 0 else 0.0
            if duration <= 0:
                _debug(f"{debug_prefix} skip=raw_no_duration stats={seg_stats}")
                continue
            segments = [(0.0, duration)]
            _debug(f"{debug_prefix} send_mode=raw duration={duration:.2f}")
        else:
            if not segments:
                _debug(
                    f"{debug_prefix} skip=no_speech stats={seg_stats}"
                )
                rate = float(seg_stats.get("rate", 0) or 0)
                frames = float(seg_stats.get("frames", 0) or 0)
                if rate > 0 and frames > 0:
                    segments = [(0.0, frames / rate)]
                else:
                    continue
            max_mb = BOT_CONFIG.get("VOICE_TRANSCRIBE_MAX_MB", 10)
            hard_max_mb = BOT_CONFIG.get("VOICE_TRANSCRIBE_HARD_MAX_MB", 25)
            max_bytes = int(min(max_mb, hard_max_mb) * 1024 * 1024)
            segments = _build_pause_chunks(segments, seg_stats, max_bytes)
            _debug(
                f"{debug_prefix} send_mode=segmented segments={len(segments)} max_mb={max_mb}"
            )

        for seg_idx, (start_sec, end_sec) in enumerate(segments, start=1):
            segment_wav = _extract_wav_segment(tmp_path, start_sec, end_sec)
            if not segment_wav:
                _debug(
                    f"{debug_prefix} seg{seg_idx} skip=extract_failed"
                )
                continue

            converted_path = None
            segment_dir = None
            segment_paths: list[str] = []
            try:
                if send_mode == "raw":
                    audio_path = segment_wav
                    size_bytes = os.path.getsize(audio_path)
                    hard_max = int(BOT_CONFIG.get("VOICE_TRANSCRIBE_HARD_MAX_MB", 25) * 1024 * 1024)
                    if size_bytes > hard_max:
                        logger.info(
                            "Skipping raw audio over limit for user %s (%s bytes)",
                            user_id,
                            size_bytes,
                        )
                        _debug(
                            f"{debug_prefix} seg{seg_idx} skip=raw_over_limit size={size_bytes}"
                        )
                        continue
                    segment_paths = [audio_path]
                else:
                    audio_path, converted, convert_error = await _convert_voice_log_audio(segment_wav)
                    if converted:
                        converted_path = audio_path
                        _debug(f"{debug_prefix} seg{seg_idx} converted=mp3")
                    elif convert_error:
                        _debug(f"{debug_prefix} seg{seg_idx} convert_error={convert_error}")

                    size_bytes = os.path.getsize(audio_path)
                    max_mb = BOT_CONFIG.get("VOICE_TRANSCRIBE_MAX_MB", 10)
                    hard_max_mb = BOT_CONFIG.get("VOICE_TRANSCRIBE_HARD_MAX_MB", 25)
                    max_bytes = int(min(max_mb, hard_max_mb) * 1024 * 1024)
                    if size_bytes > max_bytes:
                        segment_paths, segment_dir, split_error = await _split_voice_log_audio(audio_path)
                        if len(segment_paths) == 1:
                            logger.info(
                                "Skipping audio over limit for user %s (%s bytes)",
                                user_id,
                                size_bytes,
                            )
                            if split_error:
                                _debug(
                                    f"{debug_prefix} seg{seg_idx} skip=over_limit size={size_bytes} split_error={split_error}"
                                )
                            else:
                                _debug(
                                    f"{debug_prefix} seg{seg_idx} skip=over_limit size={size_bytes}"
                                )
                            continue
                        _debug(
                            f"{debug_prefix} seg{seg_idx} split_segments={len(segment_paths)} size={size_bytes}"
                        )
                    else:
                        segment_paths = [audio_path]
                    if size_bytes < 256:
                        logger.info("Skipping too small audio for user %s (%s bytes)", user_id, size_bytes)
                        _debug(f"{debug_prefix} seg{seg_idx} skip=too_small size={size_bytes}")
                        continue

                _debug(
                    f"{debug_prefix} seg{seg_idx} stt_model={voice_model} size={size_bytes} start={start_sec:.2f} end={end_sec:.2f}"
                )
                transcript_parts: list[str] = []
                transcribed_bytes = 0
                for part_idx, segment_path in enumerate(segment_paths, start=1):
                    segment_size = os.path.getsize(segment_path)
                    transcript, error = await transcribe_audio(segment_path, user_id=str(user_id))
                    if not transcript:
                        if error:
                            logger.warning("Discord channel STT error: %s", error)
                            _debug(
                                f"{debug_prefix} seg{seg_idx} stt_error_part{part_idx}={error}"
                            )
                            if len(stt_errors) < 3 and error not in stt_errors:
                                stt_errors.append(error)
                        continue
                    transcript_parts.append(transcript)
                    transcribed_bytes += segment_size
                    _debug(
                        f"{debug_prefix} seg{seg_idx} stt_ok_part{part_idx} size={segment_size} chars={len(transcript)}"
                    )

                if not transcript_parts:
                    continue

                transcript = " ".join(transcript_parts)
                if segment_paths:
                    total_parts = len(segment_paths)
                    name_prefix = username or (str(user_id) if user_id else str(user_key))
                    for part_idx, segment_path in enumerate(segment_paths, start=1):
                        staged_path = _stage_voice_log_audio(segment_path, prefix=name_prefix)
                        if not staged_path:
                            continue
                        part_suffix = (
                            f" part {part_idx}/{total_parts}"
                            if total_parts > 1
                            else ""
                        )
                        pending_audio_files.append(
                            {
                                "path": staged_path,
                                "caption": f"{username}{part_suffix} {start_sec:.1f}-{end_sec:.1f}s",
                            }
                        )
                if len(transcript.strip()) < 3 and (end_sec - start_sec) < 0.6:
                    continue
                username = username or (member.display_name if member else str(user_id))
                timeline_entries.append(
                    {
                        "start": start_sec,
                        "end": end_sec,
                        "username": username,
                        "text": transcript,
                    }
                )
                add_voice_log(
                    platform="discord",
                    guild_id=str(guild.id) if guild else None,
                    channel_id=str(channel.id) if channel else "unknown",
                    user_id=str(user_id or user_key),
                    username=username,
                    text=transcript,
                )
                log_stt_usage(
                    platform="discord",
                    chat_id=str(channel.id) if channel else "unknown",
                    user_id=str(user_id),
                    duration_seconds=None,
                    size_bytes=transcribed_bytes or size_bytes,
                )
                await _maybe_handle_wake_word(
                    voice_client,
                    channel,
                    user_id,
                    username,
                    transcript,
                    timeline_entries,
                    _debug,
                )
            finally:
                if segment_dir and os.path.exists(segment_dir):
                    try:
                        shutil.rmtree(segment_dir, ignore_errors=True)
                    except OSError:
                        logger.warning("Failed to remove temp dir %s", segment_dir)
                if converted_path and os.path.exists(converted_path):
                    try:
                        os.unlink(converted_path)
                    except OSError:
                        logger.warning("Failed to remove temp file %s", converted_path)
                if segment_wav and os.path.exists(segment_wav):
                    try:
                        os.unlink(segment_wav)
                    except OSError:
                        logger.warning("Failed to remove temp file %s", segment_wav)

    for tmp_path in cleanup_paths:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                logger.warning("Failed to remove temp file %s", tmp_path)

    items: list[tuple[str, str]] = []
    if timeline_entries:
        ordered_entries = sorted(timeline_entries, key=lambda item: item["start"])
        merged_entries: list[dict[str, object]] = []
        for entry in ordered_entries:
            if (
                merged_entries
                and merged_entries[-1]["username"] == entry["username"]
                and float(entry["start"]) - float(merged_entries[-1]["end"]) <= 0.5
            ):
                merged_entries[-1]["text"] = (
                    f"{merged_entries[-1]['text']} {entry['text']}"
                )
                merged_entries[-1]["end"] = entry["end"]
            else:
                merged_entries.append(entry)
        items = [
            (entry["username"], entry["text"]) for entry in merged_entries
        ]

    try:
        if items:
            await _send_discord_transcript(channel, items)
        if items or debug_lines:
            message_parts: list[str] = []
            if items:
                logger.info(
                    "Voice log collected %d entries for channel %s",
                    len(items),
                    getattr(channel, "id", "unknown"),
                )
                message_parts.append(_format_voice_log_lines(channel, items))
            if stt_errors:
                message_parts.append("⚠️ STT error:\n" + "\n".join(stt_errors))
            if debug_enabled and debug_lines:
                message_parts.append("🧪 Voice log debug:\n" + "\n".join(debug_lines))
            message_text = "\n\n".join(message_parts)
            max_len = 3800
            if len(message_text) > max_len:
                message_text = message_text[: max_len - 20].rstrip() + "\n…(truncated)"
            await _send_admin_voice_log(message_text, audio_files=pending_audio_files)
    finally:
        try:
            await _maybe_send_daily_summary(channel)
        except Exception as exc:
            logger.warning("Daily summary failure: %s", exc)
        for audio in pending_audio_files:
            path = audio.get("path")
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    logger.warning("Failed to remove temp file %s", path)


async def _process_voice_log_entries_async(
    entries: list[dict[str, object]],
    voice_client: discord.VoiceClient,
) -> None:
    async with _voice_log_processing_semaphore:
        await _process_voice_log_entries(entries, voice_client)


async def _voice_log_callback(
    sink: discord.sinks.Sink,
    voice_client: discord.VoiceClient,
    done_event: asyncio.Event,
) -> None:
    done_event.set()
    asyncio.create_task(process_voice_log_sink(sink, voice_client))


async def _voice_log_loop(voice_client: discord.VoiceClient) -> None:
    interval = int(BOT_CONFIG.get("VOICE_LOG_INTERVAL_SECONDS", 10))
    auto_leave_seconds = int(BOT_CONFIG.get("VOICE_AUTO_LEAVE_SECONDS", 60))
    empty_since: datetime | None = None
    sink: RollingWaveSink | None = None

    while voice_client and voice_client.is_connected():
        channel = getattr(voice_client, "channel", None)
        humans = count_humans_in_voice(channel) if channel else 0
        if channel:
            members = getattr(channel, "members", None) or []
            logger.info(
                "Voice log loop humans=%s members=%s channel=%s",
                humans,
                ",".join(str(getattr(m, "id", "unknown")) for m in members),
                getattr(channel, "id", "unknown"),
            )
        if humans == 0:
            if empty_since is None:
                empty_since = datetime.now()
            elif (datetime.now() - empty_since).total_seconds() >= auto_leave_seconds:
                try:
                    if voice_client.recording:
                        await asyncio.to_thread(voice_client.stop_recording)
                except Exception as exc:
                    logger.warning("Failed to stop recording: %s", exc)
                if sink:
                    pending = sink.pop_chunks(finalize=True)
                    if pending:
                        asyncio.create_task(
                            _process_voice_log_entries_async(pending, voice_client)
                        )
                try:
                    guild = getattr(voice_client, "guild", None)
                    if guild:
                        cancel_voice_log_task(guild.id)
                        set_last_voice_channel(str(guild.id), None)
                    await voice_client.disconnect()
                except Exception as exc:
                    logger.warning("Failed to auto-leave (loop fallback): %s", exc)
                return
        else:
            empty_since = None

        recording = bool(getattr(voice_client, "recording", False))
        logger.info(
            "Voice log loop tick guild=%s channel=%s recording=%s",
            getattr(getattr(voice_client, "guild", None), "id", "unknown"),
            getattr(channel, "id", "unknown"),
            recording,
        )
        if not recording:
            sink = RollingWaveSink(interval_seconds=interval)
            done_event = asyncio.Event()
            try:
                logger.info(
                    "Voice log start recording guild=%s channel=%s",
                    getattr(getattr(voice_client, "guild", None), "id", "unknown"),
                    getattr(getattr(voice_client, "channel", None), "id", "unknown"),
                )
                voice_client.start_recording(
                    sink, _voice_log_capture_callback, voice_client, done_event
                )
            except Exception as exc:
                logger.warning("Voice log recording failed: %s", exc)
                await asyncio.sleep(1)
                continue

        if sink:
            pending = sink.pop_chunks()
            if pending:
                asyncio.create_task(
                    _process_voice_log_entries_async(pending, voice_client)
                )
        await asyncio.sleep(1)


def ensure_voice_log_task(voice_client: discord.VoiceClient) -> None:
    if not BOT_CONFIG.get("VOICE_LOG_ENABLED", True):
        return
    if not voice_client or not voice_client.is_connected():
        return
    guild_id = voice_client.guild.id if voice_client.guild else None
    if guild_id is None:
        return
    task = _voice_log_tasks.get(guild_id)
    if task and not task.done():
        return
    _voice_log_tasks[guild_id] = asyncio.create_task(_voice_log_loop(voice_client))


def cancel_voice_log_task(guild_id: int) -> None:
    task = _voice_log_tasks.pop(guild_id, None)
    if task:
        task.cancel()
