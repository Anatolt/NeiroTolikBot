import logging
import re
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes
from config import BOT_CONFIG
from handlers.message_service import MessageProcessingRequest, process_message_request
from handlers.commands import show_discord_chats_command, show_tg_chats_command
from handlers.voice_messages import YES_VARIANTS, handle_voice_confirmation
from services.memory import (
    add_admin,
    get_latest_pending_discord_join_request,
    get_pending_discord_join_requests,
    is_admin,
    set_discord_join_request_status,
)

logger = logging.getLogger(__name__)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик входящих сообщений."""
    message = update.message
    if not message:
        logger.debug("Received update without message")
        return
    
    if not message.text:
        logger.debug(f"Received non-text message in chat {message.chat_id}, type: {message.chat.type}")
        return

    bot_username = context.bot.username
    text = message.text
    chat_type = message.chat.type
    chat_id = str(message.chat_id)
    user_id = str(message.from_user.id)
    effective_text = text
    normalized_text = text.strip().lower()

    # Проверка ввода пароля администратора
    if context.user_data.get("awaiting_admin_pass"):
        context.user_data["awaiting_admin_pass"] = False
        if text.strip() == BOT_CONFIG.get("ADMIN_PASS"):
            context.user_data["is_admin"] = True
            add_admin(chat_id, user_id)
            await message.reply_text(
                f"Админ-режим активирован. Бот перезапускался в {BOT_CONFIG.get('BOOT_TIME')}."
            )
        else:
            await message.reply_text("Неверный пароль.")
        return

    if normalized_text in {"покажи чаты дискорд", "покажи чаты тг"}:
        if normalized_text == "покажи чаты дискорд":
            await show_discord_chats_command(update, context)
        else:
            await show_tg_chats_command(update, context)
        return

    if normalized_text in YES_VARIANTS or normalized_text in {f"/{item}" for item in YES_VARIANTS}:
        handled = await handle_voice_confirmation(update, context)
        if handled:
            return

    if (
        chat_type == ChatType.PRIVATE
        and is_admin(chat_id, user_id)
        and normalized_text.startswith(("yes", "no"))
    ):
        parts = normalized_text.split()
        decision = parts[0]
        request_id = None
        if len(parts) > 1 and parts[1].isdigit():
            request_id = int(parts[1])

        request = None
        if request_id is not None:
            pending = get_pending_discord_join_requests()
            for item in pending:
                if int(item.get("id", -1)) == request_id:
                    request = item
                    break
        else:
            request = get_latest_pending_discord_join_request()

        if not request:
            await message.reply_text("Нет ожидающих запросов.")
            return

        new_status = "approved" if decision == "yes" else "denied"
        set_discord_join_request_status(int(request["id"]), new_status)
        await message.reply_text(
            f"Принято. Запрос {request['id']} — {'разрешено' if new_status == 'approved' else 'отклонено'}."
        )
        return
    
    # Добавляем подробное логирование для всех сообщений
    logger.info(f"Received message: '{text}' from user {message.from_user.username if message.from_user else 'unknown'} in chat {message.chat_id}")
    logger.info(f"Chat type: {chat_type} (value: {chat_type.value if hasattr(chat_type, 'value') else chat_type}), Bot username: {bot_username}")
    logger.info(f"Chat title: {message.chat.title if hasattr(message.chat, 'title') else 'N/A'}")
    
    # Проверка на упоминание бота в групповых чатах
    if chat_type in [ChatType.GROUP, ChatType.SUPERGROUP]:
        # Проверяем упоминание бота через entities (более надежный способ)
        bot_mentioned = False
        if message.entities:
            for entity in message.entities:
                if entity.type == "mention" and bot_username:
                    mention_text = text[entity.offset:entity.offset + entity.length]
                    if mention_text == f"@{bot_username}":
                        bot_mentioned = True
                        # Удаляем упоминание из текста
                        effective_text = (
                            text[:entity.offset] + text[entity.offset + entity.length:]
                        ).strip()
                        # Удаляем лишние пробелы и знаки препинания в начале
                        effective_text = re.sub(r'^[,\s:]+', '', effective_text)
                        break
        
        # Если не нашли через entities, проверяем простым поиском строки
        if not bot_mentioned and bot_username and f"@{bot_username}" in text:
            bot_mentioned = True
            effective_text = text.replace(f"@{bot_username}", "").strip()
            # Удаляем лишние пробелы и знаки препинания в начале
            effective_text = re.sub(r'^[,\s:]+', '', effective_text)
        
        # Проверяем, является ли сообщение ответом на сообщение бота
        is_reply_to_bot = False
        if message.reply_to_message and message.reply_to_message.from_user:
            # Проверяем, что ответ направлен на сообщение от бота
            if message.reply_to_message.from_user.id == context.bot.id:
                is_reply_to_bot = True
                logger.info("Message is a reply to bot's message, processing")
        
        if not bot_mentioned and not is_reply_to_bot:
            logger.info("Group chat message without bot mention or reply to bot, ignoring")
            return
        
        logger.info(f"Group chat message, extracted text: '{effective_text}'")

    request = MessageProcessingRequest(
        text=effective_text,
        chat_id=chat_id,
        user_id=user_id,
        bot_username=bot_username,
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
