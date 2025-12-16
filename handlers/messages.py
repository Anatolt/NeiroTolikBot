import logging
import re
import time
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes
from handlers.commands import MODELS_HINT_TEXT
from services.generation import (
    CATEGORY_TITLES,
    build_models_messages,
    generate_image,
    generate_text,
)
from services.memory import (
    add_message,
    get_history,
    get_routing_mode,
    get_show_response_header,
    set_routing_mode,
    set_show_response_header,
)
from services.web_search import search_web
from services.consilium import (
    parse_models_from_message,
    select_default_consilium_models,
    generate_consilium_responses,
    format_consilium_results,
    extract_prompt_from_consilium_message,
)
from services.router import route_request
from config import BOT_CONFIG
from services.memory import add_admin, is_admin

logger = logging.getLogger(__name__)

_ROUTING_RULES_KEYWORDS = {
    "роутинг алгоритмами",
    "роутинг правилами",
    "routing rules",
    "routing algorithms",
    "routing algo",
}

_ROUTING_LLM_KEYWORDS = {
    "роутинг ллм",
    "роутинг llm",
    "routing llm",
    "routing ai",
}

_ROUTING_STATUS_KEYWORDS = {
    "какой роутинг",
    "режим роутинга",
    "routing mode",
}

_HEADER_DISABLE_KEYWORDS = {
    "спрячь шапку",
    "скрой шапку",
    "скрыть шапку",
    "выключи шапку",
    "отключи шапку",
    "убери шапку",
    "без шапки",
    "скрой техшапку",
}

_HEADER_ENABLE_KEYWORDS = {
    "включи шапку",
    "показывай шапку",
    "верни шапку",
    "покажи шапку",
    "включи техшапку",
    "техшапка вкл",
}


def _normalize_routing_choice(text: str) -> str | None:
    normalized = text.strip().lower()
    if normalized in _ROUTING_RULES_KEYWORDS:
        return "rules"
    if normalized in _ROUTING_LLM_KEYWORDS:
        return "llm"
    return None


def _is_routing_status_request(text: str) -> bool:
    return text.strip().lower() in _ROUTING_STATUS_KEYWORDS


def _normalize_header_toggle(text: str) -> bool | None:
    normalized = text.strip().lower()
    if normalized in _HEADER_DISABLE_KEYWORDS:
        return False
    if normalized in _HEADER_ENABLE_KEYWORDS:
        return True
    return None


def _format_response_header(
    routing_mode: str | None, context_info: dict | None, model: str | None
) -> str | None:
    parts: list[str] = []

    if routing_mode:
        routing_label = "алгоритмический" if routing_mode == "rules" else "LLM"
        parts.append(f"🔀 Роутинг: {routing_label}")

    if context_info:
        tokens = context_info.get("usage_tokens")
        chars = context_info.get("usage_chars")
        limit = context_info.get("context_limit")

        context_chunks: list[str] = []
        if tokens and limit:
            context_chunks.append(f"{tokens}/{limit} т")
        elif tokens:
            context_chunks.append(f"{tokens} т")

        if chars:
            context_chunks.append(f"{chars} симв")

        if context_chunks:
            parts.append(f"📦 Контекст: {' • '.join(context_chunks)}")

        trimmed = context_info.get("trimmed_from_context")
        if trimmed:
            parts.append(f"✂️ Обрезано: {trimmed}")

        if context_info.get("summary_text"):
            parts.append("🧾 Саммари истории")

        warnings = context_info.get("warnings") or []
        if warnings:
            parts.append(f"⚠️ {warnings[0]}")

    if model:
        parts.append(f"🤖 Модель: {model}")

    return " • ".join(parts) if parts else None

async def _notify_context_guard(message, context_info: dict | None) -> None:
    if not context_info:
        return

    notices = []
    if context_info.get("summary_text"):
        notices.append("⚠️ Контекст переполнен — делаю саммари истории.")
    elif context_info.get("trimmed_from_context"):
        notices.append("⚠️ Контекст переполнен — скрываю самые старые сообщения из запроса.")

    for warn in context_info.get("warnings", []):
        notices.append(f"ℹ️ {warn}")

    for note in notices:
        try:
            await message.reply_text(note)
        except Exception as e:
            logger.warning(f"Failed to send context notice: {e}")

async def get_capabilities() -> list[str]:
    """Получение и форматирование информации о доступных моделях."""
    try:
        capabilities = await build_models_messages(
            ["free", "large_context", "specialized", "paid"],
            header="🤖 Доступные модели по категориям:\n\n",
            max_items_per_category=20,
        )

        if not capabilities:
            return ["Извините, не удалось получить информацию о моих возможностях."]

        instructions = "💡 Как использовать:\n"
        instructions += f"• Просто напиши свой вопрос - отвечу через {BOT_CONFIG['DEFAULT_MODEL']}\n"
        instructions += "• Укажи модель в начале ('chatgpt расскажи о погоде')\n"
        instructions += "• Или в конце ('расскажи о погоде через claude')\n"
        instructions += "• Для картинок используй 'нарисуй' или 'сгенерируй картинку'"

        if len(capabilities[-1] + instructions) > 3000:
            capabilities.append(instructions)
        else:
            capabilities[-1] += instructions

        return capabilities
    except Exception as e:
        logger.error(f"Error getting capabilities: {str(e)}")
        return ["Извините, не удалось получить информацию о моих возможностях."]

async def send_models_by_request(
    message,
    order: list[str],
    header: str,
    max_items: int | None = 20,
) -> None:
    """Отправляет список моделей для указанной категории."""

    parts = await build_models_messages(order, header=header, max_items_per_category=max_items)
    if not parts:
        await message.reply_text("Не удалось получить список моделей. Пожалуйста, попробуйте позже.")
        return

    for part in parts:
        await message.reply_text(part)

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
    show_response_header = get_show_response_header(chat_id, user_id)
    effective_text = text

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

    # Индивидуальное переключение режима роутинга через текстовые команды
    header_toggle = _normalize_header_toggle(effective_text)
    if header_toggle is not None:
        set_show_response_header(chat_id, user_id, header_toggle)
        reply = (
            "🛠 Техшапка включена и будет показываться над ответами.\n"
            "Чтобы скрыть, отправьте 'скрыть шапку' или команду /header_off."
        )
        if not header_toggle:
            reply = (
                "🫥 Техшапка скрыта.\n"
                "Чтобы вернуть её, отправьте 'показывай шапку' или команду /header_on."
            )

        await message.reply_text(reply)
        return

    routing_choice = _normalize_routing_choice(effective_text)
    if routing_choice:
        set_routing_mode(chat_id, user_id, routing_choice)
        mode_label = "алгоритмический" if routing_choice == "rules" else "LLM"
        await message.reply_text(
            f"🔀 Включён {mode_label} роутинг для ваших сообщений в этом чате.\n"
            f"Чтобы переключиться, отправьте 'роутинг алгоритмами' или 'роутинг ллм', либо используйте слеш-команды /routing_rules и /routing_llm."
        )
        return

    if _is_routing_status_request(effective_text):
        current_mode = get_routing_mode(chat_id, user_id) or BOT_CONFIG.get("ROUTING_MODE", "rules")
        mode_label = "алгоритмический" if current_mode == "rules" else "LLM"
        await message.reply_text(f"🔎 Текущий режим роутинга: {mode_label}.")
        return
    
    # Маршрутизация запроса
    user_routing_mode = get_routing_mode(chat_id, user_id) or BOT_CONFIG.get("ROUTING_MODE", "rules")
    logger.info(f"Routing request (mode={user_routing_mode}): '{effective_text}'")
    decision = await route_request(effective_text, bot_username, routing_mode=user_routing_mode)
    request_type = decision.action or "text"
    content = decision.prompt or effective_text
    suggested_models = decision.target_models or []
    model = suggested_models[0] if suggested_models else None
    category = decision.category
    use_context = decision.use_context
    logger.info(
        f"Router resolved request to: {request_type}, model: {model}, use_context: {decision.use_context}, reason: {decision.reason}"
    )

    if request_type == "search" and not content:
        request_type = "search_previous"

    if request_type == "models_category" and category:
        content = category

    if request_type == "text" and len(suggested_models) > 1:
        request_type = "consilium"
    
    # Обработка запроса
    if request_type == "help":
        logger.info("Processing help request")
        capabilities = await get_capabilities()
        for part in capabilities:
            await message.reply_text(part)
    elif request_type == "models_hint":
        logger.info("Providing models hint")
        await message.reply_text(MODELS_HINT_TEXT)
    elif request_type == "models_category":
        logger.info(f"Providing models list for category: {content}")
        if content == "all":
            await send_models_by_request(
                message,
                ["free", "large_context", "specialized", "paid"],
                MODELS_HINT_TEXT,
                max_items=None,
            )
        else:
            await send_models_by_request(
                message,
                [content],
                CATEGORY_TITLES.get(content, "Список моделей:"),
                max_items=20,
            )
    elif request_type == "image":
        logger.info(f"Processing image generation request: '{content}'")
        await message.reply_text("Генерирую изображение...")
        image_url = await generate_image(content)
        if image_url:
            await message.reply_photo(image_url)
        else:
            await message.reply_text("Не удалось сгенерировать изображение.")
    elif request_type == "search":
        # Поиск с указанным запросом
        logger.info(f"Processing web search request: '{content}'")
        chat_id = str(message.chat_id)
        user_id = str(message.from_user.id)
        model_name = model or BOT_CONFIG["DEFAULT_MODEL"]
        
        await message.reply_text("Ищу информацию в интернете...")
        
        # Выполняем поиск
        search_results = await search_web(content)
        
        # Формируем промпт с результатами поиска
        prompt_with_search = f"Пользователь попросил найти информацию: '{content}'. Вот результаты поиска в интернете:\n\n{search_results}\n\nПожалуйста, проанализируй найденную информацию и дай развернутый ответ на запрос пользователя."
        
        # Добавляем сообщение в историю
        add_message(chat_id, user_id, "user", model_name, f"погугли {content}")
        
        # Генерируем ответ с результатами поиска
        response, used_model, context_info = await generate_text(
            prompt_with_search,
            model_name,
            chat_id,
            user_id,
            search_results=search_results,
            use_context=use_context,
        )

        await _notify_context_guard(message, context_info)
        
        # Добавляем ответ в историю
        add_message(chat_id, user_id, "assistant", used_model, response)
        
        # Отправляем ответ
        header = (
            _format_response_header(user_routing_mode, context_info, used_model)
            if show_response_header
            else None
        )
        reply_text = f"{header}\n\n{response}" if header else response
        await message.reply_text(reply_text)
    
    elif request_type == "search_previous":
        # Поиск по предыдущему сообщению - возвращаемся к последнему ответу модели
        logger.info("Processing web search for previous message")
        chat_id = str(message.chat_id)
        user_id = str(message.from_user.id)
        model_name = model or BOT_CONFIG["DEFAULT_MODEL"]
        
        # Получаем историю сообщений
        history = get_history(chat_id, user_id, limit=10)
        
        # Ищем последнее сообщение пользователя и последний ответ ассистента
        previous_user_message = None
        previous_assistant_message = None
        
        for msg in history:
            if msg["role"] == "assistant" and not previous_assistant_message:
                previous_assistant_message = msg["text"]
            elif msg["role"] == "user" and msg["text"].lower() not in ["погугли", "поищи"] and not previous_user_message:
                previous_user_message = msg["text"]
                # Если нашли и пользователя и ассистента, можно выходить
                if previous_assistant_message:
                    break
        
        if not previous_user_message or not previous_assistant_message:
            await message.reply_text("Не найдено предыдущего сообщения для поиска. Пожалуйста, укажите, что искать, например: 'погугли погода в Москве'")
            return
        
        await message.reply_text(f"Ищу дополнительную информацию по вашему предыдущему вопросу: '{previous_user_message}'...")
        
        # Просим модель сформулировать поисковый запрос на основе предыдущего вопроса и ответа
        search_prompt = f"Пользователь ранее спросил: '{previous_user_message}'\n\nЯ ответил: '{previous_assistant_message}'\n\nТеперь пользователь просит найти дополнительную информацию в интернете. Сформулируй краткий поисковый запрос (2-5 слов) для поиска в интернете, который поможет дополнить или уточнить мой ответ. Ответь только поисковым запросом, без дополнительных слов."
        
        # Получаем поисковый запрос от модели (без добавления в историю, чтобы не засорять)
        search_query_response, _used_model, _context_info = await generate_text(
            search_prompt, model_name, None, None, use_context=False
        )
        search_query = search_query_response.strip().strip('"').strip("'")
        
        logger.info(f"Model formulated search query: '{search_query}'")
        
        # Выполняем поиск
        search_results = await search_web(search_query)
        
        # Формируем финальный промпт
        final_prompt = f"Пользователь ранее спросил: '{previous_user_message}'\n\nЯ ранее ответил: '{previous_assistant_message}'\n\nТеперь я нашел дополнительную информацию в интернете по запросу '{search_query}':\n\n{search_results}\n\nПожалуйста, проанализируй найденную информацию и дополни мой предыдущий ответ актуальными данными из интернета."
        
        # Добавляем сообщение в историю
        add_message(chat_id, user_id, "user", model_name, "погугли")
        
        # Генерируем финальный ответ
        response, used_model, context_info = await generate_text(
            final_prompt,
            model_name,
            chat_id,
            user_id,
            search_results=search_results,
            use_context=use_context,
        )

        await _notify_context_guard(message, context_info)
        
        # Добавляем ответ в историю
        add_message(chat_id, user_id, "assistant", used_model, response)
        
        # Отправляем ответ
        header = (
            _format_response_header(user_routing_mode, context_info, used_model)
            if show_response_header
            else None
        )
        reply_text = f"{header}\n\n{response}" if header else response
        await message.reply_text(reply_text)
    
    elif request_type == "consilium":
        logger.info(f"Processing consilium request: '{content}'")
        chat_id = str(message.chat_id)
        user_id = str(message.from_user.id)
        
        # Парсим модели из сообщения или используем подсказку сортировщика
        models = suggested_models or await parse_models_from_message(content)
        
        # Если модели не указаны, выбираем по умолчанию
        if not models:
            models = await select_default_consilium_models()
            if not models:
                await message.reply_text("❌ Не удалось выбрать модели для консилиума. Попробуйте указать модели явно.")
                return
        
        # Извлекаем промпт
        prompt = extract_prompt_from_consilium_message(content)
        
        if not prompt:
            await message.reply_text("❌ Не указан вопрос для консилиума. Используйте: консилиум: ваш вопрос")
            return
        
        # Отправляем сообщение о начале генерации
        status_message = await message.reply_text(f"🏥 Генерирую ответы от {len(models)} моделей...")
        
        # Добавляем запрос в историю (один раз)
        if BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("SAVE_TO_HISTORY", True):
            add_message(chat_id, user_id, "user", models[0], prompt)
        
        # Засекаем время
        start_time = time.time()
        
        # Генерируем ответы параллельно
        results = await generate_consilium_responses(prompt, models, chat_id, user_id)
        
        # Вычисляем время выполнения
        execution_time = time.time() - start_time
        
        # Форматируем результаты (теперь возвращает список сообщений)
        formatted_messages = format_consilium_results(results, execution_time)
        
        # Удаляем сообщение о статусе
        try:
            await status_message.delete()
        except Exception as e:
            logger.warning(f"Could not delete status message: {e}")
        
        # Сохраняем ответы в историю (если включено)
        if BOT_CONFIG.get("CONSILIUM_CONFIG", {}).get("SAVE_TO_HISTORY", True):
            for result in results:
                if result.get("success") and result.get("response"):
                    add_message(chat_id, user_id, "assistant", result.get("model"), result.get("response"))
        
        # Отправляем каждое сообщение отдельно
        max_length = 4000
        for msg in formatted_messages:
            # Если сообщение слишком длинное, разбиваем его
            if len(msg) > max_length:
                # Разбиваем на части
                parts = []
                current_part = ""
                lines = msg.split("\n")
                
                for line in lines:
                    if len(current_part) + len(line) + 1 > max_length:
                        if current_part:
                            parts.append(current_part)
                        current_part = line + "\n"
                    else:
                        current_part += line + "\n"
                
                if current_part:
                    parts.append(current_part)
                
                # Отправляем части
                for i, part in enumerate(parts):
                    if i == 0:
                        await message.reply_text(part)
                    else:
                        await message.reply_text(f"*(продолжение {i+1}/{len(parts)})*\n\n{part}", parse_mode="Markdown")
            else:
                await message.reply_text(msg)
    
    elif request_type == "text":
        logger.info(f"Processing text generation request: '{content}', model: {model}")
        # Добавляем сообщение в историю
        chat_id = str(message.chat_id)
        user_id = str(message.from_user.id)
        model_name = model or BOT_CONFIG["DEFAULT_MODEL"]
        add_message(chat_id, user_id, "user", model_name, content)
        
        # Генерируем ответ
        response, used_model, context_info = await generate_text(
            content, model_name, chat_id, user_id, use_context=use_context
        )

        await _notify_context_guard(message, context_info)
        
        # Добавляем ответ в историю
        add_message(chat_id, user_id, "assistant", used_model, response)
        
        # Отправляем ответ
        header = (
            _format_response_header(user_routing_mode, context_info, used_model)
            if show_response_header
            else None
        )
        reply_text = f"{header}\n\n{response}" if header else response
        await message.reply_text(reply_text)
    else:
        logger.warning(f"Unknown request type: {request_type}")
        await message.reply_text("Извините, не удалось обработать ваш запрос.")
