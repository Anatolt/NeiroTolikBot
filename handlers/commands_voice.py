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
    get_tts_voice,
    set_tts_voice,
    set_voice_log_debug,
    set_voice_log_model,
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
        audio_path, error = await synthesize_speech(text)
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
