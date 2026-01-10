import asyncio
import audioop
import logging
import os
import shutil
import subprocess
import re
import tempfile
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

logger = logging.getLogger(__name__)

_voice_log_tasks: dict[int, asyncio.Task] = {}


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


def _sanitize_tmp_prefix(raw: str | None) -> str:
    if not raw:
        return "user"
    cleaned = re.sub(r"[^\w+-]+", "_", str(raw), flags=re.UNICODE).strip("_")
    if not cleaned:
        return "user"
    return cleaned[:32]


def _stage_voice_log_audio(src_path: str, prefix: str | None = None) -> str | None:
    try:
        suffix = Path(src_path).suffix or ".wav"
        safe_prefix = _sanitize_tmp_prefix(prefix)
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix, prefix=f"{safe_prefix}+tmp_"
        ) as tmp_file:
            staged_path = tmp_file.name
        shutil.copy2(src_path, staged_path)
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
            stats.update(
                {
                    "noise_floor": noise_floor,
                    "threshold": threshold,
                    "rms_max": max(rms_values),
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

            entries.append(
                {
                    "user_key": user_key,
                    "tmp_path": tmp_path,
                    "duration": duration,
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
    for entry in entries:
        user_key = entry["user_key"]
        tmp_path = entry["tmp_path"]
        if not tmp_path:
            continue
        user_id = getattr(user_key, "id", user_key)
        if user_id not in grouped:
            grouped[user_id] = []
            user_keys[user_id] = user_key
        grouped[user_id].append(tmp_path)

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

        sink = type("MergedSink", (), {"audio_data": audio_data})()
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
                    transcript, error = await transcribe_audio(segment_path)
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
                if len(transcript.strip()) < 3 and (end_sec - start_sec) < 0.6:
                    continue
                username = username or (member.display_name if member else str(user_id))
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


async def _voice_log_callback(
    sink: discord.sinks.Sink,
    voice_client: discord.VoiceClient,
    done_event: asyncio.Event,
) -> None:
    await process_voice_log_sink(sink, voice_client)
    done_event.set()


async def _voice_log_loop(voice_client: discord.VoiceClient) -> None:
    interval = int(BOT_CONFIG.get("VOICE_LOG_INTERVAL_SECONDS", 10))
    max_total = int(BOT_CONFIG.get("VOICE_LOG_MAX_SEGMENT_SECONDS", 60))
    tail_seconds = float(BOT_CONFIG.get("VOICE_LOG_PAUSE_TAIL_SECONDS", 0.6))
    auto_leave_seconds = int(BOT_CONFIG.get("VOICE_AUTO_LEAVE_SECONDS", 60))
    empty_since: datetime | None = None
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
        if recording:
            await asyncio.sleep(1)
            continue
        collected_entries: list[dict[str, object]] = []
        total_seconds = 0.0
        extend = True

        while extend and total_seconds < max_total:
            sink = discord.sinks.WaveSink()
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
                await asyncio.sleep(interval)
            except Exception as exc:
                logger.warning("Voice log recording failed: %s", exc)
                await asyncio.sleep(interval)
                extend = False
            finally:
                try:
                    logger.info(
                        "Voice log stop recording guild=%s channel=%s recording=%s",
                        getattr(getattr(voice_client, "guild", None), "id", "unknown"),
                        getattr(getattr(voice_client, "channel", None), "id", "unknown"),
                        getattr(voice_client, "recording", False),
                    )
                    await asyncio.to_thread(voice_client.stop_recording)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(done_event.wait(), timeout=30)
                except asyncio.TimeoutError:
                    logger.warning("Voice log capture timed out")
                    extend = False

            entries, extend = _collect_interval_entries(sink, tail_seconds)
            collected_entries.extend(entries)
            total_seconds += interval

        await _process_voice_log_entries(collected_entries, voice_client)


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
