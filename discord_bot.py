import asyncio
import audioop
import logging
import os
import shutil
import subprocess
import re
import tempfile
from datetime import datetime
from pathlib import Path
import wave

import discord
from discord.ext import commands
from dotenv import load_dotenv

from config import BOT_CONFIG
from handlers.message_service import MessageProcessingRequest, process_message_request
from services.analytics import log_stt_usage
from services.speech_to_text import estimate_transcription_cost, transcribe_audio, trim_silence
from services.generation import check_model_availability, init_client, refresh_models_from_api
from services.memory import (
    create_discord_join_request,
    get_all_admins,
    get_discord_autojoin,
    get_discord_autojoin_announce_sent,
    get_last_voice_channel,
    get_notification_flows_for_channel,
    get_unprocessed_discord_join_requests,
    get_voice_auto_reply,
    get_voice_log_debug,
    get_voice_log_model,
    get_voice_notification_chat_id,
    get_voice_model,
    init_db,
    mark_discord_join_request_processed,
    add_voice_log,
    set_last_voice_channel,
    set_discord_autojoin,
    set_discord_autojoin_announce_sent,
    set_voice_auto_reply,
    upsert_discord_voice_channel,
)
from discord_selftest import register_discord_selftest
from utils.helpers import resolve_system_prompt
from telegram import Bot

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

BOT_CONFIG["DISCORD_BOT_TOKEN"] = os.getenv("DISCORD_BOT_TOKEN")
BOT_CONFIG["TELEGRAM_BOT_TOKEN"] = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_CONFIG["OPENROUTER_API_KEY"] = os.getenv("OPENROUTER_API_KEY")
BOT_CONFIG["PIAPI_KEY"] = os.getenv("PIAPI_KEY")
BOT_CONFIG["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
BOT_CONFIG["IMAGE_ROUTER_KEY"] = os.getenv("IMAGE_ROUTER_KEY")
BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"] = resolve_system_prompt(BASE_DIR)
BOT_CONFIG["ADMIN_PASS"] = os.getenv("PASS")
BOT_CONFIG["BOOT_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
voice_prompt_env = os.getenv("VOICE_TRANSCRIBE_PROMPT")
if voice_prompt_env is not None:
    BOT_CONFIG["VOICE_TRANSCRIBE_PROMPT"] = voice_prompt_env
voice_local_url_env = os.getenv("VOICE_LOCAL_WHISPER_URL")
if voice_local_url_env is not None:
    BOT_CONFIG["VOICE_LOCAL_WHISPER_URL"] = voice_local_url_env

# Необязательная настройка кастомных запасных моделей (через запятую)
fallback_models_env = os.getenv("FALLBACK_MODELS")
if fallback_models_env:
    BOT_CONFIG["FALLBACK_MODELS"] = [model.strip() for model in fallback_models_env.split(",") if model.strip()]

COMMAND_PREFIXES = ("!", "/")

intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.dm_messages = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or(*COMMAND_PREFIXES),
    intents=intents,
    help_command=None,
)
register_discord_selftest(bot)
telegram_bot = Bot(BOT_CONFIG["TELEGRAM_BOT_TOKEN"]) if BOT_CONFIG.get("TELEGRAM_BOT_TOKEN") else None
_join_request_task: asyncio.Task | None = None
_voice_disconnect_tasks: dict[int, asyncio.Task] = {}
_VOICE_DISCONNECT_DELAY_SECONDS = 15
_VOICE_EMPTY_NOTIFY_DELAY_SECONDS = 300
_voice_empty_notify_tasks: dict[int, asyncio.Task] = {}
_pending_voice_transcripts: dict[tuple[str, str], str] = {}
_pending_voice_files: dict[tuple[str, str], dict] = {}
_voice_log_tasks: dict[int, asyncio.Task] = {}

# Инициализация клиентов и БД
init_client()
init_db()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def _extract_discord_channel_link(text: str) -> tuple[str, str] | None:
    match = re.search(r"https?://(?:www\.)?discord\.com/channels/(\d+)/(\d+)", text)
    if not match:
        return None
    return match.group(1), match.group(2)


def _extract_discord_invite_link(text: str) -> str | None:
    match = re.search(
        r"https?://(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]+)",
        text,
    )
    if not match:
        return None
    return match.group(1)


async def check_default_model():
    """Выбирает лучшую доступную модель и обновляет алиасы."""
    try:
        await refresh_models_from_api()
    except Exception as e:
        logger.error(f"Failed to refresh models from API: {str(e)}")

    models_to_probe = []
    for candidate in [BOT_CONFIG.get("DEFAULT_MODEL"), *BOT_CONFIG.get("FALLBACK_MODELS", [])]:
        if candidate and candidate not in models_to_probe:
            models_to_probe.append(candidate)

    for candidate in models_to_probe:
        if await check_model_availability(candidate):
            BOT_CONFIG["DEFAULT_MODEL"] = candidate
            logger.info(f"Using available default model: {candidate}")
            break
    else:
        logger.warning(
            f"No available models from the list {models_to_probe}. Falling back to openai/gpt-3.5-turbo"
        )
        BOT_CONFIG["DEFAULT_MODEL"] = "openai/gpt-3.5-turbo"


def _build_start_message(display_name: str | None) -> str:
    user = display_name or "там"
    default_model = BOT_CONFIG["DEFAULT_MODEL"]
    return (
        f"Привет, {user}! Я бот-помощник.\n\n"
        f"📝 Спроси меня что-нибудь — отвечу через {default_model}.\n"
        "🎨 Попроси нарисовать картинку (например, 'нарисуй закат над морем').\n"
        "🤖 Хочешь другую модель? Укажи ее в начале или конце запроса (например, 'chatgpt какой сегодня день?').\n"
        "❓ Команды и помощь: /help"
    )


def _build_discord_help_message() -> str:
    return (
        "Команды Discord-бота:\n"
        "• /start — краткое приветствие\n"
        "• /help — справка по командам\n"
        "• /join — подключиться к голосовому каналу, где вы сейчас\n"
        "• /leave — выйти из голосового канала\n"
        "• /autojoin_on — включить автоподключение к голосу\n"
        "• /autojoin_off — отключить автоподключение к голосу\n\n"
        "В серверах бот отвечает по упоминанию @ИмяБота или с префиксами !/.\n"
        "В личных сообщениях отвечает на любой текст."
    )


def _strip_bot_mention(content: str, bot_user: discord.User | discord.ClientUser | None) -> str:
    if not bot_user:
        return content

    cleaned = content
    mention_variants = [f"<@{bot_user.id}>", f"<@!{bot_user.id}>", f"@{bot_user.name}"]
    for mention in mention_variants:
        cleaned = cleaned.replace(mention, "")
    return cleaned.strip()


async def _send_responses(message: discord.Message, clean_content: str) -> None:
    request = MessageProcessingRequest(
        text=clean_content,
        chat_id=str(message.channel.id),
        user_id=str(message.author.id),
        bot_username=bot.user.name if bot.user else None,
        username=str(message.author),
        platform="discord",
    )

    async def _ack() -> None:
        await message.channel.send("✅ Принял запрос, думаю...")

    responses = await process_message_request(request, ack_callback=_ack)

    for response in responses:
        if response.photo_url:
            await message.channel.send(response.photo_url)
        elif response.text:
            await message.channel.send(response.text)


async def _handle_voice_confirmation(message: discord.Message) -> bool:
    content = (message.content or "").strip().lower()
    if content.startswith("/"):
        content = content[1:]

    if content not in {"yes", "y"}:
        return False

    key = (str(message.channel.id), str(message.author.id))

    file_entry = _pending_voice_files.pop(key, None)
    if file_entry:
        file_path = file_entry.get("path")
        if not file_path or not os.path.exists(file_path):
            return True
        await message.channel.send("Ок, распознаю голосовое...")
        transcript, error = await transcribe_audio(file_path)
        try:
            os.unlink(file_path)
        except OSError:
            logger.warning("Failed to remove temp file %s", file_path)
        if transcript:
            log_stt_usage(
                platform="discord",
                chat_id=str(message.channel.id),
                user_id=str(message.author.id),
                duration_seconds=file_entry.get("duration"),
                size_bytes=file_entry.get("size_bytes"),
            )
        await _handle_transcript_result(message, transcript, error)
        return True

    transcript = _pending_voice_transcripts.pop(key, None)
    if not transcript:
        return False

    await _send_responses(message, transcript)
    if not get_voice_auto_reply(str(message.channel.id), str(message.author.id)):
        await message.channel.send(
            "Можно перейти в режим диалога, чтобы я не переспрашивал отвечать ли на голосовухи: "
            "/voice_msg_conversation_on"
        )
    return True


async def _handle_transcript_result(
    message: discord.Message, transcript: str | None, error: str | None
) -> bool:
    if not transcript:
        await message.channel.send("Не удалось распознать голосовое сообщение.")
        if error:
            logger.warning("Discord audio STT error: %s", error)
        return False

    await message.channel.send(f"Текст голосового:\n{transcript}")

    if get_voice_auto_reply(str(message.channel.id), str(message.author.id)):
        await _send_responses(message, transcript)
        return True

    key = (str(message.channel.id), str(message.author.id))
    _pending_voice_transcripts[key] = transcript
    await message.channel.send("Нужен ответ? /yes")
    return True


def _format_cost_estimate(cost: float | None) -> str:
    if cost is None:
        return "неизвестно"
    return f"${cost:.4f}"


def _sync_discord_voice_channels() -> None:
    for guild in bot.guilds:
        for channel in list(guild.voice_channels) + list(guild.stage_channels):
            upsert_discord_voice_channel(
                channel_id=str(channel.id),
                channel_name=channel.name,
                guild_id=str(guild.id),
                guild_name=guild.name,
            )


async def _send_admin_voice_log(
    text: str,
    audio_files: list[dict[str, str]] | None = None,
) -> None:
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

async def _process_voice_log_sink(
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

            segments, seg_stats = _detect_speech_segments(tmp_path)
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
                    audio_path, converted, convert_error = await _convert_voice_log_audio(segment_wav)
                    if converted:
                        converted_path = audio_path
                        _debug(f"{debug_prefix} seg{seg_idx} converted=mp3")
                    elif convert_error:
                        _debug(f"{debug_prefix} seg{seg_idx} convert_error={convert_error}")

                    size_bytes = os.path.getsize(audio_path)
                    max_mb = BOT_CONFIG.get("VOICE_TRANSCRIBE_MAX_MB", 10)
                    max_bytes = int(max_mb * 1024 * 1024)
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

                    voice_model = (
                        get_voice_log_model()
                        or get_voice_model()
                        or BOT_CONFIG.get("VOICE_MODEL")
                        or "whisper-1"
                    )
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
    await _process_voice_log_sink(sink, voice_client)
    done_event.set()


async def _voice_log_loop(voice_client: discord.VoiceClient) -> None:
    interval = int(BOT_CONFIG.get("VOICE_LOG_INTERVAL_SECONDS", 60))
    while voice_client and voice_client.is_connected():
        if getattr(voice_client, "recording", False):
            await asyncio.sleep(1)
            continue
        sink = discord.sinks.WaveSink()
        done_event = asyncio.Event()
        try:
            voice_client.start_recording(sink, _voice_log_callback, voice_client, done_event)
            await asyncio.sleep(interval)
        except Exception as exc:
            logger.warning("Voice log recording failed: %s", exc)
            await asyncio.sleep(interval)
        finally:
            try:
                await asyncio.to_thread(voice_client.stop_recording)
            except Exception:
                pass
            try:
                await asyncio.wait_for(done_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning("Voice log processing timed out")


def _ensure_voice_log_task(voice_client: discord.VoiceClient) -> None:
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


async def _send_telegram_notification(text: str, discord_channel_id: str | None = None) -> None:
    if not telegram_bot:
        logger.warning("Telegram bot token not configured, cannot send notifications.")
        return

    sent_chat_ids: set[str] = set()

    async def _send(chat_id: str) -> None:
        if chat_id in sent_chat_ids:
            return
        sent_chat_ids.add(chat_id)
        try:
            await telegram_bot.send_message(chat_id=int(chat_id), text=text)
        except Exception as exc:
            logger.warning("Failed to send Telegram notification to chat %s: %s", chat_id, exc)

    flow_chat_ids: list[str] = []
    if discord_channel_id:
        flows = get_notification_flows_for_channel(discord_channel_id)
        flow_chat_ids = [str(flow["telegram_chat_id"]) for flow in flows if flow.get("telegram_chat_id")]
        for chat_id in flow_chat_ids:
            await _send(chat_id)

    chat_id = get_voice_notification_chat_id()
    if not chat_id or flow_chat_ids:
        if not flow_chat_ids and not chat_id:
            logger.info("No admins or flow/voice notification chat configured.")
        return

    await _send(str(chat_id))


async def _send_telegram_join_request(request_id: int, guild_name: str, user_name: str) -> None:
    if not telegram_bot:
        logger.warning("Telegram bot token not configured, cannot send join request.")
        return

    admins = get_all_admins()
    if not admins:
        logger.warning("No admins configured; join request cannot be delivered.")
        return

    text = (
        "Просят присоединиться к Discord.\n"
        f"Сервер: {guild_name}\n"
        f"Пользователь: {user_name}\n"
        f"Запрос: {request_id}\n\n"
        "Ответьте: yes или no (можно с номером, например: yes 12)."
    )

    for admin in admins:
        chat_id = admin.get("chat_id")
        if not chat_id:
            continue
        try:
            await telegram_bot.send_message(chat_id=int(chat_id), text=text)
        except Exception as exc:
            logger.warning("Failed to send join request to admin %s: %s", chat_id, exc)


async def _notify_discord_user(user_id: int, text: str) -> None:
    try:
        user = await bot.fetch_user(user_id)
        await user.send(text)
    except Exception as exc:
        logger.warning("Failed to notify Discord user %s: %s", user_id, exc)


async def _process_join_requests_loop() -> None:
    while True:
        requests = get_unprocessed_discord_join_requests()
        for request in requests:
            try:
                request_id = int(request["id"])
                status = request.get("status")
                channel_id_raw = str(request.get("discord_channel_id", ""))
                user_id = int(request["discord_user_id"])
                guild_name = request.get("discord_guild_name") or "Discord"

                if not channel_id_raw.isdigit():
                    if status == "approved":
                        await _notify_discord_user(
                            user_id,
                            "Админ разрешил. Пригласите меня на сервер «{guild_name}»: "
                            "https://discord.com/oauth2/authorize?client_id=1451265052978974931&permissions=3147776&scope=bot%20applications.commands",
                        )
                    elif status == "denied":
                        await _notify_discord_user(user_id, "Админ отказал в подключении.")

                    mark_discord_join_request_processed(request_id)
                    continue

                channel_id = int(channel_id_raw)

                channel = bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await bot.fetch_channel(channel_id)
                    except Exception:
                        channel = None

                if status == "approved":
                    if channel is None:
                        await _notify_discord_user(user_id, "Админ разрешил, но я не нашёл канал.")
                    elif channel.type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                        await _notify_discord_user(user_id, "Админ разрешил, но это не голосовой канал.")
                    else:
                        try:
                            voice_client = await _connect_voice_channel(channel)
                            if voice_client:
                                _ensure_voice_log_task(voice_client)
                            set_last_voice_channel(str(channel.guild.id), str(channel.id))
                            await _notify_discord_user(user_id, "Админ разрешил. Подключаюсь.")
                        except Exception as exc:
                            await _notify_discord_user(user_id, "Админ разрешил, но не смог подключиться.")
                            logger.warning("Failed to join voice channel: %s", exc)
                elif status == "denied":
                    await _notify_discord_user(user_id, "Админ отказал в подключении.")

                mark_discord_join_request_processed(request_id)
            except Exception as exc:
                logger.warning("Failed to process join request: %s", exc)

        await asyncio.sleep(3)


async def _disconnect_if_empty(guild_id: int) -> None:
    await asyncio.sleep(_VOICE_DISCONNECT_DELAY_SECONDS)
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    voice_client = guild.voice_client
    if not voice_client or not voice_client.is_connected():
        return
    channel = voice_client.channel
    if not channel:
        return
    humans = [m for m in channel.members if not m.bot]
    if not humans:
        try:
            task = _voice_log_tasks.pop(guild_id, None)
            if task:
                task.cancel()
            await voice_client.disconnect()
        except Exception as exc:
            logger.warning("Failed to auto-leave voice channel: %s", exc)


async def _notify_if_voice_empty(channel_id: int, guild_id: int) -> None:
    await asyncio.sleep(_VOICE_EMPTY_NOTIFY_DELAY_SECONDS)
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            channel = None
    if channel is None:
        return
    if channel.type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
        return
    humans = [m for m in channel.members if not m.bot]
    if humans:
        return
    guild_name = guild.name or "Discord"
    notification = (
        f"🎧 Все вышли из голосового канала «{channel.name}» ({guild_name}). "
        "Уже 5 минут никого нет."
    )
    await _send_telegram_notification(notification, discord_channel_id=str(channel.id))


def _cleanup_voice_empty_task(channel_id: int, task: asyncio.Task) -> None:
    if _voice_empty_notify_tasks.get(channel_id) is task:
        _voice_empty_notify_tasks.pop(channel_id, None)


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    logger.info("Joined new guild: %s (%s)", guild.name, guild.id)
    _sync_discord_voice_channels()
    set_discord_autojoin_announce_sent(str(guild.id), False)


async def _handle_dm_message(message: discord.Message, clean_content: str) -> None:
    await _send_responses(message, clean_content)


async def _handle_guild_message(message: discord.Message, clean_content: str) -> None:
    bot_mentioned = bot.user is not None and bot.user.mentioned_in(message)
    has_prefix = message.content.startswith(COMMAND_PREFIXES)

    if not bot_mentioned and not has_prefix:
        return

    filtered_content = _strip_bot_mention(clean_content, bot.user)
    if has_prefix:
        for prefix in COMMAND_PREFIXES:
            if filtered_content.startswith(prefix):
                filtered_content = filtered_content[len(prefix) :].strip()
                break

    if not filtered_content:
        return

    await _send_responses(message, filtered_content)


def _pick_announcement_channel(guild: discord.Guild) -> discord.TextChannel | None:
    channel = guild.system_channel
    if channel and channel.permissions_for(guild.me).send_messages:  # type: ignore[arg-type]
        return channel

    for text_channel in guild.text_channels:
        if text_channel.permissions_for(guild.me).send_messages:  # type: ignore[arg-type]
            return text_channel

    return None


def _count_humans_in_voice(
    channel: discord.abc.GuildChannel, exclude_member_id: int | None = None
) -> int:
    guild = getattr(channel, "guild", None)
    if guild:
        count = 0
        voice_states = getattr(guild, "voice_states", None)
        if voice_states is None:
            voice_states = getattr(guild, "_voice_states", None)
        if not voice_states:
            voice_states = {}
        for member_id, voice_state in voice_states.items():
            if not voice_state or not voice_state.channel:
                continue
            if voice_state.channel.id != channel.id:
                continue
            if exclude_member_id is not None and member_id == exclude_member_id:
                continue
            member = guild.get_member(member_id)
            if member and member.bot:
                continue
            count += 1
        return count
    members = getattr(channel, "members", None) or []
    return sum(
        1
        for member in members
        if not member.bot and (exclude_member_id is None or member.id != exclude_member_id)
    )


async def _connect_voice_channel(channel: discord.VoiceChannel | discord.StageChannel) -> discord.VoiceClient | None:
    voice_client = channel.guild.voice_client
    if voice_client and voice_client.is_connected():
        await voice_client.move_to(channel)
    else:
        voice_client = await channel.connect()

    try:
        await channel.guild.change_voice_state(channel=channel, self_deaf=False, self_mute=False)
    except Exception as exc:
        logger.warning("Failed to set voice state for receive: %s", exc)

    return voice_client


@bot.event
async def on_ready():
    logger.info("Discord bot connected as %s (id=%s)", bot.user, bot.user.id if bot.user else "n/a")
    _sync_discord_voice_channels()
    if hasattr(bot, "sync_commands"):
        try:
            guild_ids = [guild.id for guild in bot.guilds] if bot.guilds else None
            await bot.sync_commands(force=True, guild_ids=guild_ids)
            logger.info("Discord app commands synced.")
        except Exception as exc:
            logger.warning("Failed to sync Discord app commands: %s", exc)

    global _join_request_task
    if _join_request_task is None or _join_request_task.done():
        _join_request_task = asyncio.create_task(_process_join_requests_loop())

    for guild in bot.guilds:
        last_channel_id = get_last_voice_channel(str(guild.id))
        if not last_channel_id:
            continue
        try:
            channel = guild.get_channel(int(last_channel_id))
        except (TypeError, ValueError):
            channel = None
        if channel is None or channel.type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
            continue
        voice_client = guild.voice_client
        if voice_client and voice_client.is_connected():
            try:
                await voice_client.disconnect()
            except Exception as exc:
                logger.warning("Failed to disconnect before reconnect: %s", exc)
        try:
            voice_client = await _connect_voice_channel(channel)
            if voice_client:
                _ensure_voice_log_task(voice_client)
            logger.info("Reconnected to voice channel %s in guild %s", channel.id, guild.id)
        except Exception as exc:
            logger.warning("Failed to reconnect to voice channel %s: %s", channel.id, exc)


@bot.command(name="start")
async def start_command(ctx: commands.Context) -> None:
    await ctx.send(_build_start_message(ctx.author.display_name))


@bot.command(name="help")
async def help_command(ctx: commands.Context) -> None:
    await ctx.send(_build_discord_help_message())


if hasattr(bot, "slash_command"):
    @bot.slash_command(name="help", description="Справка по командам бота")
    async def help_slash(ctx: discord.ApplicationContext) -> None:
        await ctx.respond(_build_discord_help_message())


@bot.command(name="join")
async def join_voice_command(ctx: commands.Context) -> None:
    """Join the caller's voice channel."""
    voice_state = getattr(ctx.author, "voice", None)
    if not voice_state or not voice_state.channel:
        await ctx.send("Сначала зайди в голосовой канал.")
        return

    channel = voice_state.channel
    voice_client = ctx.voice_client

    if voice_client and voice_client.is_connected() and voice_client.channel.id == channel.id:
        await ctx.send(f"Уже в канале «{channel.name}».")
        return

    voice_client = await _connect_voice_channel(channel)
    if voice_client:
        _ensure_voice_log_task(voice_client)
    set_last_voice_channel(str(ctx.guild.id), str(channel.id))
    await ctx.send(f"Подключился к «{channel.name}».")


if hasattr(bot, "slash_command"):
    @bot.slash_command(name="join", description="Подключиться к голосовому каналу")
    async def join_voice_slash(ctx: discord.ApplicationContext) -> None:
        if not ctx.guild:
            await ctx.respond("Команда доступна только на сервере.")
            return

        voice_state = getattr(ctx.author, "voice", None)
        if not voice_state or not voice_state.channel:
            await ctx.respond("Сначала зайди в голосовой канал.")
            return

        channel = voice_state.channel
        voice_client = ctx.guild.voice_client
        if voice_client and voice_client.is_connected() and voice_client.channel.id == channel.id:
            await ctx.respond(f"Уже в канале «{channel.name}».")
            return

        voice_client = await _connect_voice_channel(channel)
        if voice_client:
            _ensure_voice_log_task(voice_client)
        set_last_voice_channel(str(ctx.guild.id), str(channel.id))
        await ctx.respond(f"Подключился к «{channel.name}».")


@bot.command(name="leave")
async def leave_voice_command(ctx: commands.Context) -> None:
    """Leave the current voice channel."""
    voice_client = ctx.voice_client
    if not voice_client or not voice_client.is_connected():
        await ctx.send("Я сейчас не в голосовом канале.")
        return

    await voice_client.disconnect()
    task = _voice_log_tasks.pop(ctx.guild.id, None) if ctx.guild else None
    if task:
        task.cancel()
    if ctx.guild:
        set_last_voice_channel(str(ctx.guild.id), None)
    await ctx.send("Отключился от голосового канала.")


if hasattr(bot, "slash_command"):
    @bot.slash_command(name="leave", description="Выйти из голосового канала")
    async def leave_voice_slash(ctx: discord.ApplicationContext) -> None:
        if not ctx.guild:
            await ctx.respond("Команда доступна только на сервере.")
            return

        voice_client = ctx.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            await ctx.respond("Я сейчас не в голосовом канале.")
            return

        await voice_client.disconnect()
        task = _voice_log_tasks.pop(ctx.guild.id, None) if ctx.guild else None
        if task:
            task.cancel()
        if ctx.guild:
            set_last_voice_channel(str(ctx.guild.id), None)
        await ctx.respond("Отключился от голосового канала.")


@bot.command(name="autojoin_on")
async def autojoin_on_command(ctx: commands.Context) -> None:
    """Enable auto-join for this guild."""
    if not ctx.guild:
        await ctx.send("Команда доступна только на сервере.")
        return

    set_discord_autojoin(str(ctx.guild.id), True)
    set_discord_autojoin_announce_sent(str(ctx.guild.id), False)
    await ctx.send("Автоподключение включено.")


if hasattr(bot, "slash_command"):
    @bot.slash_command(name="autojoin_on", description="Включить автоподключение к голосу")
    async def autojoin_on_slash(ctx: discord.ApplicationContext) -> None:
        if not ctx.guild:
            await ctx.respond("Команда доступна только на сервере.")
            return

        set_discord_autojoin(str(ctx.guild.id), True)
        set_discord_autojoin_announce_sent(str(ctx.guild.id), False)
        await ctx.respond("Автоподключение включено.")


@bot.command(name="autojoin_off")
async def autojoin_off_command(ctx: commands.Context) -> None:
    """Disable auto-join for this guild."""
    if not ctx.guild:
        await ctx.send("Команда доступна только на сервере.")
        return

    set_discord_autojoin(str(ctx.guild.id), False)
    await ctx.send("Автоподключение отключено.")


@bot.command(name="voice_msg_conversation_on")
async def voice_msg_conversation_on_command(ctx: commands.Context) -> None:
    set_voice_auto_reply(str(ctx.channel.id), str(ctx.author.id), True)
    await ctx.send(
        "🔊 Автоответ на голосовые сообщения включён.\n"
        "Отключить: /voice_msg_conversation_off"
    )


@bot.command(name="voice_msg_conversation_off")
async def voice_msg_conversation_off_command(ctx: commands.Context) -> None:
    set_voice_auto_reply(str(ctx.channel.id), str(ctx.author.id), False)
    await ctx.send(
        "🔇 Автоответ на голосовые сообщения отключён.\n"
        "Включить: /voice_msg_conversation_on"
    )


if hasattr(bot, "slash_command"):
    @bot.slash_command(name="voice_msg_conversation_on", description="Включить автоответ на голосовые сообщения")
    async def voice_msg_conversation_on_slash(ctx: discord.ApplicationContext) -> None:
        set_voice_auto_reply(str(ctx.channel.id), str(ctx.author.id), True)
        await ctx.respond(
            "🔊 Автоответ на голосовые сообщения включён.\n"
            "Отключить: /voice_msg_conversation_off"
        )


    @bot.slash_command(name="voice_msg_conversation_off", description="Отключить автоответ на голосовые сообщения")
    async def voice_msg_conversation_off_slash(ctx: discord.ApplicationContext) -> None:
        set_voice_auto_reply(str(ctx.channel.id), str(ctx.author.id), False)
        await ctx.respond(
            "🔇 Автоответ на голосовые сообщения отключён.\n"
            "Включить: /voice_msg_conversation_on"
        )


    @bot.slash_command(name="autojoin_off", description="Отключить автоподключение к голосу")
    async def autojoin_off_slash(ctx: discord.ApplicationContext) -> None:
        if not ctx.guild:
            await ctx.respond("Команда доступна только на сервере.")
            return

        set_discord_autojoin(str(ctx.guild.id), False)
        await ctx.respond("Автоподключение отключено.")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    content = message.content or ""
    is_dm = message.guild is None

    if is_dm and content:
        link = _extract_discord_channel_link(content)
        if link:
            guild_id, channel_id = link
            channel = bot.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await bot.fetch_channel(int(channel_id))
                except Exception:
                    channel = None

            if channel is None or not getattr(channel, "guild", None):
                await message.channel.send("Не вижу такой канал или у меня нет доступа.")
                return

            if str(channel.guild.id) != guild_id:
                await message.channel.send("Ссылка не совпадает с сервером канала.")
                return

            if channel.type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                await message.channel.send("Это не голосовой канал.")
                return

            await message.channel.send(
                "Вижу ссылку на Discord. Пошёл спрашивать у админа, можно ли мне присоединиться."
            )

            request_id = create_discord_join_request(
                discord_user_id=str(message.author.id),
                discord_user_name=str(message.author),
                discord_guild_id=str(channel.guild.id),
                discord_guild_name=channel.guild.name,
                discord_channel_id=str(channel.id),
                discord_channel_name=getattr(channel, "name", str(channel.id)),
            )
            await _send_telegram_join_request(request_id, channel.guild.name, str(message.author))
            return

        invite_code = _extract_discord_invite_link(content)
        if invite_code:
            invite = None
            try:
                invite = await bot.fetch_invite(invite_code)
            except Exception as exc:
                logger.warning("Failed to fetch invite %s: %s", invite_code, exc)

            if invite and invite.guild:
                guild_name = invite.guild.name
                guild_id = str(invite.guild.id)
            else:
                guild_name = "неизвестный сервер"
                guild_id = "unknown"

            channel_id = f"invite:{invite_code}"
            channel_name = "invite"
            if invite and invite.channel:
                channel_name = getattr(invite.channel, "name", "invite")
                if invite.channel.type in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
                    channel_id = str(invite.channel.id)

            await message.channel.send(
                "Вижу ссылку на Discord. Пошёл спрашивать у админа, можно ли мне присоединиться."
            )

            request_id = create_discord_join_request(
                discord_user_id=str(message.author.id),
                discord_user_name=str(message.author),
                discord_guild_id=guild_id,
                discord_guild_name=guild_name,
                discord_channel_id=channel_id,
                discord_channel_name=channel_name,
            )
            await _send_telegram_join_request(request_id, guild_name, str(message.author))
            return

    if await _handle_voice_confirmation(message):
        return

    if message.attachments:
        audio_attachment = None
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("audio/"):
                audio_attachment = attachment
                break
            if attachment.filename.lower().endswith((".ogg", ".mp3", ".wav", ".m4a")):
                audio_attachment = attachment
                break

        if audio_attachment:
            await message.channel.send("Распознаю голосовое сообщение...")

            tmp_path = None
            size_bytes = None
            try:
                suffix = ""
                if audio_attachment.filename and "." in audio_attachment.filename:
                    suffix = "." + audio_attachment.filename.rsplit(".", 1)[-1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".ogg") as tmp_file:
                    tmp_path = tmp_file.name
                await audio_attachment.save(tmp_path)

                trimmed_path, trimmed = trim_silence(tmp_path)
                if trimmed:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        logger.warning("Failed to remove temp file %s", tmp_path)
                    tmp_path = trimmed_path

                size_bytes = os.path.getsize(tmp_path)
                max_mb = BOT_CONFIG.get("VOICE_TRANSCRIBE_MAX_MB", 10)
                confirm_mb = BOT_CONFIG.get("VOICE_TRANSCRIBE_CONFIRM_MB", 5)
                max_bytes = int(max_mb * 1024 * 1024)
                confirm_bytes = int(confirm_mb * 1024 * 1024)

                if size_bytes > max_bytes:
                    await message.channel.send("Файл слишком большой для распознавания (лимит 10 МБ).")
                    return

                if size_bytes >= confirm_bytes:
                    cost = estimate_transcription_cost(None, size_bytes)
                    key = (str(message.channel.id), str(message.author.id))
                    _pending_voice_files[key] = {
                        "path": tmp_path,
                        "size_bytes": size_bytes,
                    }
                    await message.channel.send(
                        f"Файл большой, распознавание будет стоить примерно {_format_cost_estimate(cost)}. "
                        "Отправлять? /yes"
                    )
                    tmp_path = None
                    return

                transcript, error = await transcribe_audio(tmp_path)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        logger.warning("Failed to remove temp file %s", tmp_path)

            if transcript:
                log_stt_usage(
                    platform="discord",
                    chat_id=str(message.channel.id),
                    user_id=str(message.author.id),
                    duration_seconds=None,
                    size_bytes=size_bytes,
                )
            await _handle_transcript_result(message, transcript, error)
            return

    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return
    if content.startswith(COMMAND_PREFIXES):
        await bot.process_commands(message)
        return

    if is_dm:
        if content:
            await _handle_dm_message(message, content)
    else:
        await _handle_guild_message(message, content)

    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.bot:
        return
    if after.channel is not None:
        existing_task = _voice_empty_notify_tasks.pop(after.channel.id, None)
        if existing_task and not existing_task.done():
            existing_task.cancel()

    if before.channel is None and after.channel is not None:
        channel = after.channel
        guild_name = channel.guild.name if channel.guild else "Discord"
        others_count = _count_humans_in_voice(channel, exclude_member_id=member.id)
        notification = (
            f"🎧 {member.display_name} подключился к голосовому каналу "
            f"«{channel.name}» ({guild_name}). "
            f"Сейчас в чате ещё {others_count} чел."
        )
        await _send_telegram_notification(notification, discord_channel_id=str(channel.id))

        if channel.guild and get_discord_autojoin(str(channel.guild.id)):
            voice_client = channel.guild.voice_client
            if voice_client is None or not voice_client.is_connected():
                try:
                    voice_client = await _connect_voice_channel(channel)
                    if voice_client:
                        _ensure_voice_log_task(voice_client)
                    set_last_voice_channel(str(channel.guild.id), str(channel.id))
                    if not get_discord_autojoin_announce_sent(str(channel.guild.id)):
                        announce_channel = _pick_announcement_channel(channel.guild)
                        if announce_channel:
                            await announce_channel.send(
                                f"Подключился к голосовому каналу «{channel.name}», "
                                "т.к. кто-то в него зашёл.\n"
                                "Чтобы я вышел, напишите /leave.\n"
                                "Чтобы я не подключался автоматически, напишите /autojoin_off.\n"
                                "Чтобы снова включить автоподключение, напишите /autojoin_on."
                            )
                        set_discord_autojoin_announce_sent(str(channel.guild.id), True)
                except Exception as exc:
                    logger.warning("Failed to auto-join voice channel: %s", exc)

    voice_client = member.guild.voice_client
    guild_id = member.guild.id
    if voice_client and voice_client.is_connected():
        channel = voice_client.channel
        if channel:
            humans = [m for m in channel.members if not m.bot]
            existing_task = _voice_disconnect_tasks.pop(guild_id, None)
            if existing_task and not existing_task.done():
                existing_task.cancel()
            if not humans:
                _voice_disconnect_tasks[guild_id] = asyncio.create_task(
                    _disconnect_if_empty(guild_id)
                )

    if before.channel is not None and before.channel != after.channel:
        channel = before.channel
        humans = [m for m in channel.members if not m.bot]
        existing_task = _voice_empty_notify_tasks.pop(channel.id, None)
        if existing_task and not existing_task.done():
            existing_task.cancel()
        if not humans:
            task = asyncio.create_task(
                _notify_if_voice_empty(channel.id, channel.guild.id)
            )
            _voice_empty_notify_tasks[channel.id] = task
            task.add_done_callback(lambda t, cid=channel.id: _cleanup_voice_empty_task(cid, t))


async def main() -> None:
    if not BOT_CONFIG["DISCORD_BOT_TOKEN"] or not BOT_CONFIG["OPENROUTER_API_KEY"]:
        logger.error("Please set DISCORD_BOT_TOKEN and OPENROUTER_API_KEY in .env file")
        return

    await check_default_model()

    async with bot:
        await bot.start(BOT_CONFIG["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Discord bot stopped by user")
    except Exception as e:
        logger.error(f"Error running Discord bot: {str(e)}")
