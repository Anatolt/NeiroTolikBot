import asyncio
import audioop
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import wave

import discord

from config import BOT_CONFIG
from discord_app.runtime import get_bot, get_telegram_bot
from services.analytics import log_stt_usage
from services.memory import (
    add_voice_log,
    get_all_admins,
    get_voice_log_debug,
    get_voice_log_model,
    get_voice_model,
    get_voice_transcribe_mode,
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


def _get_ffmpeg_path() -> str | None:
    for candidate in (shutil.which("ffmpeg"), "/usr/bin/ffmpeg", "/bin/ffmpeg"):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _stage_voice_log_audio(src_path: str) -> str | None:
    try:
        suffix = Path(src_path).suffix or ".wav"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
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


async def process_voice_log_sink(
    sink: discord.sinks.Sink, voice_client: discord.VoiceClient
) -> None:
    channel = getattr(voice_client, "channel", None)
    guild = getattr(channel, "guild", None)

    timeline_entries: list[dict[str, object]] = []
    debug_enabled = get_voice_log_debug()
    debug_lines: list[str] = []
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
            debug_prefix = f"user={username or user_id or user_key}"
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_file:
                tmp_path = tmp_file.name
                audio.file.seek(0)
                tmp_file.write(audio.file.read())

            with open(tmp_path, "rb") as verify_handle:
                header = verify_handle.read(12)
            if not (header.startswith(b"RIFF") and b"WAVE" in header):
                logger.warning("Skipping non-wav audio for user %s", user_id)
                _debug(f"{debug_prefix} skip=non_wav")
                continue

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
                segments = _split_long_segments(segments)
                _debug(f"{debug_prefix} send_mode=segmented segments={len(segments)}")

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
                        for part_idx, segment_path in enumerate(segment_paths, start=1):
                            staged_path = _stage_voice_log_audio(segment_path)
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
        finally:
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
        if items or debug_lines:
            message_parts: list[str] = []
            if items:
                logger.info(
                    "Voice log collected %d entries for channel %s",
                    len(items),
                    getattr(channel, "id", "unknown"),
                )
                message_parts.append(_format_voice_log_lines(channel, items))
            if debug_enabled and debug_lines:
                message_parts.append("🧪 Voice log debug:\n" + "\n".join(debug_lines))
            message_text = "\n\n".join(message_parts)
            max_len = 3800
            if len(message_text) > max_len:
                message_text = message_text[: max_len - 20].rstrip() + "\n…(truncated)"
            await _send_admin_voice_log(message_text, audio_files=pending_audio_files)
    finally:
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
    interval = int(BOT_CONFIG.get("VOICE_LOG_INTERVAL_SECONDS", 60))
    while voice_client and voice_client.is_connected():
        recording = bool(getattr(voice_client, "recording", False))
        logger.info(
            "Voice log loop tick guild=%s channel=%s recording=%s",
            getattr(getattr(voice_client, "guild", None), "id", "unknown"),
            getattr(getattr(voice_client, "channel", None), "id", "unknown"),
            recording,
        )
        if recording:
            await asyncio.sleep(1)
            continue
        sink = discord.sinks.WaveSink()
        done_event = asyncio.Event()
        try:
            logger.info(
                "Voice log start recording guild=%s channel=%s",
                getattr(getattr(voice_client, "guild", None), "id", "unknown"),
                getattr(getattr(voice_client, "channel", None), "id", "unknown"),
            )
            voice_client.start_recording(sink, _voice_log_callback, voice_client, done_event)
            await asyncio.sleep(interval)
        except Exception as exc:
            logger.warning("Voice log recording failed: %s", exc)
            await asyncio.sleep(interval)
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
                logger.warning("Voice log processing timed out")


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
