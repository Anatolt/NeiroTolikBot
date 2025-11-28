import logging
import re
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
from services.memory import add_message
from config import BOT_CONFIG

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

    # Проверка на запрос изображения
    if text_lower.startswith(("нарисуй", "сгенерируй картинку", "создай изображение")):
        return "image", text, None
    
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
            return "text", prompt, extracted_model
        # Если промпт пустой, но модель указана, все равно возвращаем модель
        # (пользователь может хотеть просто проверить связь с моделью)
        return "text", "", extracted_model
    
    # Проверка на указание модели в начале
    model_keywords = {
        "chatgpt": "openai/gpt-3.5-turbo",
        "claude": "anthropic/claude-3-haiku",
        "claude_opus": "anthropic/claude-3-opus",
        "claude_sonnet": "anthropic/claude-3-sonnet",
        "deepseek": "deepseek/deepseek-r1-distill-qwen-14b",
        "mistral": "mistralai/mistral-large-2407",
        "llama": "meta-llama/llama-3.1-8b-instruct:free",
        "meta": "meta-llama/llama-3.1-8b-instruct:free",
        "qwen": "qwen/qwen2.5-vl-3b-instruct:free",
        "fimbulvetr": "sao10k/fimbulvetr-11b-v2",
        "sao10k": "sao10k/fimbulvetr-11b-v2"
    }
    
    # Проверяем наличие модели в начале запроса
    words = prompt.lower().split()
    if words and words[0] in model_keywords:
        model = model_keywords[words[0]]
        prompt = " ".join(words[1:]).strip()
        return "text", prompt, model
    
    # Проверяем наличие модели в конце запроса
    for keyword, model_name in model_keywords.items():
        if prompt.lower().endswith(f"через {keyword}"):
            model = model_name
            prompt = prompt[:-len(f"через {keyword}")].strip()
            return "text", prompt, model
    
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
        
        if not bot_mentioned:
            logger.info("Group chat message without bot mention, ignoring")
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
    elif request_type == "text":
        logger.info(f"Processing text generation request: '{content}', model: {model}")
        # Добавляем сообщение в историю
        chat_id = str(message.chat_id)
        user_id = str(message.from_user.id)
        model_name = model or BOT_CONFIG["DEFAULT_MODEL"]
        add_message(chat_id, user_id, "user", model_name, content)
        
        # Генерируем ответ
        response = await generate_text(content, model_name, chat_id, user_id)
        
        # Добавляем ответ в историю
        add_message(chat_id, user_id, "assistant", model_name, response)
        
        # Отправляем ответ
        await message.reply_text(f"Ответ от {model_name}:\n\n{response}")
    else:
        logger.warning(f"Unknown request type: {request_type}")
        await message.reply_text("Извините, не удалось обработать ваш запрос.")
