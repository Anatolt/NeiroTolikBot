import logging
import os
import asyncio
from pathlib import Path
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from dotenv import load_dotenv
from config import BOT_CONFIG
from utils.helpers import post_init, notify_admins_on_startup, resolve_system_prompt
from handlers.commands import (
    clear_memory_command,
    help_command,
    admin_help_command,
    models_all_command,
    models_command,
    models_free_command,
    models_large_context_command,
    models_paid_command,
    models_pic_command,
    models_specialized_command,
    models_voice_command,
    models_voice_log_command,
    models_free_callback,
    set_text_model_command,
    set_voice_model_command,
    set_voice_log_model_command,
    set_pic_model_command,
    setflow_command,
    flow_command,
    unsetflow_command,
    show_discord_chats_command,
    show_tg_chats_command,
    selftest_command,
    new_dialog,
    start,
    admin_command,
    consilium_command,
    header_off_command,
    header_on_command,
    routing_llm_command,
    routing_mode_command,
    routing_rules_command,
    voice_msg_conversation_off_command,
    voice_msg_conversation_on_command,
    voice_log_debug_off_command,
    voice_log_debug_on_command,
    voice_send_raw_command,
    voice_send_segmented_command,
)
from handlers.chat_tracking import track_chat
from handlers.messages import handle_message
from handlers.voice_messages import handle_voice_message, voice_confirmation_command
from services.generation import (
    init_client,
    check_model_availability,
    refresh_models_from_api,
)
from services.memory import init_db
from datetime import datetime

# Загрузка переменных окружения
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# Загрузка конфигурации из .env
BOT_CONFIG["TELEGRAM_BOT_TOKEN"] = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_CONFIG["DISCORD_BOT_TOKEN"] = os.getenv("DISCORD_BOT_TOKEN")
BOT_CONFIG["OPENROUTER_API_KEY"] = os.getenv("OPENROUTER_API_KEY")
BOT_CONFIG["PIAPI_KEY"] = os.getenv("PIAPI_KEY")
BOT_CONFIG["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
BOT_CONFIG["IMAGE_ROUTER_KEY"] = os.getenv("IMAGE_ROUTER_KEY")
BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"] = resolve_system_prompt(BASE_DIR)
BOT_CONFIG["ADMIN_PASS"] = os.getenv("PASS")
BOT_CONFIG["BOOT_TIME"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
voice_prompt_env = os.getenv("VOICE_TRANSCRIBE_PROMPT")
if voice_prompt_env is not None:
    BOT_CONFIG["VOICE_TRANSCRIBE_PROMPT"] = voice_prompt_env
voice_local_url_env = os.getenv("VOICE_LOCAL_WHISPER_URL")
if voice_local_url_env is not None:
    BOT_CONFIG["VOICE_LOCAL_WHISPER_URL"] = voice_local_url_env

# Необязательная настройка кастомных запасных моделей (через запятую)
fallback_models_env = os.getenv("FALLBACK_MODELS")
if fallback_models_env:
    BOT_CONFIG["FALLBACK_MODELS"] = [
        model.strip() for model in fallback_models_env.split(",") if model.strip()
    ]

# Параметры экономного потребления памяти
UPDATE_QUEUE_MAXSIZE = int(os.getenv("UPDATE_QUEUE_MAXSIZE", "50"))
MAX_CONCURRENT_UPDATES = int(os.getenv("MAX_CONCURRENT_UPDATES", "2"))

# Инициализация клиента OpenRouter
init_client()

# Инициализация базы данных для памяти
init_db()

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

async def check_default_model():
    """Выбирает лучшую доступную модель и обновляет алиасы."""
    try:
        await refresh_models_from_api()
    except Exception as e:
        logger.error(f"Failed to refresh models from API: {str(e)}")

    # Проверяем доступность модели по умолчанию и резервных
    models_to_probe = []
    for candidate in [BOT_CONFIG.get("DEFAULT_MODEL"), *BOT_CONFIG.get("FALLBACK_MODELS", [])]:
        if candidate and candidate not in models_to_probe:
            models_to_probe.append(candidate)

    for candidate in models_to_probe:
        if await check_model_availability(candidate):
            BOT_CONFIG["DEFAULT_MODEL"] = candidate
            logger.info(f"Using available default model: {candidate}")
            break
    else:
        logger.warning(
            f"No available models from the list {models_to_probe}. Falling back to openai/gpt-3.5-turbo"
        )
        BOT_CONFIG["DEFAULT_MODEL"] = "openai/gpt-3.5-turbo"

async def main() -> None:
    """Основная функция запуска бота."""
    if not BOT_CONFIG["TELEGRAM_BOT_TOKEN"] or not BOT_CONFIG["OPENROUTER_API_KEY"]:
        logger.error("Please set TELEGRAM_BOT_TOKEN and OPENROUTER_API_KEY in .env file")
        return

    # Проверяем доступность модели по умолчанию
    await check_default_model()

    # Создаем приложение с ограничениями для экономии памяти
    update_queue = asyncio.Queue(maxsize=UPDATE_QUEUE_MAXSIZE)
    application = (
        Application.builder()
        .token(BOT_CONFIG["TELEGRAM_BOT_TOKEN"])
        .post_init(post_init)
        .concurrent_updates(False)
        .update_queue(update_queue)
        .build()
    )

    # Регистрация обработчиков команд
    application.add_handler(MessageHandler(filters.ALL, track_chat), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("new", new_dialog))
    application.add_handler(CommandHandler("clear", clear_memory_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin_help", admin_help_command))
    application.add_handler(CommandHandler("models", models_command))
    application.add_handler(CommandHandler("models_free", models_free_command))
    application.add_handler(CommandHandler("models_paid", models_paid_command))
    application.add_handler(CommandHandler("models_large_context", models_large_context_command))
    application.add_handler(CommandHandler("models_specialized", models_specialized_command))
    application.add_handler(CommandHandler("models_all", models_all_command))
    application.add_handler(CommandHandler("models_voice", models_voice_command))
    application.add_handler(CommandHandler("voice_log_models", models_voice_log_command))
    application.add_handler(CommandHandler("models_pic", models_pic_command))
    application.add_handler(CommandHandler("set_text_model", set_text_model_command))
    application.add_handler(CommandHandler("set_voice_model", set_voice_model_command))
    application.add_handler(CommandHandler("set_voice_log_model", set_voice_log_model_command))
    application.add_handler(CommandHandler("set_pic_model", set_pic_model_command))
    application.add_handler(CallbackQueryHandler(models_free_callback, pattern="^models_free:page:"))
    application.add_handler(CommandHandler("consilium", consilium_command))
    application.add_handler(CommandHandler("selftest", selftest_command))
    application.add_handler(CommandHandler("header_on", header_on_command))
    application.add_handler(CommandHandler("header_off", header_off_command))
    application.add_handler(CommandHandler("rout_algo", routing_rules_command))
    application.add_handler(CommandHandler("rout_llm", routing_llm_command))
    application.add_handler(CommandHandler("rout", routing_mode_command))
    application.add_handler(CommandHandler("voice_msg_conversation_on", voice_msg_conversation_on_command))
    application.add_handler(CommandHandler("voice_msg_conversation_off", voice_msg_conversation_off_command))
    application.add_handler(CommandHandler("voice_log_debug_on", voice_log_debug_on_command))
    application.add_handler(CommandHandler("voice_log_debug_off", voice_log_debug_off_command))
    application.add_handler(CommandHandler("voice_send_raw", voice_send_raw_command))
    application.add_handler(CommandHandler("voice_send_segmented", voice_send_segmented_command))
    application.add_handler(CommandHandler("yes", voice_confirmation_command))
    application.add_handler(CommandHandler("y", voice_confirmation_command))
    application.add_handler(CommandHandler("setflow", setflow_command))
    application.add_handler(CommandHandler("flow", flow_command))
    application.add_handler(CommandHandler("unsetflow", unsetflow_command))
    application.add_handler(CommandHandler("show_discord_chats", show_discord_chats_command))
    application.add_handler(CommandHandler("show_tg_chats", show_tg_chats_command))
    
    # Обработчик текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # Обработчик голосовых сообщений
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_message))

    logger.info("Starting bot polling...")
    
    # Запускаем бота
    await application.initialize()
    await application.start()
    # Указываем явно, какие типы обновлений получать (включая сообщения из групп)
    await application.updater.start_polling(allowed_updates=["message", "edited_message", "callback_query"])
    
    # Отправляем уведомления админам о перезапуске
    await notify_admins_on_startup(application)
    
    # Держим бота в активном состоянии
    try:
        # Создаем Future, который никогда не завершится
        stop = asyncio.Future()
        await stop
    except asyncio.CancelledError:
        logger.info("Bot is stopping...")
    finally:
        # Корректно завершаем работу
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Error running bot: {str(e)}")
