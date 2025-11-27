import logging
import os
import asyncio
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from dotenv import load_dotenv
from config import BOT_CONFIG
from utils.helpers import post_init
from handlers.commands import start, new_dialog, clear_memory_command, help_command, models_command
from handlers.messages import handle_message
from services.generation import init_client, check_model_availability
from services.memory import init_db

# Загрузка переменных окружения
load_dotenv()

# Загрузка конфигурации из .env
BOT_CONFIG["TELEGRAM_BOT_TOKEN"] = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_CONFIG["OPENROUTER_API_KEY"] = os.getenv("OPENROUTER_API_KEY")
BOT_CONFIG["PIAPI_KEY"] = os.getenv("PIAPI_KEY")
BOT_CONFIG["CUSTOM_SYSTEM_PROMPT"] = os.getenv("CUSTOM_SYSTEM_PROMPT", "You are a helpful assistant.")

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
    """Проверка доступности модели по умолчанию."""
    is_available = await check_model_availability(BOT_CONFIG["DEFAULT_MODEL"])
    if not is_available:
        logger.warning(f"Default model {BOT_CONFIG['DEFAULT_MODEL']} is not available. Falling back to gpt-3.5-turbo")
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
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("new", new_dialog))
    application.add_handler(CommandHandler("clear", clear_memory_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("models", models_command))
    
    # Обработчик текстовых сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Starting bot polling...")
    
    # Запускаем бота
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
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
