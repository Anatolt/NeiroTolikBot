from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from config import BOT_CONFIG
from utils.helpers import escape_markdown_v2
from services.memory import clear_memory, start_new_dialog, set_show_response_header, is_admin


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
    user = update.effective_user
    user_mention = user.mention_markdown_v2()
    default_model_escaped = escape_markdown_v2(BOT_CONFIG["DEFAULT_MODEL"])
    mini_app_url = (BOT_CONFIG.get("MINI_APP_URL") or "").strip()

    text = (
        f"Привет, {user_mention}\\! Я бот\\-помощник\\.\n\n"
        f"📝 Спроси меня что\\-нибудь, и я отвечу с помощью `{default_model_escaped}`\\.\n"
        f"🎨 Попроси нарисовать картинку \\(например, 'нарисуй закат над морем'\\)\\.\n"
        f"🤖 Хочешь ответ от другой модели? Укажи ее в конце запроса \\(например, '\\.\\.\\. через deepseek', '\\.\\.\\. via claude'\\) или в начале \\(например, 'chatgpt какой сегодня день?'\\)\\.\n"
        f"   Сейчас поддерживаются: deepseek, chatgpt, claude\\.\n\n"
        f"🔄 Используй /new для начала нового диалога \\(сохраняет историю\\)\\.\n"
        f"🧹 Используй /clear для полной очистки памяти\\.\n"
        f"❓ Используй /help для получения справки\\."
    )

    reply_markup = None
    if mini_app_url:
        text += "\n\n🚀 Открыть Mini App можно кнопкой ниже\\."
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚀 Открыть Mini App", web_app=WebAppInfo(url=mini_app_url))]]
        )

    await update.message.reply_markdown_v2(text=text, reply_markup=reply_markup)


async def new_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /new - начало нового диалога."""
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    start_new_dialog(chat_id, user_id)

    user_mention = user.mention_markdown_v2()
    await update.message.reply_markdown_v2(
        f"Привет, {user_mention}\\! Начинаю новый диалог\\.\n"
        f"История нашего общения сохранена и может быть использована в будущем\\."
    )


async def clear_memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /clear - полная очистка памяти."""
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    clear_memory(chat_id, user_id)

    user_mention = user.mention_markdown_v2()
    await update.message.reply_markdown_v2(
        f"{user_mention}, память полностью очищена\\.\n"
        f"Начинаю диалог с чистого листа\\."
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запрашивает пароль и включает режим админа."""
    if not BOT_CONFIG.get("ADMIN_PASS"):
        await update.message.reply_text("Пароль администратора не задан.")
        return

    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    if is_admin(chat_id, user_id) or context.user_data.get("is_admin"):
        await update.message.reply_text(
            f"Уже в режиме админа. Бот запущен: {BOT_CONFIG.get('BOOT_TIME')}"
        )
        return

    context.user_data["awaiting_admin_pass"] = True
    await update.message.reply_text("Введите пароль администратора:")


def build_help_text(user_name: str | None = None) -> str:
    resolved_name = (user_name or "друг").strip() or "друг"

    return (
        f"Привет, {resolved_name}! Вот список доступных команд:\n\n"
        f"📝 /new - Начать новый диалог (сохраняет историю для будущего использования)\n"
        f"🧹 /clear - Полностью очистить память бота\n"
        f"❓ /help - Показать эту справку\n"
        f"🗣 /say - Озвучить текст голосом\n"
        f"🤖 /models - Подсказка по спискам моделей\n"
        f"   /models_free, /models_paid, /models_large_context, /models_specialized\n"
        f"   /models_all — полный список моделей\n"
        f"🔀 /rout_algo или /rout_llm — выбрать алгоритмический или LLM роутинг\n"
        f"   /rout — показать текущий режим\n"
        f"🛠 /header_on или /header_off — показать или спрятать техшапку над ответом\n"
        f"🏥 /consilium - Получить ответы от нескольких моделей одновременно\n\n"
        f"🎧 /voice_alerts_on, /voice_alerts_off <guild_id> confirm, /voice_alerts_status — управление Telegram-алертами по Discord voice (для текущего чата)\n\n"
        f"🧩 /voice_chunks_on, /voice_chunks_off, /voice_chunks_status — управление отправкой voice-чанков в Telegram\n\n"
        f"Также вы можете:\n"
        f"• Задавать вопросы боту\n"
        f"• Просить нарисовать картинки\n"
        f"• Указывать модель для ответа (например, 'chatgpt расскажи о погоде')\n"
        f"• Использовать консилиум: 'консилиум: ваш вопрос' или 'консилиум через chatgpt, claude: вопрос'\n"
        f"• Использовать /models для просмотра списков моделей"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help - справка по командам."""
    user = update.effective_user
    await update.message.reply_text(text=build_help_text(user.full_name if user else None))


async def header_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Включает вывод техшапки над ответами."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_show_response_header(chat_id, user_id, True)
    await update.message.reply_text(
        "🛠 Техшапка включена. Чтобы скрыть, используйте /header_off или отправьте 'скрыть шапку'."
    )


async def header_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отключает вывод техшапки над ответами."""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    set_show_response_header(chat_id, user_id, False)
    await update.message.reply_text(
        "🫥 Техшапка скрыта. Чтобы вернуть её, используйте /header_on или отправьте 'показывай шапку'."
    )
