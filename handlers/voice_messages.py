import logging
import os
import tempfile

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from handlers.message_service import MessageProcessingRequest, process_message_request
from services.memory import get_all_admins
from services.speech_to_text import transcribe_audio

logger = logging.getLogger(__name__)


async def _should_handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    if not message:
        return False

    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return True

    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == context.bot.id:
            return True

    caption = message.caption or ""
    bot_username = context.bot.username
    if bot_username and f"@{bot_username}" in caption:
        return True

    if message.caption_entities and bot_username:
        for entity in message.caption_entities:
            if entity.type == "mention":
                mention_text = caption[entity.offset : entity.offset + entity.length]
                if mention_text == f"@{bot_username}":
                    return True

    return False


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return

    if not (message.voice or message.audio):
        return

    if not await _should_handle_voice(update, context):
        return

    voice = message.voice or message.audio
    file = await voice.get_file()

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
            tmp_path = tmp_file.name
        await file.download_to_drive(tmp_path)

        transcript, error = await transcribe_audio(tmp_path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                logger.warning("Failed to remove temp file %s", tmp_path)

    if not transcript:
        await message.reply_text("Не удалось распознать голосовое сообщение.")
        if error:
            admins = get_all_admins()
            if admins:
                chat_title = message.chat.title or "личка"
                user_name = (
                    message.from_user.username
                    if message.from_user and message.from_user.username
                    else str(message.from_user.id)
                    if message.from_user
                    else "unknown"
                )
                admin_text = (
                    "STT ошибка при распознавании голосового сообщения.\n"
                    f"Чат: {chat_title} ({message.chat_id})\n"
                    f"Пользователь: {user_name}\n"
                    f"Причина: {error}"
                )
                for admin in admins:
                    chat_id = admin.get("chat_id")
                    if not chat_id:
                        continue
                    try:
                        await context.bot.send_message(chat_id=int(chat_id), text=admin_text)
                    except Exception as exc:
                        logger.warning("Failed to notify admin %s: %s", chat_id, exc)
        return

    request = MessageProcessingRequest(
        text=transcript,
        chat_id=str(message.chat_id),
        user_id=str(message.from_user.id),
        bot_username=context.bot.username,
        username=message.from_user.username if message.from_user else None,
    )

    responses = await process_message_request(request)
    for response in responses:
        if response.photo_url:
            await message.reply_photo(response.photo_url)
        elif response.text:
            await message.reply_text(response.text, parse_mode=response.parse_mode)
