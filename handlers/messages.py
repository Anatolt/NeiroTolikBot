import logging
import re
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes
from config import BOT_CONFIG
from handlers.message_service import MessageProcessingRequest, process_message_request
from handlers.commands import (
    show_discord_chats_command,
    show_tg_chats_command,
    voice_chunks_off_command,
    voice_chunks_on_command,
    voice_chunks_status_command,
    voice_alerts_off_command,
    voice_alerts_on_command,
    voice_alerts_status_command,
)
from handlers.voice_messages import handle_voice_confirmation, PENDING_CONSILIUM_KEY
from services.consilium import parse_consilium_request, select_default_consilium_models
from services.memory import (
    add_admin,
    add_message,
    get_latest_pending_discord_join_request,
    get_miniapp_image_model,
    get_miniapp_text_model,
    get_pending_discord_join_requests,
    get_preferred_model,
    is_admin,
    set_discord_join_request_status,
    upsert_user_profile,
)

logger = logging.getLogger(__name__)


def _router_target_lines(routed, chat_id: str, user_id: str) -> list[str]:
    action = routed.request_type
    suggested = [m for m in (routed.suggested_models or []) if m]

    if action in {"help", "models_hint", "models_category"}:
        return ["🎯 Куда пойдет запрос: без вызова LLM (служебный ответ)"]

    if action == "consilium":
        if suggested:
            return [f"🎯 Куда пойдет запрос: консилиум ({', '.join(suggested)})"]
        return ["🎯 Куда пойдет запрос: консилиум (модели будут выбраны автоматически)"]

    if action == "image":
        image_model = get_miniapp_image_model(user_id) or BOT_CONFIG.get("IMAGE_GENERATION", {}).get("MODEL")
        translation_model = BOT_CONFIG.get("ROUTER_MODEL") or BOT_CONFIG.get("DEFAULT_MODEL")
        return [
            f"🎯 Куда пойдет запрос: генерация изображения ({image_model or 'не задана'})",
            f"📝 Модель перевода промпта: {translation_model or 'не задана'}",
        ]

    target_model = routed.model or get_preferred_model(chat_id, user_id) or get_miniapp_text_model(user_id) or BOT_CONFIG.get("DEFAULT_MODEL")
    return [f"🎯 Куда пойдет запрос: {target_model or 'не задана'}"]

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
        
        # Allow slash commands in groups even when Telegram client doesn't attach
        # bot_command entities (otherwise commands can be silently ignored).
        is_slash_command_text = effective_text.strip().startswith("/")
        if not bot_mentioned and not is_reply_to_bot and not is_slash_command_text:
            logger.info("Group chat message without bot mention or reply to bot, ignoring")
            return
        
        logger.info(f"Group chat message, extracted text: '{effective_text}'")

    stripped_effective = effective_text.strip()
    if stripped_effective.startswith("/"):
        parts = stripped_effective.split()
        raw_cmd = parts[0][1:]
        cmd = raw_cmd.split("@", 1)[0].lower() if raw_cmd else ""
        cmd_args = parts[1:]
        if cmd in {
            "voice_alerts_off",
            "voice_alerts_on",
            "voice_alerts_status",
            "voice_chunks_off",
            "voice_chunks_on",
            "voice_chunks_status",
        }:
            context.args = cmd_args
            if cmd == "voice_alerts_off":
                await voice_alerts_off_command(update, context)
            elif cmd == "voice_alerts_on":
                await voice_alerts_on_command(update, context)
            elif cmd == "voice_alerts_status":
                await voice_alerts_status_command(update, context)
            elif cmd == "voice_chunks_off":
                await voice_chunks_off_command(update, context)
            elif cmd == "voice_chunks_on":
                await voice_chunks_on_command(update, context)
            else:
                await voice_chunks_status_command(update, context)
            return

        voice_chunks_prefix = "voice_chunks_"
        voice_alerts_prefix = "voice_alerts_"
        if cmd.startswith(voice_chunks_prefix):
            suffix = cmd[len(voice_chunks_prefix):].strip()
            if suffix.isdigit():
                context.args = [suffix]
                await voice_chunks_status_command(update, context)
                return
            await message.reply_text(
                "Неверный формат команды.\n"
                "Используйте: /voice_chunks_status <guild_id> или /voice_chunks_status <номер>"
            )
            return

        if cmd.startswith(voice_alerts_prefix):
            suffix = cmd[len(voice_alerts_prefix):].strip()
            if suffix.isdigit():
                context.args = [suffix]
                await voice_alerts_status_command(update, context)
                return
            await message.reply_text(
                "Неверный формат команды.\n"
                "Используйте: /voice_alerts_status <guild_id> или /voice_alerts_status <номер>"
            )
            return
        # Let Telegram CommandHandlers process known commands.
        # If the client sent slash-text without command entity, avoid routing it to LLM.
        return

    if effective_text.strip().lower().startswith("консилиум"):
        models, prompt, has_colon = parse_consilium_request(effective_text)
        if not has_colon:
            await message.reply_text(
                "❗ Для консилиума нужен двоеточие после списка моделей.\n"
                "Пример: консилиум gpt, claude: ваш вопрос"
            )
            return
        if not prompt:
            await message.reply_text("❌ Не указан вопрос для консилиума. Используйте: консилиум модели: ваш вопрос")
            return
        if not models:
            models = await select_default_consilium_models()
            if not models:
                await message.reply_text(
                    "❌ Не удалось выбрать модели для консилиума. Попробуйте указать модели явно."
                )
                return

        pending = context.user_data.get(PENDING_CONSILIUM_KEY, {})
        key = f"{chat_id}:{user_id}"
        pending[key] = {"prompt": prompt, "models": models}
        context.user_data[PENDING_CONSILIUM_KEY] = pending

        models_list = ", ".join(models)
        await message.reply_text(
            "🏥 Консилиум готов к запуску.\n"
            f"Модели: {models_list}\n"
            f"Вопрос: {prompt}\n"
            "Нужен ответ? /yes"
        )
        return

    user_name = None
    if message.from_user:
        user_name = message.from_user.username or message.from_user.full_name

    upsert_user_profile("telegram", chat_id, user_id, user_name)

    request = MessageProcessingRequest(
        text=effective_text,
        chat_id=chat_id,
        user_id=user_id,
        bot_username=bot_username,
        username=user_name,
        platform="telegram",
    )

    async def _ack() -> None:
        await message.reply_text("✅ Принял запрос, думаю...")

    async def _router_start() -> None:
        await message.reply_text("🤖 Обращаюсь в LLM роутер...")

    async def _router_decision(routed) -> bool:
        router_model = BOT_CONFIG.get("ROUTER_MODEL") or BOT_CONFIG.get("DEFAULT_MODEL")
        action = routed.request_type
        reason = routed.reason or "без пояснения"
        model_list = ", ".join(routed.suggested_models) if routed.suggested_models else "не указаны"
        info_lines = [
            f"🤖 Ответ от LLM роутера ({router_model}).",
            f"Действие: {action}",
            *_router_target_lines(routed, chat_id, user_id),
            f"Рекомендации роутера: {model_list}",
            f"Причина: {reason}",
            "Выполнить это действие? /yes",
        ]
        info = "\n".join(info_lines)

        user_message_saved = False
        try:
            add_message(chat_id, user_id, "user", routed.model or "router_pending", effective_text)
            user_message_saved = True
        except Exception as exc:
            logger.warning("Failed to persist pending routed user message: %s", exc)

        pending = context.user_data.get("pending_llm_routes", {})
        key = f"{chat_id}:{user_id}"
        pending[key] = {
            "request": {
                "text": effective_text,
                "chat_id": chat_id,
                "user_id": user_id,
                "bot_username": bot_username,
                "username": user_name,
            },
            "routed": {
                "request_type": routed.request_type,
                "content": routed.content,
                "suggested_models": routed.suggested_models,
                "model": routed.model,
                "category": routed.category,
                "use_context": routed.use_context,
                "reason": routed.reason,
                "user_routing_mode": routed.user_routing_mode,
            },
            "user_message_saved": user_message_saved,
        }
        context.user_data["pending_llm_routes"] = pending

        await message.reply_text(info)
        return False

    responses = await process_message_request(
        request,
        ack_callback=_ack,
        router_start_callback=_router_start,
        router_decision_callback=_router_decision,
    )

    for response in responses:
        if response.photo_url:
            await message.reply_photo(response.photo_url)
        elif response.text:
            await message.reply_text(response.text, parse_mode=response.parse_mode)
