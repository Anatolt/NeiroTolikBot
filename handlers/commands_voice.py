import asyncio
import logging
import os
import shutil
import subprocess
import tempfile

from telegram import Update
from telegram.ext import ContextTypes

from config import BOT_CONFIG
from handlers.commands_utils import is_admin_user
from services.memory import (
    get_discord_voice_channels,
    get_last_voice_alerts_toggle,
    get_notification_flows,
    get_voice_chunk_notifications_enabled,
    get_tts_voice,
    get_tts_provider,
    get_voice_presence_notifications_enabled,
    log_voice_alerts_toggle,
    set_voice_chunk_notifications_enabled,
    set_tts_provider,
    set_tts_voice,
    set_voice_log_debug,
    set_voice_log_model,
    set_voice_presence_notifications_enabled,
    set_voice_model,
    set_voice_transcribe_mode,
    set_voice_auto_reply,
)
from services.tts import synthesize_speech

logger = logging.getLogger(__name__)


def _get_ffmpeg_path() -> str | None:
    for candidate in (shutil.which("ffmpeg"), "/usr/bin/ffmpeg", "/bin/ffmpeg"):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _guild_options_for_chat(chat_id: str) -> dict[str, str]:
    channels = get_discord_voice_channels()
    channel_map = {str(item.get("channel_id")): item for item in channels}
    flows = get_notification_flows()
    options: dict[str, str] = {}
    for flow in flows:
        if str(flow.get("telegram_chat_id")) != str(chat_id):
            continue
        channel_info = channel_map.get(str(flow.get("discord_channel_id")))
        if not channel_info:
            continue
        guild_id = str(channel_info.get("guild_id") or "").strip()
        if not guild_id:
            continue
        guild_name = str(channel_info.get("guild_name") or guild_id).strip() or guild_id
        options[guild_id] = guild_name
    return options


async def _resolve_voice_alerts_guild(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command_name: str
) -> tuple[str | None, str | None]:
    message = update.message
    if not message:
        return None, None

    options = _guild_options_for_chat(str(update.effective_chat.id))
    ordered_options = sorted(options.items(), key=lambda item: item[1].lower())
    all_channels = get_discord_voice_channels()
    all_guilds: dict[str, str] = {}
    for item in all_channels:
        guild_id = str(item.get("guild_id") or "").strip()
        if not guild_id:
            continue
        guild_name = str(item.get("guild_name") or guild_id).strip() or guild_id
        all_guilds[guild_id] = guild_name
    ordered_all = sorted(all_guilds.items(), key=lambda item: item[1].lower())
    args = context.args or []
    if args:
        raw_arg = args[0].strip()
        if not raw_arg.isdigit():
            await message.reply_text(
                f"Неверный аргумент. Пример: /{command_name} 123456789012345678 или /{command_name} 1"
            )
            return None, None

        if raw_arg in all_guilds:
            return raw_arg, all_guilds[raw_arg]

        index = int(raw_arg)
        if 1 <= index <= len(ordered_all):
            guild_id, guild_name = ordered_all[index - 1]
            return guild_id, guild_name

        await message.reply_text(
            f"Сервер не найден. Укажи guild_id или номер из списка.\n"
            f"Пример: /{command_name} 123456789012345678 или /{command_name} 1\n"
            "Список: /show_discord_chats"
        )
        return None, None

    if len(options) == 1:
        guild_id, guild_name = next(iter(options.items()))
        return guild_id, guild_name

    if not options:
        await message.reply_text(
            "Не удалось определить сервер для этого чата.\n"
            "Укажи guild_id явно: /voice_alerts_off <guild_id>\n"
            "Список серверов: /show_discord_chats"
        )
        return None, None

    lines = [
        "У этого Telegram-чата несколько серверов. Укажи guild_id или номер:",
    ]
    for idx, (guild_id, guild_name) in enumerate(ordered_options, start=1):
        lines.append(f"{idx}) {guild_name} — {guild_id}")
    lines.append(f"Пример: /{command_name} <guild_id> или /{command_name} <номер>")
    await message.reply_text("\n".join(lines))
    return None, None


async def _convert_tts_to_ogg(src_path: str) -> tuple[str | None, str | None]:
    ffmpeg_path = _get_ffmpeg_path()
    if not ffmpeg_path:
        return None, "ffmpeg_missing"

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
            dst_path = tmp_file.name

        cmd = [
            ffmpeg_path,
            "-y",
            "-i",
            src_path,
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "libopus",
            dst_path,
        ]
        result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if result.returncode != 0:
            try:
                os.unlink(dst_path)
            except OSError:
                logger.warning("Failed to remove temp file %s", dst_path)
            return None, result.stderr.strip() or "convert_failed"
        return dst_path, None
    except Exception as exc:
        logger.warning("Failed to convert TTS audio: %s", exc)
        return None, str(exc)


async def set_voice_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меняет модель распознавания речи."""
    voice_models = BOT_CONFIG.get("VOICE_MODELS", [])
    if not voice_models:
        await update.message.reply_text("Список моделей распознавания речи пуст.")
        return

    args = context.args or []
    if not args or not args[0].isdigit():
        lines = ["Использование: /set_voice_model <номер>", "", "Доступные модели:"]
        for idx, model in enumerate(voice_models, start=1):
            lines.append(f"{idx}) {model}")
        await update.message.reply_text("\n".join(lines))
        return

    index = int(args[0])
    if index < 1 or index > len(voice_models):
        await update.message.reply_text("Номер модели вне диапазона.")
        return

    selected = voice_models[index - 1]
    set_voice_model(selected)
    set_voice_log_model(selected)
    await update.message.reply_text(
        f"✅ Модель распознавания речи установлена: {selected}\n"
        "Также обновил модель для голосовых логов."
    )


async def set_voice_log_model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меняет модель распознавания для голосовых логов."""
    voice_models = BOT_CONFIG.get("VOICE_MODELS", [])
    if not voice_models:
        await update.message.reply_text("Список моделей распознавания речи пуст.")
        return

    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /set_voice_log_model <номер>")
        return

    index = int(args[0])
    if index < 1 or index > len(voice_models):
        await update.message.reply_text("Номер модели вне диапазона.")
        return

    selected = voice_models[index - 1]
    set_voice_log_model(selected)
    await update.message.reply_text(
        f"✅ Модель распознавания логов установлена: {selected}"
    )


async def voice_log_debug_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Включает подробный лог распознавания."""
    set_voice_log_debug(True)
    await update.message.reply_text("✅ Подробный лог распознавания включен.")


async def voice_log_debug_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отключает подробный лог распознавания."""
    set_voice_log_debug(False)
    await update.message.reply_text("✅ Подробный лог распознавания отключен.")


async def voice_send_raw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает отправку аудио в STT без нарезки."""
    if not is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    set_voice_transcribe_mode("raw")
    await update.message.reply_text(
        "✅ Режим отправки аудио: raw (без нарезки).\n"
        "Это дороже. Переключить: /voice_send_segmented"
    )


async def voice_send_segmented_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает отправку аудио в STT с нарезкой."""
    if not is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    set_voice_transcribe_mode("segmented")
    await update.message.reply_text(
        "✅ Режим отправки аудио: segmented (с нарезкой).\n"
        "Переключить: /voice_send_raw"
    )


async def voice_msg_conversation_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает автоответ на голосовые сообщения."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_voice_auto_reply(chat_id, user_id, True)
    await update.message.reply_text(
        "🔊 Автоответ на голосовые сообщения включён.\n"
        "Отключить: /voice_msg_conversation_off"
    )


async def voice_msg_conversation_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отключает автоответ на голосовые сообщения."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_voice_auto_reply(chat_id, user_id, False)
    await update.message.reply_text(
        "🔇 Автоответ на голосовые сообщения отключён.\n"
        "Включить: /voice_msg_conversation_on"
    )


async def voice_alerts_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отключает Telegram-оповещения о событиях voice для Discord-сервера."""
    message = update.message
    if not message:
        return
    if not is_admin_user(update, context):
        await message.reply_text("Доступ к админ-командам запрещён.")
        return
    guild_id, guild_name = await _resolve_voice_alerts_guild(update, context, "voice_alerts_off")
    if not guild_id:
        return
    chat_id = str(update.effective_chat.id) if update.effective_chat else None
    args = context.args or []
    if len(args) < 2 or args[1].strip().lower() != "confirm":
        await message.reply_text(
            "Чтобы отключить оповещения, добавьте подтверждение:\n"
            f"/voice_alerts_off {guild_id} confirm"
        )
        return
    set_voice_presence_notifications_enabled(guild_id, False, chat_id)
    actor = update.effective_user
    actor_name = (actor.username if actor and actor.username else None) or (
        actor.full_name if actor else None
    )
    log_voice_alerts_toggle(
        guild_id=guild_id,
        enabled=False,
        actor_platform="telegram",
        actor_chat_id=chat_id,
        actor_chat_title=getattr(update.effective_chat, "title", None),
        actor_user_id=str(actor.id) if actor else None,
        actor_name=actor_name,
        source="telegram_command",
        command_text=message.text,
    )
    await message.reply_text(
        f"🔕 Voice-оповещения отключены для этого Telegram-чата на сервере: {guild_name} ({guild_id}).\n"
        f"Включить обратно: /voice_alerts_on {guild_id}"
    )


async def voice_alerts_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает Telegram-оповещения о событиях voice для Discord-сервера."""
    message = update.message
    if not message:
        return
    if not is_admin_user(update, context):
        await message.reply_text("Доступ к админ-командам запрещён.")
        return
    guild_id, guild_name = await _resolve_voice_alerts_guild(update, context, "voice_alerts_on")
    if not guild_id:
        return
    chat_id = str(update.effective_chat.id) if update.effective_chat else None
    set_voice_presence_notifications_enabled(guild_id, True, chat_id)
    actor = update.effective_user
    actor_name = (actor.username if actor and actor.username else None) or (
        actor.full_name if actor else None
    )
    log_voice_alerts_toggle(
        guild_id=guild_id,
        enabled=True,
        actor_platform="telegram",
        actor_chat_id=chat_id,
        actor_chat_title=getattr(update.effective_chat, "title", None),
        actor_user_id=str(actor.id) if actor else None,
        actor_name=actor_name,
        source="telegram_command",
        command_text=message.text,
    )
    await message.reply_text(
        f"🔔 Voice-оповещения включены для этого Telegram-чата на сервере: {guild_name} ({guild_id})."
    )


async def voice_alerts_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает статус Telegram-оповещений о событиях voice для Discord-сервера."""
    message = update.message
    if not message:
        return
    if not is_admin_user(update, context):
        await message.reply_text("Доступ к админ-командам запрещён.")
        return
    guild_id, guild_name = await _resolve_voice_alerts_guild(update, context, "voice_alerts_status")
    if not guild_id:
        return
    chat_id = str(update.effective_chat.id) if update.effective_chat else None
    enabled = get_voice_presence_notifications_enabled(guild_id, chat_id)
    status = "включены" if enabled else "отключены"
    lines = [f"Статус voice-оповещений для этого Telegram-чата на {guild_name} ({guild_id}): {status}."]
    last = get_last_voice_alerts_toggle(guild_id, chat_id)
    if last:
        last_status = "включил" if int(last.get("enabled") or 0) == 1 else "выключил"
        actor_name = last.get("actor_name") or "unknown"
        actor_user = last.get("actor_user_id") or "unknown"
        actor_chat = last.get("actor_chat_title") or last.get("actor_chat_id") or "unknown"
        ts = last.get("created_at") or "unknown"
        lines.append(
            f"Последнее изменение: {last_status} {actor_name} ({actor_user}) в чате {actor_chat} [{ts}]."
        )
    await message.reply_text("\n".join(lines))


async def voice_chunks_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отключает отправку voice-чанков в Telegram для Discord-сервера."""
    message = update.message
    if not message:
        return
    guild_id, guild_name = await _resolve_voice_alerts_guild(update, context, "voice_chunks_off")
    if not guild_id:
        return
    set_voice_chunk_notifications_enabled(guild_id, False)
    await message.reply_text(
        f"🔕 Отправка voice-чанков отключена для сервера: {guild_name} ({guild_id}).\n"
        f"Включить обратно: /voice_chunks_on {guild_id}"
    )


async def voice_chunks_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает отправку voice-чанков в Telegram для Discord-сервера."""
    message = update.message
    if not message:
        return
    guild_id, guild_name = await _resolve_voice_alerts_guild(update, context, "voice_chunks_on")
    if not guild_id:
        return
    set_voice_chunk_notifications_enabled(guild_id, True)
    await message.reply_text(
        f"🔔 Отправка voice-чанков включена для сервера: {guild_name} ({guild_id})."
    )


async def voice_chunks_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает статус отправки voice-чанков в Telegram для Discord-сервера."""
    message = update.message
    if not message:
        return
    guild_id, guild_name = await _resolve_voice_alerts_guild(update, context, "voice_chunks_status")
    if not guild_id:
        return
    enabled = get_voice_chunk_notifications_enabled(guild_id)
    status = "включена" if enabled else "отключена"
    await message.reply_text(
        f"Отправка voice-чанков для {guild_name} ({guild_id}): {status}."
    )


async def tts_voices_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает список доступных голосов TTS."""
    voices = BOT_CONFIG.get("TTS_VOICES", [])
    if not voices:
        await update.message.reply_text("Список голосов TTS пуст.")
        return

    current = get_tts_voice() or BOT_CONFIG.get("TTS_VOICE")
    lines = ["🗣 Доступные голоса TTS:"]
    if current:
        lines.append(f"Текущий: {current}")
    for idx, voice in enumerate(voices, start=1):
        lines.append(f"{idx}) {voice} — `/set_tts_voice {idx}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def set_tts_voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Меняет голос TTS."""
    if not is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    voices = BOT_CONFIG.get("TTS_VOICES", [])
    if not voices:
        await update.message.reply_text("Список голосов TTS пуст.")
        return

    args = context.args or []
    if not args or not args[0].isdigit():
        lines = ["Использование: /set_tts_voice <номер>", "", "Доступные голоса:"]
        for idx, voice in enumerate(voices, start=1):
            lines.append(f"{idx}) {voice}")
        await update.message.reply_text("\n".join(lines))

        return

    index = int(args[0])
    if index < 1 or index > len(voices):
        await update.message.reply_text("Номер голоса вне диапазона.")
        return

    selected = voices[index - 1]
    set_tts_voice(selected)
    await update.message.reply_text(f"✅ Голос TTS установлен: {selected}")


async def set_tts_provider_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Переключает TTS провайдера (local/openai)."""
    if not is_admin_user(update, context):
        await update.message.reply_text("Доступ к админ-командам запрещён.")
        return

    args = context.args or []
    if not args:
        current = get_tts_provider() or "local"
        await update.message.reply_text(
            "Использование: /set_tts_provider <local|openai>\n"
            f"Текущий: {current}"
        )
        return

    choice = args[0].strip().lower()
    if choice not in {"local", "openai"}:
        await update.message.reply_text("Нужно указать local или openai.")
        return

    set_tts_provider(choice)
    await update.message.reply_text(f"✅ TTS провайдер установлен: {choice}")

async def say_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Озвучивает текст голосом (TTS) и отправляет голосовое сообщение."""
    message = update.message
    if not message:
        return

    text = " ".join(context.args or []).strip()
    if not text:
        await message.reply_text("Использование: /say <текст>")
        return

    await message.reply_text("🗣️ Озвучиваю...")

    audio_path = None
    ogg_path = None
    try:
        audio_path, error = await synthesize_speech(
            text,
            platform="telegram",
            chat_id=str(update.effective_chat.id),
            user_id=str(update.effective_user.id),
        )
        if error or not audio_path:
            await message.reply_text(f"Ошибка TTS: {error}")
            return

        ogg_path, convert_error = await _convert_tts_to_ogg(audio_path)
        if not ogg_path:
            await message.reply_text(f"Ошибка конвертации: {convert_error}")
            return

        with open(ogg_path, "rb") as voice_handle:
            await message.reply_voice(voice=voice_handle)
    finally:
        for path in (audio_path, ogg_path):
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    logger.warning("Failed to remove temp file %s", path)
