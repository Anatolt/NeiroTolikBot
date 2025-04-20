import logging
import os
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import re # Import re for escaping markdown
from openai import AsyncOpenAI
import json
from typing import Optional
from dotenv import load_dotenv
import aiohttp
import asyncio

# Load environment variables
load_dotenv()

# Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
PIAPI_KEY = os.getenv("PIAPI_KEY")  # Add PIAPI key
DEFAULT_MODEL = "anthropic/claude-3-haiku"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
BOT_REFERER = "https://t.me/NeiroTolikBot"
BOT_TITLE = "NeiroTolikBot"

# Initialize OpenAI client with async support
client = AsyncOpenAI(
    base_url=OPENROUTER_BASE_URL,
    api_key=OPENROUTER_API_KEY,
    default_headers={
        "HTTP-Referer": BOT_REFERER,
        "X-Title": BOT_TITLE
    }
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Placeholder Functions ---
async def generate_text(prompt: str, model: str) -> str:
    """Generate text using OpenRouter API."""
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": prompt} # Send only the user prompt
            ],
            max_tokens=1000, # Reverted max_tokens
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error generating text: {str(e)}")
        return f"Произошла ошибка при генерации текста: {str(e)}"

async def generate_image(prompt: str) -> str:
    """Generate image using PiAPI.ai."""
    if not PIAPI_KEY:
        logger.error("PIAPI_KEY environment variable is not set.")
        return "Ошибка конфигурации: Ключ API для генерации изображений не найден."

    try:
        url = "https://api.piapi.ai/api/v1/task" # New PiAPI.ai URL
        headers = {
            "X-API-Key": PIAPI_KEY,             # Correct header name
            "Content-Type": "application/json"
        }

        payload = {
            "model": "Qubico/flux1-schnell",     # Example model from PiAPI.ai docs
            "task_type": "txt2img",
            "input": {
                "prompt": prompt,
                "negative_prompt": "ugly, blurry, bad quality, distorted", # Keep negative prompt
                # Add other relevant params if needed from PiAPI.ai docs
                "aspect_ratio": "square" # Keep aspect ratio if supported
            }
        }

        async with aiohttp.ClientSession() as session:
            # 1. Start generation task
            logger.info(f"Sending image generation request to PiAPI.ai for prompt: {prompt}")
            async with session.post(url, headers=headers, data=json.dumps(payload)) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"PiAPI.ai Error Response: {error_text} (Status: {response.status})")
                    raise Exception(f"Failed to start PiAPI.ai image generation: {error_text}")

                task_data = await response.json()
                # Correctly extract task_id from the nested 'data' dictionary
                data_dict = task_data.get("data")
                task_id = data_dict.get("task_id") if data_dict else None

                if not task_id:
                    logger.error(f"No task_id received from PiAPI.ai: {task_data}")
                    raise Exception("No task_id received from PiAPI.ai")

                logger.info(f"Started PiAPI.ai image generation task: {task_id}")

            # 2. Poll for task completion
            max_attempts = 60  # Increase polling time slightly if needed (60 seconds)
            attempts = 0
            status_check_url = f"{url}/{task_id}" # URL for checking status (assuming GET to the same endpoint + /task_id)

            while attempts < max_attempts:
                await asyncio.sleep(2) # Wait 2 seconds between checks
                logger.info(f"Checking status for task {task_id} (Attempt {attempts + 1}/{max_attempts})")
                async with session.get(status_check_url, headers=headers) as response: # Use GET for status
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Status check failed for task {task_id}: {error_text} (Status: {response.status})")
                        # Don't raise immediately, maybe a temporary issue
                        attempts += 1
                        continue # Try again

                    status_data = await response.json()
                    # Correctly extract status from the nested 'data' dictionary
                    data_dict = status_data.get("data", {})
                    task_status = data_dict.get("status")
                    logger.info(f"Task {task_id} status: {task_status}")

                    if task_status == "completed":
                        # Extract result URL from data['output']['image_url']
                        output_dict = data_dict.get("output", {})
                        image_url = output_dict.get("image_url")
                        if image_url:
                            logger.info(f"Image generation successful for task {task_id}: {image_url}")
                            return image_url
                        else:
                            logger.error(f"Completed task {task_id} but no result URL found: {status_data}")
                            raise Exception("No image URL in successful PiAPI.ai response")
                    elif task_status == "failed":
                        # Extract error details, potentially also nested
                        error_details = data_dict.get("error", {}).get("message", "Unknown error")
                        logger.error(f"Image generation failed for task {task_id}: {error_details}")
                        raise Exception(f"PiAPI.ai image generation failed: {error_details}")
                    elif task_status in ["processing", "pending"]:
                         # Continue polling
                         pass
                    else:
                        logger.warning(f"Unknown task status for {task_id}: {task_status}")
                        # Decide how to handle unknown statuses, maybe continue polling for a bit

                    attempts += 1

            logger.error(f"Image generation timed out for task {task_id}")
            raise Exception("Image generation timed out with PiAPI.ai")

    except Exception as e:
        logger.error(f"Error generating image with PiAPI.ai: {str(e)}", exc_info=True)
        return f"Произошла ошибка при генерации изображения через PiAPI.ai: {str(e)}"

# Добавим новую функцию для получения возможностей
async def get_capabilities() -> str:
    """Get and format information about available models."""
    try:
        response = await client.models.list()
        
        # Форматируем ответ в читаемый вид
        capabilities = ["Вот что я умею:\n\n🤖 Доступные модели:\n"]
        current_part = capabilities[0]
        
        # Обрабатываем данные как словарь
        for model in response.data:
            model_data = model if isinstance(model, dict) else model.model_dump()  # Используем model_dump вместо dict
            model_id = model_data.get('id', 'Unknown')
            context_length = model_data.get('context_length', 'N/A')
            pricing = model_data.get('pricing', {})
            prompt_price = pricing.get('prompt', 'N/A') if isinstance(pricing, dict) else 'N/A'
            
            model_info = f"• {model_id} (макс. контекст: {context_length})\n"
            if prompt_price != 'N/A':
                model_info += f"  └─ Цена: ${prompt_price}/1K токенов\n"
            
            # Если текущая часть станет слишком длинной, начинаем новую
            if len(current_part + model_info) > 3000:  # Оставляем запас от лимита в 4096
                capabilities.append(model_info)
                current_part = model_info
            else:
                current_part += model_info
        
        # Добавляем инструкции в последнее сообщение
        instructions = "\n💡 Как использовать:\n"
        instructions += "• Просто напиши свой вопрос - отвечу через claude-3-haiku\n"
        instructions += "• Укажи модель в начале ('chatgpt расскажи о погоде')\n"
        instructions += "• Или в конце ('расскажи о погоде через claude')\n"
        instructions += "• Для картинок используй 'нарисуй' или 'сгенерируй картинку'"
        
        if len(current_part + instructions) > 3000:
            capabilities.append(instructions)
        else:
            capabilities[-1] += instructions
        
        return capabilities  # Возвращаем список сообщений
    except Exception as e:
        logger.error(f"Error getting capabilities: {str(e)}")
        return ["Извините, не удалось получить информацию о моих возможностях."]

# --- Routing Logic ---
async def route_request(text: str, bot_username: str | None) -> tuple[str, str, str | None]:
    """
    Parses the message text, identifies the target service (text/image)
    and model (if specified), and extracts the clean prompt.

    Returns:
        tuple: (service_type, clean_prompt, model_name)
               service_type: "text" or "image"
               clean_prompt: The user's prompt without keywords/mentions
               model_name: The requested model or None if default or image
    """
    text_lower = text.lower()
    prompt = text
    model = None
    service = "text" # Default to text generation

    # Проверяем запрос возможностей
    capability_keywords = ["что ты умеешь", "твои возможности", "помощь", "справка", "help"]
    if any(keyword in text_lower for keyword in capability_keywords):
        capabilities = await get_capabilities()
        # Возвращаем специальный тип сервиса для обработки справки
        return "capabilities", capabilities, None

    # In groups, mention is already removed before calling this function if check passed
    # If called from private chat, bot_username is None

    # --- Keyword-based Routing ---
    image_keywords = ["нарисуй", "картинка", "изображение", "сгенерируй картинку", "generate image"]
    # Use word boundaries to avoid matching parts of words, check start/end
    if any(f" {keyword} " in f" {text_lower} " or text_lower.startswith(keyword) or text_lower.endswith(keyword) for keyword in image_keywords):
        service = "image"
        # More robust keyword removal
        for keyword in image_keywords:
            # Try removing "keyword " or " keyword" or just "keyword" if it's the whole prompt
            if prompt.lower().startswith(keyword + " "):
                prompt = prompt[len(keyword) + 1:].strip()
            elif prompt.lower().endswith(" " + keyword):
                prompt = prompt[:-len(keyword) - 1].strip()
            elif prompt.lower() == keyword:
                 prompt = ""
                 break
            # Simple replacement as fallback (might catch parts of words in some cases)
            prompt = prompt.replace(keyword, "", 1).strip()
            prompt = prompt.replace(keyword.capitalize(), "", 1).strip() # Handle capitalization

        logger.info(f"Routing to image generation. Clean prompt: '{prompt}'")
        return service, prompt, None # Model not relevant for image placeholder

    # --- Model Specification Routing (Example) ---
    model_keywords = {
        "deepseek": "deepseek/deepseek-r1-distill-qwen-14b",
        "chatgpt": "mistralai/mistral-large-2407",  # Используем Mistral как альтернативу ChatGPT
        "claude": "anthropic/claude-2.1:beta",
        "qwen": "qwen/qwen2.5-vl-3b-instruct:free",
        "llama": "meta-llama/llama-3.1-8b-instruct:free",
        "fimbulvetr": "sao10k/fimbulvetr-11b-v2"
    }

    found_model_keyword = None
    # Check for formats like "... via model", "... using model", "model ..."
    for keyword, model_id in model_keywords.items():
        # Check at the end of the string (e.g., "prompt text via chatgpt")
        if text_lower.endswith(f" via {keyword}") or text_lower.endswith(f" через {keyword}"):
            model = model_id
            phrase_len = len(f" via {keyword}") if text_lower.endswith(f" via {keyword}") else len(f" через {keyword}")
            prompt = prompt[:-phrase_len].strip()
            found_model_keyword = keyword
            break
        # Check at the beginning (e.g., "chatgpt prompt text")
        elif text_lower.startswith(keyword + " "):
             model = model_id
             prompt = prompt[len(keyword):].strip()
             found_model_keyword = keyword
             break
        # Check for simple keyword presence as a fallback (less precise)
        # elif f" {keyword} " in f" {text_lower} ": # Avoid using this as it conflicts easily
        #     model = model_id
        #     # Removing the keyword here is tricky, might leave prompt fragmented
        #     # Consider just using the model if found this way, without altering prompt much
        #     logger.warning(f"Found model keyword '{keyword}' mid-prompt. Extraction might be imperfect.")
        #     prompt = prompt.replace(keyword, "").strip() # Simplistic removal
        #     found_model_keyword = keyword
        #     break


    if found_model_keyword:
         logger.info(f"Routing to text generation with specified model: {model}. Clean prompt: '{prompt}'")
    else:
        model = DEFAULT_MODEL
        logger.info(f"Routing to text generation with default model: {model}. Clean prompt: '{prompt}'")


    return service, prompt, model


# --- Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles incoming messages, routes them, and calls the appropriate generator."""
    message = update.message
    if not message or not message.text:
        return

    bot_username = context.bot.username
    text = message.text
    chat_type = message.chat.type
    effective_text = text # Keep original text for routing if needed, modify this one

    # In groups, only respond if mentioned
    is_mentioned = False
    if chat_type in ['group', 'supergroup']:
        mention = f"@{bot_username}"
        if effective_text.startswith(mention):
             effective_text = effective_text[len(mention):].strip()
             is_mentioned = True
        elif mention in effective_text: # Mention somewhere else? Less common for direct commands.
             # We could ignore these, or try to handle, but for now, require start mention.
             logger.debug(f"Ignoring message in group {message.chat.id} - mention not at start.")
             return
        else:
            logger.debug(f"Ignoring message in group {message.chat.id} as bot was not mentioned.")
            return

        if not effective_text: # Ignore message if it only contained the mention
             logger.debug(f"Ignoring message in group {message.chat.id} as it only contained mention.")
             return

    # If it's a private chat or the bot was mentioned in a group
    logger.info(f"Processing message from {message.from_user.name} in chat {message.chat.id}: '{effective_text}'")

    # Route the request using the text *after* removing the mention (if any)
    service_type, clean_prompt, model_name = await route_request(effective_text, bot_username if is_mentioned else None)

    if not clean_prompt and service_type != "capabilities":  # Check added to handle empty prompt correctly
        await update.message.reply_text("Пожалуйста, укажите ваш запрос после упоминания или ключевого слова.")
        return

    # Call the appropriate function based on routing
    try:
        if service_type == "capabilities":
            # clean_prompt now contains a list of messages
            for message_part in clean_prompt:
                # Assuming capabilities text is already formatted/escaped if needed
                await update.message.reply_text(message_part)
        elif service_type == "image":
            await update.message.reply_text("🎨 Генерирую изображение, это может занять некоторое время...")
            image_url = await generate_image(clean_prompt)
            if image_url.startswith("http"):
                logger.info(f"Bot image response (PiAPI): {image_url}")
                # Escape the prompt for the caption using MarkdownV2
                escaped_prompt = escape_markdown_v2(clean_prompt)
                caption = f"🖼 Изображение по запросу: {escaped_prompt}\n\\(Сгенерировано с помощью PiAPI\\.ai\\)"
                await update.message.reply_photo(image_url, caption=caption, parse_mode='MarkdownV2')
            else:
                logger.info(f"Bot image error response (PiAPI): {image_url}")
                # Keep error message reply as plain text
                await update.message.reply_text(f"Ошибка генерации изображения: {image_url}")
        elif service_type == "text" and model_name:
            response_text = await generate_text(clean_prompt, model_name)
            logger.info(f"Bot response ({model_name}): {response_text}")
            # Escape model name and response for MarkdownV2
            escaped_model_name = escape_markdown_v2(model_name)
            escaped_response_text = escape_markdown_v2(response_text)
            await update.message.reply_markdown_v2(f"Ответ от `{escaped_model_name}`:\n\n{escaped_response_text}")
    except Exception as e:
        logger.error(f"Error handling message: {e}", exc_info=True)
        # Escape the error message for safety, though it might not be markdown formatted
        error_message_escaped = escape_markdown_v2(str(e))
        await update.message.reply_markdown_v2(f"Извините, произошла ошибка при обработке вашего запроса\\.\n`{error_message_escaped}`")


async def post_init(application: Application) -> None:
    """Sets bot commands after initialization."""
    await application.bot.set_my_commands([
        BotCommand("start", "Начать диалог"),
        # Add more commands if needed
    ])
    logger.info("Bot commands set.")

def escape_markdown_v2(text: str) -> str:
    """Escapes characters for Telegram MarkdownV2."""
    # Chars to escape: _ * [ ] ( ) ~ ` > # + - = | { } . !
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
    user = update.effective_user
    user_mention = user.mention_markdown_v2() # Use Markdown mention
    # Use MarkdownV2 for the reply
    default_model_escaped = escape_markdown_v2(DEFAULT_MODEL) # Escape model name

    # Using MarkdownV2 with \n for line breaks
    text = (
        f"Привет, {user_mention}\\! Я бот\\-помощник\\.\n\n"
        f"📝 Спроси меня что\\-нибудь, и я отвечу с помощью `{default_model_escaped}`\\.\n"
        f"🎨 Попроси нарисовать картинку \\(например, 'нарисуй закат над морем'\\)\\.\n"
        f"🤖 Хочешь ответ от другой модели? Укажи ее в конце запроса \\(например, '\\.\\.\\. через deepseek', '\\.\\.\\. via claude'\\) или в начале \\(например, 'chatgpt какой сегодня день?'\\)\\.\n"
        f"   Сейчас поддерживаются: deepseek, chatgpt, claude\\."
    )

    await update.message.reply_markdown_v2(
        text=text,
        # disable_web_page_preview=True # Still useful
    )


# --- Main Function ---
def main() -> None:
    """Start the bot."""
    # Check if environment variables are set
    if not TELEGRAM_BOT_TOKEN or not OPENROUTER_API_KEY:
        logger.error("Please set TELEGRAM_BOT_TOKEN and OPENROUTER_API_KEY in .env file")
        return

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # --- Handlers ---
    # Handle /start command
    application.add_handler(CommandHandler("start", start))

    # Handle regular messages using the routing logic
    # Ensure it handles both private chats and mentions in groups
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Run the bot until the user presses Ctrl-C
    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main() 
