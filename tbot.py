import logging
import os
import asyncio
import contextvars
from functools import wraps
from pathlib import Path
from telegram import Bot, Message
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from dotenv import load_dotenv
from config import BOT_CONFIG
from utils.helpers import post_init, notify_admins_on_startup, resolve_system_prompt
from handlers.commands import (
    clear_memory_command,
    help_command,
    admin_help_command,
    user_profile_command,
    models_all_command,
    models_command,
    models_free_command,
    models_large_context_command,
    models_paid_command,
    models_pic_command,
    models_specialized_command,
    models_voice_command,
    models_voice_log_command,
    tts_voices_command,
    models_free_callback,
    models_large_context_callback,
    models_paid_callback,
    models_pic_callback,
    models_specialized_callback,
    set_model_number_command,
    set_pic_model_number_command,
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
    say_command,
    set_tts_voice_command,
    set_tts_provider_command,
)
from handlers.chat_tracking import track_chat
from handlers.messages import handle_message
from handlers.voice_messages import handle_voice_message, voice_confirmation_command
from services.generation import (
    init_client,
    check_model_availability,
    refresh_models_from_api,
)
from services.memory import add_message_unique, init_db
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
BOT_CONFIG["MINI_APP_URL"] = os.getenv("MINI_APP_URL")
voice_prompt_env = os.getenv("VOICE_TRANSCRIBE_PROMPT")
if voice_prompt_env is not None:
    BOT_CONFIG["VOICE_TRANSCRIBE_PROMPT"] = voice_prompt_env
voice_local_url_env = os.getenv("VOICE_LOCAL_WHISPER_URL")
if voice_local_url_env is not None:
    BOT_CONFIG["VOICE_LOCAL_WHISPER_URL"] = voice_local_url_env
tts_model_env = os.getenv("TTS_MODEL")
if tts_model_env is not None:
    BOT_CONFIG["TTS_MODEL"] = tts_model_env
tts_voice_env = os.getenv("TTS_VOICE")
if tts_voice_env is not None:
    BOT_CONFIG["TTS_VOICE"] = tts_voice_env

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


_COMMAND_MEMORY_CONTEXT = contextvars.ContextVar("command_memory_context", default=None)
_COMMAND_MEMORY_PATCHED = False


def _persist_command_memory(role: str, model: str, text: str | None, *, chat_id: str | None = None) -> None:
    if not text:
        return

    ctx = _COMMAND_MEMORY_CONTEXT.get()
    if not ctx:
        return

    ctx_chat_id = ctx.get("chat_id")
    ctx_user_id = ctx.get("user_id")
    if not ctx_chat_id or not ctx_user_id:
        return

    target_chat_id = str(chat_id) if chat_id is not None else str(ctx_chat_id)
    if target_chat_id != str(ctx_chat_id):
        return

    try:
        add_message_unique(str(ctx_chat_id), str(ctx_user_id), role, model, str(text))
    except Exception as exc:
        logger.warning("Failed to persist command message (%s): %s", role, exc)


def _extract_text(args, kwargs) -> str | None:
    text = kwargs.get("text")
    if text is None and args:
        text = args[0]
    return str(text) if text is not None else None


def _extract_marker_text(args, kwargs, marker: str) -> str:
    caption = kwargs.get("caption")
    if caption is None and len(args) > 1:
        caption = args[1]
    if caption is None:
        return marker
    return f"{marker} {caption}"


def _ensure_command_memory_patched() -> None:
    global _COMMAND_MEMORY_PATCHED
    if _COMMAND_MEMORY_PATCHED:
        return

    def _patch_message_text_method(method_name: str) -> None:
        original = getattr(Message, method_name, None)
        if not original or getattr(original, "_command_memory_wrapped", False):
            return

        @wraps(original)
        async def _wrapped(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            _persist_command_memory(
                "assistant",
                "command",
                _extract_text(args, kwargs),
                chat_id=str(getattr(self, "chat_id", "")),
            )
            return result

        _wrapped._command_memory_wrapped = True
        setattr(Message, method_name, _wrapped)

    def _patch_message_media_method(method_name: str, marker: str) -> None:
        original = getattr(Message, method_name, None)
        if not original or getattr(original, "_command_memory_wrapped", False):
            return

        @wraps(original)
        async def _wrapped(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            _persist_command_memory(
                "assistant",
                "command",
                _extract_marker_text(args, kwargs, marker),
                chat_id=str(getattr(self, "chat_id", "")),
            )
            return result

        _wrapped._command_memory_wrapped = True
        setattr(Message, method_name, _wrapped)

    def _patch_bot_text_method(method_name: str) -> None:
        original = getattr(Bot, method_name, None)
        if not original or getattr(original, "_command_memory_wrapped", False):
            return

        @wraps(original)
        async def _wrapped(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            chat_id = kwargs.get("chat_id")
            if chat_id is None and args:
                chat_id = args[0]
            payload_args = args[1:] if args else args
            _persist_command_memory(
                "assistant",
                "command",
                _extract_text(payload_args, kwargs),
                chat_id=str(chat_id) if chat_id is not None else None,
            )
            return result

        _wrapped._command_memory_wrapped = True
        setattr(Bot, method_name, _wrapped)

    def _patch_bot_media_method(method_name: str, marker: str) -> None:
        original = getattr(Bot, method_name, None)
        if not original or getattr(original, "_command_memory_wrapped", False):
            return

        @wraps(original)
        async def _wrapped(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            chat_id = kwargs.get("chat_id")
            if chat_id is None and args:
                chat_id = args[0]
            payload_args = args[1:] if args else args
            _persist_command_memory(
                "assistant",
                "command",
                _extract_marker_text(payload_args, kwargs, marker),
                chat_id=str(chat_id) if chat_id is not None else None,
            )
            return result

        _wrapped._command_memory_wrapped = True
        setattr(Bot, method_name, _wrapped)

    _patch_message_text_method("reply_text")
    _patch_message_text_method("reply_markdown_v2")
    _patch_message_media_method("reply_photo", "[photo]")
    _patch_message_media_method("reply_document", "[document]")
    _patch_message_media_method("reply_voice", "[voice]")
    _patch_message_media_method("reply_audio", "[audio]")
    _patch_message_media_method("reply_video", "[video]")

    _patch_bot_text_method("send_message")
    _patch_bot_media_method("send_photo", "[photo]")
    _patch_bot_media_method("send_document", "[document]")
    _patch_bot_media_method("send_voice", "[voice]")
    _patch_bot_media_method("send_audio", "[audio]")

    _COMMAND_MEMORY_PATCHED = True


def _command_with_memory(callback):
    @wraps(callback)
    async def _wrapped(update, context):
        message = update.effective_message
        chat_id = str(message.chat_id) if message and message.chat_id is not None else None
        user_id = str(message.from_user.id) if message and message.from_user else None

        token = _COMMAND_MEMORY_CONTEXT.set({"chat_id": chat_id, "user_id": user_id})
        try:
            if chat_id and user_id and message and message.text:
                _persist_command_memory("user", "command", message.text, chat_id=chat_id)
            return await callback(update, context)
        finally:
            _COMMAND_MEMORY_CONTEXT.reset(token)

    return _wrapped


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

    _ensure_command_memory_patched()

    # Регистрация обработчиков команд
    application.add_handler(MessageHandler(filters.ALL, track_chat), group=-1)
    application.add_handler(CommandHandler("start", _command_with_memory(start)))
    application.add_handler(CommandHandler("new", _command_with_memory(new_dialog)))
    application.add_handler(CommandHandler("clear", _command_with_memory(clear_memory_command)))
    application.add_handler(CommandHandler("admin", _command_with_memory(admin_command)))
    application.add_handler(CommandHandler("help", _command_with_memory(help_command)))
    application.add_handler(CommandHandler("admin_help", _command_with_memory(admin_help_command)))
    application.add_handler(CommandHandler("user_profile", _command_with_memory(user_profile_command)))
    application.add_handler(CommandHandler("models", _command_with_memory(models_command)))
    application.add_handler(CommandHandler("models_free", _command_with_memory(models_free_command)))
    application.add_handler(CommandHandler("models_paid", _command_with_memory(models_paid_command)))
    application.add_handler(CommandHandler("models_large_context", _command_with_memory(models_large_context_command)))
    application.add_handler(CommandHandler("models_specialized", _command_with_memory(models_specialized_command)))
    application.add_handler(CommandHandler("models_all", _command_with_memory(models_all_command)))
    application.add_handler(CommandHandler("models_voice", _command_with_memory(models_voice_command)))
    application.add_handler(CommandHandler("voice_log_models", _command_with_memory(models_voice_log_command)))
    application.add_handler(CommandHandler("tts_voices", _command_with_memory(tts_voices_command)))
    application.add_handler(CommandHandler("models_pic", _command_with_memory(models_pic_command)))
    application.add_handler(CommandHandler("set_text_model", _command_with_memory(set_text_model_command)))
    application.add_handler(CommandHandler("set_voice_model", _command_with_memory(set_voice_model_command)))
    application.add_handler(CommandHandler("set_voice_log_model", _command_with_memory(set_voice_log_model_command)))
    application.add_handler(CommandHandler("set_pic_model", _command_with_memory(set_pic_model_command)))
    application.add_handler(CallbackQueryHandler(models_free_callback, pattern="^models_free:page:"))
    application.add_handler(CallbackQueryHandler(models_paid_callback, pattern="^models_paid:page:"))
    application.add_handler(CallbackQueryHandler(models_large_context_callback, pattern="^models_large_context:page:"))
    application.add_handler(CallbackQueryHandler(models_pic_callback, pattern="^models_pic:page:"))
    application.add_handler(CallbackQueryHandler(models_specialized_callback, pattern="^models_specialized:page:"))
    application.add_handler(MessageHandler(filters.Regex(r"^/set_model_\d+(?:@\w+)?$"), _command_with_memory(set_model_number_command)))
    application.add_handler(MessageHandler(filters.Regex(r"^/set_pic_model_\d+(?:@\w+)?$"), _command_with_memory(set_pic_model_number_command)))
    application.add_handler(CommandHandler("consilium", _command_with_memory(consilium_command)))
    application.add_handler(CommandHandler("selftest", _command_with_memory(selftest_command)))
    application.add_handler(CommandHandler("header_on", _command_with_memory(header_on_command)))
    application.add_handler(CommandHandler("header_off", _command_with_memory(header_off_command)))
    application.add_handler(CommandHandler("rout_algo", _command_with_memory(routing_rules_command)))
    application.add_handler(CommandHandler("rout_llm", _command_with_memory(routing_llm_command)))
    application.add_handler(CommandHandler("rout", _command_with_memory(routing_mode_command)))
    application.add_handler(CommandHandler("voice_msg_conversation_on", _command_with_memory(voice_msg_conversation_on_command)))
    application.add_handler(CommandHandler("voice_msg_conversation_off", _command_with_memory(voice_msg_conversation_off_command)))
    application.add_handler(CommandHandler("voice_log_debug_on", _command_with_memory(voice_log_debug_on_command)))
    application.add_handler(CommandHandler("voice_log_debug_off", _command_with_memory(voice_log_debug_off_command)))
    application.add_handler(CommandHandler("voice_send_raw", _command_with_memory(voice_send_raw_command)))
    application.add_handler(CommandHandler("voice_send_segmented", _command_with_memory(voice_send_segmented_command)))
    application.add_handler(CommandHandler("say", _command_with_memory(say_command)))
    application.add_handler(CommandHandler("set_tts_voice", _command_with_memory(set_tts_voice_command)))
    application.add_handler(CommandHandler("set_tts_provider", _command_with_memory(set_tts_provider_command)))
    application.add_handler(CommandHandler("yes", _command_with_memory(voice_confirmation_command)))
    application.add_handler(CommandHandler("y", _command_with_memory(voice_confirmation_command)))
    application.add_handler(CommandHandler("setflow", _command_with_memory(setflow_command)))
    application.add_handler(CommandHandler("flow", _command_with_memory(flow_command)))
    application.add_handler(CommandHandler("unsetflow", _command_with_memory(unsetflow_command)))
    application.add_handler(CommandHandler("show_discord_chats", _command_with_memory(show_discord_chats_command)))
    application.add_handler(CommandHandler("show_tg_chats", _command_with_memory(show_tg_chats_command)))
    
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
