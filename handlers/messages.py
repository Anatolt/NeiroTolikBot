import logging
from telegram import Update
from telegram.ext import ContextTypes
from utils.helpers import escape_markdown_v2
from services.generation import (
    categorize_models,
    fetch_models_data,
    generate_image,
    generate_text,
)
from services.memory import add_message
from config import BOT_CONFIG

logger = logging.getLogger(__name__)

async def get_capabilities() -> str:
    """Получение и форматирование информации о доступных моделях."""
    try:
        models_data = await fetch_models_data()
        if not models_data:
            return ["Извините, не удалось получить информацию о моих возможностях."]

        categories = categorize_models(models_data)
        capabilities = ["🤖 Доступные модели по категориям:\n\n"]
        current_part = capabilities[0]
        max_items_per_category = 20

        category_titles = {
            "free": "БЕСПЛАТНЫЕ МОДЕЛИ:",
            "large_context": "МОДЕЛИ С БОЛЬШИМ КОНТЕКСТОМ (≥100K):",
            "specialized": "СПЕЦИАЛИЗИРОВАННЫЕ МОДЕЛИ:",
            "paid": "ПЛАТНЫЕ МОДЕЛИ:",
        }

        for key in ["free", "large_context", "specialized", "paid"]:
            models = categories.get(key, [])
            if not models:
                continue

            category_block = f"{category_titles[key]}\n"
            displayed_models = models[:max_items_per_category]

            for model in displayed_models:
                context_length = model.get('context_length', 0)
                context_kb = context_length / 1024 if context_length else 0
                context_str = f"{context_kb:.0f}K" if context_kb > 0 else 'N/A'
                category_block += f"• {model.get('id', 'Unknown')} ({context_str})\n"

            remaining = len(models) - len(displayed_models)
            if remaining > 0:
                category_block += f"…и еще {remaining} моделей в этой категории\n"

            category_block += "\n"

            if len(current_part + category_block) > 3000:
                capabilities.append(category_block)
                current_part = category_block
            else:
                current_part += category_block

        instructions = "💡 Как использовать:\n"
        instructions += f"• Просто напиши свой вопрос - отвечу через {BOT_CONFIG['DEFAULT_MODEL']}\n"
        instructions += "• Укажи модель в начале ('chatgpt расскажи о погоде')\n"
        instructions += "• Или в конце ('расскажи о погоде через claude')\n"
        instructions += "• Для картинок используй 'нарисуй' или 'сгенерируй картинку'"

        if len(current_part + instructions) > 3000:
            capabilities.append(instructions)
        else:
            capabilities[-1] += instructions

        return capabilities
    except Exception as e:
        logger.error(f"Error getting capabilities: {str(e)}")
        return ["Извините, не удалось получить информацию о моих возможностях."]

async def route_request(text: str, bot_username: str | None) -> tuple[str, str, str | None]:
    """Маршрутизация запроса к соответствующему сервису."""
    # Проверка на запрос возможностей
    if text.lower() in ["что ты умеешь", "возможности", "capabilities", "help", "помощь"]:
        return "help", "help", None
    
    # Проверка на запрос списка моделей
    if text.lower() in ["модели", "models"]:
        return "capabilities", await get_capabilities(), None
    
    # Проверка на запрос изображения
    if text.lower().startswith(("нарисуй", "сгенерируй картинку", "создай изображение")):
        return "image", text, None
    
    # Определение модели для текстового запроса
    model = None
    prompt = text
    
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
    if not message or not message.text:
        return

    bot_username = context.bot.username
    text = message.text
    chat_type = message.chat.type
    effective_text = text
    
    # Добавляем подробное логирование
    logger.info(f"Received message: '{text}' from user {message.from_user.username} in chat {message.chat_id}")
    logger.info(f"Chat type: {chat_type}, Bot username: {bot_username}")
    
    # Проверка на упоминание бота в групповых чатах
    if chat_type in ["group", "supergroup"]:
        if bot_username and f"@{bot_username}" in text:
            effective_text = text.replace(f"@{bot_username}", "").strip()
            logger.info(f"Group chat message, extracted text: '{effective_text}'")
        else:
            logger.info("Group chat message without bot mention, ignoring")
            return
    
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
    elif request_type == "capabilities":
        logger.info("Processing capabilities request")
        for part in content:
            await message.reply_text(part)
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
