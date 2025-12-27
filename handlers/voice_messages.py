import logging
import os
import tempfile

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from handlers.message_service import (
    MessageProcessingRequest,
    RoutedRequest,
    execute_routed_request,
    process_message_request,
)
from services.memory import get_all_admins, get_voice_auto_reply
from services.speech_to_text import transcribe_audio

logger = logging.getLogger(__name__)

YES_VARIANTS = {"yes", "y"}
PENDING_LLM_ROUTER_KEY = "pending_llm_routes"


async def _process_voice_transcript(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    transcript: str,
) -> None:
    message = update.message
    if not message:
        return

    request = MessageProcessingRequest(
        text=transcript,
        chat_id=str(message.chat_id),
        user_id=str(message.from_user.id),
        bot_username=context.bot.username,
        username=message.from_user.username if message.from_user else None,
    )

    async def _ack() -> None:
        await message.reply_text("✅ Принял запрос, думаю...")

    responses = await process_message_request(request, ack_callback=_ack)
    for response in responses:
        if response.photo_url:
            await message.reply_photo(response.photo_url)
        elif response.text:
            await message.reply_text(response.text, parse_mode=response.parse_mode)

    if not get_voice_auto_reply(str(message.chat_id), str(message.from_user.id)):
        await message.reply_text(
            "Можно перейти в режим диалога, чтобы я не переспрашивал отвечать ли на голосовухи: "
            "/voice_msg_conversation_on"
        )


async def handle_voice_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    if not message or not message.text:
        return False

    normalized = message.text.strip().lower()
    if normalized.startswith("/"):
        normalized = normalized[1:]

    if normalized not in YES_VARIANTS:
        return False

    pending = context.user_data.get("pending_voice_transcripts", {})
    transcript = pending.pop(str(message.chat_id), None)
    context.user_data["pending_voice_transcripts"] = pending

    if not transcript:
        return False

    await _process_voice_transcript(update, context, transcript)
    return True


async def voice_confirmation_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await handle_llm_router_confirmation(update, context):
        return
    await handle_voice_confirmation(update, context)


async def handle_llm_router_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    if not message or not message.text:
        return False

    normalized = message.text.strip().lower()
    if normalized.startswith("/"):
        normalized = normalized[1:]

    if normalized not in YES_VARIANTS:
        return False

    pending = context.user_data.get(PENDING_LLM_ROUTER_KEY, {})
    key = f"{message.chat_id}:{message.from_user.id}"
    entry = pending.pop(key, None)
    context.user_data[PENDING_LLM_ROUTER_KEY] = pending

    if not entry:
        return False

    request_data = entry.get("request", {})
    routed_data = entry.get("routed", {})

    request = MessageProcessingRequest(
        text=request_data.get("text", ""),
        chat_id=str(request_data.get("chat_id", message.chat_id)),
        user_id=str(request_data.get("user_id", message.from_user.id)),
        bot_username=request_data.get("bot_username"),
        username=request_data.get("username"),
    )

    routed = RoutedRequest(
        request_type=routed_data.get("request_type", "text"),
        content=routed_data.get("content", request.text),
        suggested_models=routed_data.get("suggested_models", []),
        model=routed_data.get("model"),
        category=routed_data.get("category"),
        use_context=bool(routed_data.get("use_context", True)),
        reason=routed_data.get("reason"),
        user_routing_mode=routed_data.get("user_routing_mode", "llm"),
    )

    async def _ack() -> None:
        await message.reply_text("✅ Принял запрос, выполняю...")

    responses = await execute_routed_request(request, routed, ack_callback=_ack)
    for response in responses:
        if response.photo_url:
            await message.reply_photo(response.photo_url)
        elif response.text:
            await message.reply_text(response.text, parse_mode=response.parse_mode)

    return True


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

    await message.reply_text("Распознаю голосовое сообщение...")

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

    await message.reply_text(f"Текст голосового:\n{transcript}")

    if get_voice_auto_reply(str(message.chat_id), str(message.from_user.id)):
        await _process_voice_transcript(update, context, transcript)
        return

    pending = context.user_data.get("pending_voice_transcripts", {})
    pending[str(message.chat_id)] = transcript
    context.user_data["pending_voice_transcripts"] = pending

    await message.reply_text("Нужен ответ? /yes")
