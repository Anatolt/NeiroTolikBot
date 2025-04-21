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
from config import BOT_CONFIG

# Load environment variables
load_dotenv()

# Load configuration from .env
BOT_CONFIG["TELEGRAM_BOT_TOKEN"] = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_CONFIG["OPENROUTER_API_KEY"] = os.getenv("OPENROUTER_API_KEY")
BOT_CONFIG["PIAPI_KEY"] = os.getenv("PIAPI_KEY")
BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"] = os.getenv("CUSTOM_SYSTEM_PROMPT", "You are a helpful assistant.")

# Initialize OpenAI client with async support
client = AsyncOpenAI(
    base_url=BOT_CONFIG["OPENROUTER_BASE_URL"],
    api_key=BOT_CONFIG["OPENROUTER_API_KEY"],
    default_headers={
        "HTTP-Referer": BOT_CONFIG["BOT_REFERER"],
        "X-Title": BOT_CONFIG["BOT_TITLE"]
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
    messages = []
    if BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"]: # Only add if the prompt is set and not empty
        messages.append({"role": "system", "content": BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"]})
    messages.append({"role": "user", "content": prompt})

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=BOT_CONFIG["TEXT_GENERATION"]["MAX_TOKENS"],
            temperature=BOT_CONFIG["TEXT_GENERATION"]["TEMPERATURE"]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error generating text: {str(e)}")
        return f"Произошла ошибка при генерации текста: {str(e)}"

async def generate_image(prompt: str) -> str:
    """Generate image using PiAPI.ai."""
    if not BOT_CONFIG["PIAPI_KEY"]:
        logger.error("PIAPI_KEY environment variable is not set.")
        return "Ошибка конфигурации: Ключ API для генерации изображений не найден."

    try:
        url = "https://api.piapi.ai/api/v1/task"
        headers = {
            "X-API-Key": BOT_CONFIG["PIAPI_KEY"],
            "Content-Type": "application/json"
        }

        payload = {
            "model": BOT_CONFIG["IMAGE_GENERATION"]["MODEL"],
            "task_type": BOT_CONFIG["IMAGE_GENERATION"]["TASK_TYPE"],
            "input": {
                "prompt": prompt,
                "negative_prompt": BOT_CONFIG["IMAGE_GENERATION"]["NEGATIVE_PROMPT"],
                "aspect_ratio": BOT_CONFIG["IMAGE_GENERATION"]["ASPECT_RATIO"]
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
                data_dict = task_data.get("data")
                task_id = data_dict.get("task_id") if data_dict else None

                if not task_id:
                    logger.error(f"No task_id received from PiAPI.ai: {task_data}")
                    raise Exception("No task_id received from PiAPI.ai")

                logger.info(f"Started PiAPI.ai image generation task: {task_id}")

            # 2. Poll for task completion
            max_attempts = BOT_CONFIG["IMAGE_GENERATION"]["MAX_ATTEMPTS"]
            attempts = 0
            status_check_url = f"{url}/{task_id}"

            while attempts < max_attempts:
                await asyncio.sleep(BOT_CONFIG["IMAGE_GENERATION"]["POLLING_INTERVAL"])
                logger.info(f"Checking status for task {task_id} (Attempt {attempts + 1}/{max_attempts})")
                async with session.get(status_check_url, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Status check failed for task {task_id}: {error_text} (Status: {response.status})")
                        attempts += 1
                        continue

                    status_data = await response.json()
                    data_dict = status_data.get("data", {})
                    task_status = data_dict.get("status")
                    logger.info(f"Task {task_id} status: {task_status}")

                    if task_status == "completed":
                        output_dict = data_dict.get("output", {})
                        image_url = output_dict.get("image_url")
                        if image_url:
                            logger.info(f"Image generation successful for task {task_id}: {image_url}")
                            return image_url
                        else:
                            logger.error(f"Completed task {task_id} but no result URL found: {status_data}")
                            raise Exception("No image URL in successful PiAPI.ai response")
                    elif task_status == "failed":
                        error_details = data_dict.get("error", {}).get("message", "Unknown error")
                        logger.error(f"Image generation failed for task {task_id}: {error_details}")
                        raise Exception(f"PiAPI.ai image generation failed: {error_details}")
                    elif task_status in ["processing", "pending"]:
                        pass
                    else:
                        logger.warning(f"Unknown task status for {task_id}: {task_status}")

                    attempts += 1

            logger.error(f"Image generation timed out for task {task_id}")
            raise Exception("Image generation timed out with PiAPI.ai")

    except Exception as e:
        logger.error(f"Error generating image with PiAPI.ai: {str(e)}", exc_info=True)
        return f"Произошла ошибка при генерации изображения через PiAPI.ai: {str(e)}"

async def get_capabilities() -> str:
    """Get and format information about available models."""
    try:
        response = await client.models.list()
        
        capabilities = ["Вот что я умею:\n\n🤖 Доступные модели:\n"]
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
    text_lower = text.lower()
    prompt = text
    model = None
    service = "text"

    if any(keyword in text_lower for keyword in BOT_CONFIG["KEYWORDS"]["CAPABILITIES"]):
        capabilities = await get_capabilities()
        return "capabilities", capabilities, None

    if any(f" {keyword} " in f" {text_lower} " or text_lower.startswith(keyword) or text_lower.endswith(keyword) for keyword in BOT_CONFIG["KEYWORDS"]["IMAGE"]):
        service = "image"
        for keyword in BOT_CONFIG["KEYWORDS"]["IMAGE"]:
            if prompt.lower().startswith(keyword + " "):
                prompt = prompt[len(keyword) + 1:].strip()
            elif prompt.lower().endswith(" " + keyword):
                prompt = prompt[:-len(keyword) - 1].strip()
            elif prompt.lower() == keyword:
                prompt = ""
                break
            prompt = prompt.replace(keyword, "", 1).strip()
            prompt = prompt.replace(keyword.capitalize(), "", 1).strip()

        logger.info(f"Routing to image generation. Clean prompt: '{prompt}'")
        return service, prompt, None

    found_model_keyword = None
    for keyword, model_id in BOT_CONFIG["MODELS"].items():
        if text_lower.endswith(f" via {keyword}") or text_lower.endswith(f" через {keyword}"):
            model = model_id
            phrase_len = len(f" via {keyword}") if text_lower.endswith(f" via {keyword}") else len(f" через {keyword}")
            prompt = prompt[:-phrase_len].strip()
            found_model_keyword = keyword
            break
        elif text_lower.startswith(keyword + " "):
            model = model_id
            prompt = prompt[len(keyword):].strip()
            found_model_keyword = keyword
            break

    if found_model_keyword:
        logger.info(f"Routing to text generation with specified model: {model}. Clean prompt: '{prompt}'")
    else:
        model = BOT_CONFIG["DEFAULT_MODEL"]
        logger.info(f"Routing to text generation with default model: {model}. Clean prompt: '{prompt}'")

    return service, prompt, model

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    bot_username = context.bot.username
    text = message.text
    chat_type = message.chat.type
    effective_text = text

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
            else:
                logger.info(f"Bot image error response (PiAPI): {image_url}")
                await update.message.reply_text(f"Ошибка генерации изображения: {image_url}")
        elif service_type == "text" and model_name:
            response_text = await generate_text(clean_prompt, model_name)
            logger.info(f"Bot response ({model_name}): {response_text}")
            escaped_model_name = escape_markdown_v2(model_name)
            escaped_response_text = escape_markdown_v2(response_text)
            await update.message.reply_markdown_v2(f"Ответ от `{escaped_model_name}`:\n\n{escaped_response_text}")
    except Exception as e:
        logger.error(f"Error handling message: {e}", exc_info=True)
        error_message_escaped = escape_markdown_v2(str(e))
        await update.message.reply_markdown_v2(f"Извините, произошла ошибка при обработке вашего запроса\\.\n`{error_message_escaped}`")

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Начать диалог"),
    ])
    logger.info("Bot commands set.")

def escape_markdown_v2(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_mention = user.mention_markdown_v2()
    default_model_escaped = escape_markdown_v2(BOT_CONFIG["DEFAULT_MODEL"])

    text = (
        f"Привет, {user_mention}\\! Я бот\\-помощник\\.\n\n"
        f"📝 Спроси меня что\\-нибудь, и я отвечу с помощью `{default_model_escaped}`\\.\n"
        f"🎨 Попроси нарисовать картинку \\(например, 'нарисуй закат над морем'\\)\\.\n"
        f"🤖 Хочешь ответ от другой модели? Укажи ее в конце запроса \\(например, '\\.\\.\\. через deepseek', '\\.\\.\\. via claude'\\) или в начале \\(например, 'chatgpt какой сегодня день?'\\)\\.\n"
        f"   Сейчас поддерживаются: deepseek, chatgpt, claude\\."
    )

    await update.message.reply_markdown_v2(text=text)

def main() -> None:
    if not BOT_CONFIG["TELEGRAM_BOT_TOKEN"] or not BOT_CONFIG["OPENROUTER_API_KEY"]:
        logger.error("Please set TELEGRAM_BOT_TOKEN and OPENROUTER_API_KEY in .env file")
        return

    application = Application.builder().token(BOT_CONFIG["TELEGRAM_BOT_TOKEN"]).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main() 
