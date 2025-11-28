import logging
from telegram import Update
from telegram.ext import ContextTypes
from utils.helpers import escape_markdown_v2
from config import BOT_CONFIG
from services.memory import start_new_dialog, clear_memory
from services.generation import CATEGORY_TITLES, build_models_messages

logger = logging.getLogger(__name__)

MODELS_HINT_TEXT = (
    "🤖 Списки моделей по категориям:\n"
    "• /models_free — бесплатные\n"
    "• /models_paid — платные\n"
    "• /models_large_context — с большим контекстом\n"
    "• /models_specialized — специализированные\n"
    "• /models_all — полный список (может быть длинным)\n\n"
    "Можно также написать: 'покажи бесплатные модели', 'покажи платные модели' и т.д."
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
    user = update.effective_user
    user_mention = user.mention_markdown_v2()
    default_model_escaped = escape_markdown_v2(BOT_CONFIG["DEFAULT_MODEL"])

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

    await update.message.reply_markdown_v2(text=text)

async def new_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /new - начало нового диалога."""
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)
    
    # Начинаем новый диалог, сохраняя историю для будущей суммаризации
    session_id = start_new_dialog(chat_id, user_id)
    
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
    
    # Полностью очищаем память
    clear_memory(chat_id, user_id)
    
    user_mention = user.mention_markdown_v2()
    await update.message.reply_markdown_v2(
        f"{user_mention}, память полностью очищена\\.\n"
        f"Начинаю диалог с чистого листа\\."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /help - справка по командам."""
    user = update.effective_user
    user_mention = user.mention_markdown_v2()
    
    text = (
        f"Привет, {user_mention}\\! Вот список доступных команд:\n\n"
        f"📝 /new \\- Начать новый диалог \\(сохраняет историю для будущего использования\\)\n"
        f"🧹 /clear \\- Полностью очистить память бота\n"
        f"❓ /help \\- Показать эту справку\n"
        f"🤖 /models \\- Подсказка по спискам моделей\n"
        f"   /models_free, /models_paid, /models_large_context, /models_specialized\n"
        f"   /models_all — полный список моделей\n\n"
        f"Также вы можете:\n"
        f"• Задавать вопросы боту\n"
        f"• Просить нарисовать картинки\n"
        f"• Указывать модель для ответа \\(например, 'chatgpt расскажи о погоде'\\)\n"
        f"• Написать 'модели' или 'models' для просмотра списка моделей"
    )
    
    await update.message.reply_markdown_v2(text=text)

async def _send_models(update: Update, order: list[str], header: str, max_items: int | None = 20) -> None:
    """Получает модели и отправляет пользователю списком."""

    messages = await build_models_messages(order, header=header, max_items_per_category=max_items)

    if not messages:
        await update.message.reply_text("Не удалось получить список моделей. Пожалуйста, попробуйте позже.")
        return

    for part in messages:
        await update.message.reply_text(part)


async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /models - показывает подсказку по спискам моделей."""
    await update.message.reply_text(MODELS_HINT_TEXT)


async def models_free_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает бесплатные модели."""
    await _send_models(update, ["free"], CATEGORY_TITLES["free"], max_items=20)


async def models_paid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает платные модели."""
    await _send_models(update, ["paid"], CATEGORY_TITLES["paid"], max_items=20)


async def models_large_context_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает модели с большим контекстом."""
    await _send_models(update, ["large_context"], CATEGORY_TITLES["large_context"], max_items=20)


async def models_specialized_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает специализированные модели."""
    await _send_models(update, ["specialized"], CATEGORY_TITLES["specialized"], max_items=20)


async def models_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает полный список моделей по категориям."""
    await _send_models(update, ["free", "large_context", "specialized", "paid"], MODELS_HINT_TEXT, max_items=None)
