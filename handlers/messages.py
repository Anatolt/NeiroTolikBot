import logging
from telegram import Update
from telegram.ext import ContextTypes
from utils.helpers import escape_markdown_v2
from services.generation import generate_text, generate_image
from services.memory import add_message, get_history, get_user_summary
from config import BOT_CONFIG

logger = logging.getLogger(__name__)

async def get_capabilities() -> str:
    """Получение и форматирование информации о доступных моделях."""
    try:
        response = await client.models.list()
        capabilities = ["todo: переписать, чтоб отвечал с учётом промта и readme git) \n\n Вот что я умею:\n\n🤖 Доступные модели:\n"]
        current_part = capabilities[0]
        
        for model in response.data:
            model_data = model if isinstance(model, dict) else model.model_dump()
            model_id = model_data.get('id', 'Unknown')
            context_length = model_data.get('context_length', 'N/A')
            pricing = model_data.get('pricing', {})
            prompt_price = pricing.get('prompt', 'N/A') if isinstance(pricing, dict) else 'N/A'
            
            model_info = f"• {model_id} (макс. контекст: {context_length})\n"
            if prompt_price != 'N/A':
                model_info += f"  └─ Цена: ${prompt_price}/1K токенов\n"
            
            if len(current_part + model_info) > 3000:
                capabilities.append(model_info)
                current_part = model_info
            else:
                current_part += model_info
        
        instructions = "\n💡 Как использовать:\n"
        instructions += f"• Просто напиши свой вопрос - отвечу через {BOT_CONFIG['DEFAULT_MODEL']}\n"
        instructions += "• Укажи модель в начале ('chatgpt расскажи о погоде')\n"
        instructions += "• Или в конце ('расскажи о погоде через claude')\n"
        instructions += "• Для картинок используй 'нарисуй' или 'сгенерируй картинку'\n"
        instructions += "• Используй /new для начала нового диалога (сохраняет историю)\n"
        instructions += "• Используй /clear для полной очистки памяти"
        
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
        "claude": "anthropic/claude-3-opus-20240229",
        "deepseek": "deepseek/deepseek-chat-33b"
    }
    
    found_model_keyword = False
    for keyword, model_name in model_keywords.items():
        if prompt.lower().startswith(keyword):
            model = model_name
            prompt = prompt[len(keyword):].strip()
            found_model_keyword = True
            break
    
    # Проверка на указание модели в конце
    if not found_model_keyword:
        for keyword, model_name in model_keywords.items():
            if prompt.lower().endswith(f"через {keyword}"):
                model = model_name
                prompt = prompt[:-len(f"через {keyword}")].strip()
                found_model_keyword = True
                break
    
    if found_model_keyword:
        logger.info(f"Routing to text generation with specified model: {model}. Clean prompt: '{prompt}'")
    else:
        model = BOT_CONFIG["DEFAULT_MODEL"]
        logger.info(f"Routing to text generation with default model: {model}. Clean prompt: '{prompt}'")

    return "text", prompt, model

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик входящих сообщений."""
    message = update.message
    if not message or not message.text:
        return

    bot_username = context.bot.username
    text = message.text
    chat_type = message.chat.type
    effective_text = text
    chat_id = str(message.chat.id)
    user_id = str(message.from_user.id)

    is_mentioned = False
    if chat_type in ['group', 'supergroup']:
        mention = f"@{bot_username}"
        if effective_text.startswith(mention):
            effective_text = effective_text[len(mention):].strip()
            is_mentioned = True
        elif mention in effective_text:
            logger.debug(f"Ignoring message in group {message.chat.id} - mention not at start.")
            return
        else:
            logger.debug(f"Ignoring message in group {message.chat.id} as bot was not mentioned.")
            return

        if not effective_text:
            logger.debug(f"Ignoring message in group {message.chat.id} as it only contained mention.")
            return

    logger.info(f"Processing message from {message.from_user.name} in chat {message.chat.id}: '{effective_text}'")

    service_type, clean_prompt, model_name = await route_request(effective_text, bot_username if is_mentioned else None)

    if not clean_prompt and service_type != "capabilities":
        await update.message.reply_text("Пожалуйста, укажите ваш запрос после упоминания или ключевого слова.")
        return

    try:
        if service_type == "capabilities":
            for message_part in clean_prompt:
                await update.message.reply_text(message_part)
        elif service_type == "image":
            await update.message.reply_text("🎨 Генерирую изображение, это может занять некоторое время...")
            image_url = await generate_image(clean_prompt)
            if image_url.startswith("http"):
                logger.info(f"Bot image response (PiAPI): {image_url}")
                escaped_prompt = escape_markdown_v2(clean_prompt)
                caption = f"🖼 Изображение по запросу: {escaped_prompt}\n\\(Сгенерировано с помощью PiAPI\\.ai\\)"
                await update.message.reply_photo(image_url, caption=caption, parse_mode='MarkdownV2')
                
                # Сохраняем запрос на изображение в истории
                add_message(chat_id, user_id, "user", "image", clean_prompt)
                add_message(chat_id, user_id, "assistant", "image", f"Сгенерировано изображение: {image_url}")
            else:
                logger.info(f"Bot image error response (PiAPI): {image_url}")
                await update.message.reply_text(f"Ошибка генерации изображения: {image_url}")
        elif service_type == "text" and model_name:
            # Сохраняем запрос пользователя в истории
            add_message(chat_id, user_id, "user", model_name, clean_prompt)
            
            # Генерируем ответ с учетом истории и суммаризации
            response_text = await generate_text(clean_prompt, model_name, chat_id, user_id)
            logger.info(f"Bot response ({model_name}): {response_text}")
            
            # Сохраняем ответ бота в истории
            add_message(chat_id, user_id, "assistant", model_name, response_text)
            
            escaped_model_name = escape_markdown_v2(model_name)
            escaped_response_text = escape_markdown_v2(response_text)
            await update.message.reply_markdown_v2(f"Ответ от `{escaped_model_name}`:\n\n{escaped_response_text}")
    except Exception as e:
        logger.error(f"Error handling message: {e}", exc_info=True)
        error_message_escaped = escape_markdown_v2(str(e))
        await update.message.reply_markdown_v2(f"Извините, произошла ошибка при обработке вашего запроса\\.\n`{error_message_escaped}`") 