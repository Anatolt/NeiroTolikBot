import logging
from telegram import Update
from telegram.ext import ContextTypes
from utils.helpers import escape_markdown_v2
from config import BOT_CONFIG
from services.memory import start_new_dialog, clear_memory
from services.generation import init_client, fetch_models_data, categorize_models

logger = logging.getLogger(__name__)

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
        f"🤖 /models \\- Показать список доступных моделей\n\n"
        f"Также вы можете:\n"
        f"• Задавать вопросы боту\n"
        f"• Просить нарисовать картинки\n"
        f"• Указывать модель для ответа \\(например, 'chatgpt расскажи о погоде'\\)\n"
        f"• Написать 'модели' или 'models' для просмотра списка моделей"
    )
    
    await update.message.reply_markdown_v2(text=text)

async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /models - показывает список доступных моделей."""
    try:
        init_client()
        logger.info("Fetching models list from OpenRouter")
        models_data = await fetch_models_data()

        if not models_data:
            await update.message.reply_text("Не удалось получить список моделей. Пустой ответ от API.")
            return

        categories = categorize_models(models_data)

        category_titles = {
            "free": "БЕСПЛАТНЫЕ МОДЕЛИ:",
            "large_context": "МОДЕЛИ С БОЛЬШИМ КОНТЕКСТОМ (≥100K):",
            "specialized": "СПЕЦИАЛИЗИРОВАННЫЕ МОДЕЛИ:",
            "paid": "ПЛАТНЫЕ МОДЕЛИ:",
        }

        message = "🤖 Доступные модели по категориям:\n\n"
        max_items_per_category = 20

        for key in ["free", "large_context", "specialized", "paid"]:
            models = categories.get(key, [])
            if not models:
                continue

            message += f"{category_titles[key]}\n"
            displayed_models = models[:max_items_per_category]
            for model in displayed_models:
                model_id = model.get('id', 'Unknown')
                context_length = model.get('context_length', 0)
                context_kb = context_length / 1024 if context_length else 0
                context_str = f"{context_kb:.0f}K" if context_kb > 0 else 'N/A'
                message += f"• {model_id} ({context_str})\n"

            remaining = len(models) - len(displayed_models)
            if remaining > 0:
                message += f"…и еще {remaining} моделей в этой категории\n"

            message += "\n"

        # Разбиваем сообщение на части, если оно слишком длинное
        max_length = 3000
        message_parts = [message[i:i+max_length] for i in range(0, len(message), max_length)]

        for part in message_parts:
            await update.message.reply_text(part)

        logger.info("Models list sent successfully")
    except Exception as e:
        logger.error(f"Error fetching models list: {str(e)}")
        await update.message.reply_text("Не удалось получить список моделей. Пожалуйста, попробуйте позже.")
