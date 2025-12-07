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
    _resolve_user_model_keyword,
)
from services.memory import add_message, get_history
from services.web_search import search_web
from services.consilium import (
    parse_models_from_message,
    select_default_consilium_models,
    generate_consilium_responses,
    format_consilium_results,
    extract_prompt_from_consilium_message,
)
from config import BOT_CONFIG
from handlers.commands import ADMIN_SESSIONS

logger = logging.getLogger(__name__)

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

async def route_request(text: str, bot_username: str | None) -> tuple[str, str, str | None]:
    """Маршрутизация запроса к соответствующему сервису."""
    text_lower = text.lower().strip()

    # Проверка на запрос возможностей
    if text_lower in ["что ты умеешь", "возможности", "capabilities", "help", "помощь"]:
        return "help", "help", None

    # Проверка на запрос списка моделей
    if text_lower in ["модели", "models"]:
        return "models_hint", "", None

    model_aliases = {
        "покажи бесплатные модели": "free",
        "покажи платные модели": "paid",
        "покажи модели с большим контекстом": "large_context",
        "покажи специализированные модели": "specialized",
        "покажи все модели": "all",
    }

    if text_lower in model_aliases:
        return "models_category", model_aliases[text_lower], None

    # Проверка на запрос консилиума
    if text_lower.startswith("консилиум"):
        return "consilium", text, None

    # Проверка на запрос изображения
    if text_lower.startswith(("нарисуй", "сгенерируй картинку", "создай изображение")):
        return "image", text, None
    
    # Проверка на запрос веб-поиска
    # "погугли ..." или "поищи ..."
    if text_lower.startswith(("погугли", "поищи")):
        # Извлекаем запрос после триггера
        search_query = text
        if text_lower.startswith("погугли"):
            search_query = text[8:].strip()  # Убираем "погугли "
        elif text_lower.startswith("поищи"):
            search_query = text[6:].strip()  # Убираем "поищи "
        
        # Если запрос пустой, это означает "погугли" без запроса - используем предыдущее сообщение
        if not search_query:
            return "search_previous", "", None
        else:
            return "search", search_query, None
    
    # Определение модели для текстового запроса
    model = None
    prompt = text
    
    # Проверка на прямое указание модели в формате "ответь с {model_name}" или "с {model_name}"
    # Паттерн для поиска "ответь с" или "с" перед именем модели
    # Имя модели может содержать: буквы, цифры, дефисы, точки, слеши, двоеточия
    model_pattern = r'(?:ответь\s+с|с)\s+([a-zA-Z0-9\-\._/]+(?::[a-zA-Z0-9\-\._]+)?)'
    match = re.search(model_pattern, text_lower, re.IGNORECASE)
    if match:
        extracted_model = match.group(1)
        resolved = _resolve_user_model_keyword(extracted_model)
        # Удаляем указание модели из текста (включая возможные пробелы и запятые после)
        # Используем более точный паттерн для удаления
        prompt = re.sub(
            r'(?:ответь\s+с|с)\s+' + re.escape(extracted_model) + r'[,\s]*',
            '',
            text,
            flags=re.IGNORECASE,
            count=1
        ).strip()
        # Убираем лишние пробелы и запятые в начале
        prompt = re.sub(r'^[,\s]+', '', prompt)
        # Если промпт не пустой, возвращаем его с моделью
        if prompt:
            return "text", prompt, resolved or extracted_model
        # Если промпт пустой, но модель указана, все равно возвращаем модель
        # (пользователь может хотеть просто проверить связь с моделью)
        return "text", "", resolved or extracted_model
    
    # Проверка на указание модели в начале
    model_keywords = {k.lower(): v for k, v in BOT_CONFIG.get("MODELS", {}).items()}
    
    # Проверяем наличие модели в начале запроса
    words = prompt.lower().split()
    if words and words[0] in model_keywords:
        resolved = _resolve_user_model_keyword(words[0]) or model_keywords[words[0]]
        model = resolved
        prompt = " ".join(words[1:]).strip()
        return "text", prompt, model
    
    # Проверяем наличие модели в конце запроса
    for keyword, model_name in model_keywords.items():
        if prompt.lower().endswith(f"через {keyword}"):
            model = _resolve_user_model_keyword(keyword) or model_name
            prompt = prompt[:-len(f"через {keyword}")].strip()
            return "text", prompt, model

    # Префиксное совпадение с витриной моделей (например, "nvidia ..." или "kwaipilot ...")
    first_word = words[0] if words else ""
    if first_word:
        resolved = _resolve_user_model_keyword(first_word)
        if resolved:
            prompt = " ".join(words[1:]).strip()
            return "text", prompt, resolved
    
    return "text", prompt, None

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
    effective_text = text

    # Проверка ввода пароля администратора
    if context.user_data.get("awaiting_admin_pass"):
        context.user_data["awaiting_admin_pass"] = False
        if text.strip() == BOT_CONFIG.get("ADMIN_PASS"):
            context.user_data["is_admin"] = True
            ADMIN_SESSIONS.add((str(message.chat_id), str(message.from_user.id)))
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
    
    # Маршрутизация запроса
    logger.info(f"Routing request: '{effective_text}'")
    request_type, content, model = await route_request(effective_text, bot_username)
    logger.info(f"Request routed to: {request_type}, model: {model}")
    
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
        response, used_model = await generate_text(
            prompt_with_search, model_name, chat_id, user_id, search_results=search_results
        )
        
        # Добавляем ответ в историю
        add_message(chat_id, user_id, "assistant", used_model, response)
        
        # Отправляем ответ
        await message.reply_text(f"Ответ от {used_model}:\n\n{response}")
    
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
        search_query_response, _used_model = await generate_text(search_prompt, model_name, None, None)
        search_query = search_query_response.strip().strip('"').strip("'")
        
        logger.info(f"Model formulated search query: '{search_query}'")
        
        # Выполняем поиск
        search_results = await search_web(search_query)
        
        # Формируем финальный промпт
        final_prompt = f"Пользователь ранее спросил: '{previous_user_message}'\n\nЯ ранее ответил: '{previous_assistant_message}'\n\nТеперь я нашел дополнительную информацию в интернете по запросу '{search_query}':\n\n{search_results}\n\nПожалуйста, проанализируй найденную информацию и дополни мой предыдущий ответ актуальными данными из интернета."
        
        # Добавляем сообщение в историю
        add_message(chat_id, user_id, "user", model_name, "погугли")
        
        # Генерируем финальный ответ
        response, used_model = await generate_text(
            final_prompt, model_name, chat_id, user_id, search_results=search_results
        )
        
        # Добавляем ответ в историю
        add_message(chat_id, user_id, "assistant", used_model, response)
        
        # Отправляем ответ
        await message.reply_text(f"Ответ от {used_model}:\n\n{response}")
    
    elif request_type == "consilium":
        logger.info(f"Processing consilium request: '{content}'")
        chat_id = str(message.chat_id)
        user_id = str(message.from_user.id)
        
        # Парсим модели из сообщения
        models = await parse_models_from_message(content)
        
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
        response, used_model = await generate_text(content, model_name, chat_id, user_id)
        
        # Добавляем ответ в историю
        add_message(chat_id, user_id, "assistant", used_model, response)
        
        # Отправляем ответ
        await message.reply_text(f"Ответ от {used_model}:\n\n{response}")
    else:
        logger.warning(f"Unknown request type: {request_type}")
        await message.reply_text("Извините, не удалось обработать ваш запрос.")
